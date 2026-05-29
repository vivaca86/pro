from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from .config import FieldMapping, LanguageConfig
from .ingest import RawTableInfo, discover_files
from .language import classify_event, infer_fields, label_product
from .normalize import STANDARD_COLUMNS


CSV_SUFFIXES = {".csv", ".tsv", ".txt"}


class DuckDBNormalizeUnsupported(ValueError):
    pass


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _csv_relation(path: Path) -> str:
    options = ["all_varchar=true", "header=true"]
    if path.suffix.lower() == ".tsv":
        options.append("delim='\\t'")
    return f"read_csv_auto({_sql_literal(str(path))}, {', '.join(options)})"


def _text_expr(column: str | None, default: str = "NULL", blank_to_null: bool = True) -> str:
    if not column:
        return default
    value = f"TRIM(CAST({_quote_identifier(column)} AS VARCHAR))"
    if blank_to_null:
        return f"NULLIF({value}, '')"
    return value


def _number_expr(column: str | None) -> str:
    if not column:
        return "0"
    return f"COALESCE(TRY_CAST({_quote_identifier(column)} AS DOUBLE), 0)"


def _describe_columns(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [str(row[0]) for row in con.execute("DESCRIBE SELECT * FROM raw_in").fetchall()]


def _empty_frame(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def _distinct_text_values(con: duckdb.DuckDBPyConnection, column: str | None) -> list[str]:
    if not column:
        return []
    rows = con.execute(
        f"""
        SELECT DISTINCT value
        FROM (
            SELECT NULLIF(CAST({_quote_identifier(column)} AS VARCHAR), '') AS value
            FROM raw_in
        )
        WHERE value IS NOT NULL
        ORDER BY value
        """
    ).fetchall()
    return [str(row[0]) for row in rows]


def _distinct_event_values(con: duckdb.DuckDBPyConnection, column: str) -> list[str]:
    rows = con.execute(
        f"""
        SELECT DISTINCT value
        FROM (
            SELECT COALESCE(NULLIF(CAST({_quote_identifier(column)} AS VARCHAR), ''), 'event') AS value
            FROM raw_in
        )
        ORDER BY value
        """
    ).fetchall()
    return [str(row[0]) for row in rows]


def _event_label_frame(event_values: list[str], config: LanguageConfig) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for raw_value in event_values:
        classification = classify_event(raw_value, config)
        rows.append(
            {
                "event_raw": raw_value,
                "event_label": classification["label"],
                "event_type": classification["event_type"],
                "event_group": classification["group"],
                "language_source": classification["source"],
            }
        )
    return pd.DataFrame(
        rows,
        columns=["event_raw", "event_label", "event_type", "event_group", "language_source"],
    )


def _content_label_frame(config: LanguageConfig) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "content_id_key": str(content_id),
                "configured_content_label": values.get("label", str(content_id)),
                "configured_content_type": values.get("type"),
            }
            for content_id, values in config.content_labels.items()
        ],
        columns=["content_id_key", "configured_content_label", "configured_content_type"],
    )


def _product_label_frame(product_values: list[str], config: LanguageConfig) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for product_id in product_values:
        label, category = label_product(product_id, config)
        rows.append(
            {
                "product_id_key": product_id,
                "product_label_value": label,
                "product_category_value": category,
            }
        )
    return pd.DataFrame(
        rows,
        columns=["product_id_key", "product_label_value", "product_category_value"],
    )


def _require_fields(fields: FieldMapping, path: Path) -> None:
    missing = [
        name
        for name in ("uid", "timestamp", "event")
        if getattr(fields, name) is None
    ]
    if missing:
        names = ", ".join(missing)
        raise DuckDBNormalizeUnsupported(f"{path.name} is missing required fields for DuckDB normalize: {names}")


def _normalize_file(path: Path, config: LanguageConfig) -> tuple[RawTableInfo, pd.DataFrame, dict[str, Any]]:
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"CREATE TEMP VIEW raw_in AS SELECT * FROM {_csv_relation(path)}")
        columns = _describe_columns(con)
        row_count = int(con.execute("SELECT COUNT(*) FROM raw_in").fetchone()[0])
        fields = infer_fields(_empty_frame(columns), config)
        _require_fields(fields, path)
        uid_value_expr = _text_expr(fields.uid)
        missing_uid_rows = int(
            con.execute(
                f"""
                SELECT COUNT(*)::BIGINT
                FROM raw_in
                WHERE {uid_value_expr} IS NULL
                """
            ).fetchone()[0]
        )

        event_values = _distinct_event_values(con, fields.event or "")
        product_values = _distinct_text_values(con, fields.product_id)

        con.register("event_labels_in", _event_label_frame(event_values, config))
        con.register("content_labels_in", _content_label_frame(config))
        con.register("product_labels_in", _product_label_frame(product_values, config))

        uid_expr = _text_expr(fields.uid, "''", blank_to_null=False)
        timestamp_expr = _text_expr(fields.timestamp)
        event_expr = f"COALESCE({_text_expr(fields.event)}, 'event')"
        content_expr = _text_expr(fields.content_id)
        product_expr = _text_expr(fields.product_id)
        result_expr = _text_expr(fields.result)

        normalized = con.execute(
            f"""
            WITH normalized_raw AS (
                SELECT
                    COALESCE({uid_expr}, '') AS uid,
                    TRY_CAST({timestamp_expr} AS TIMESTAMP) AS ts,
                    {event_expr} AS event_raw,
                    {content_expr} AS content_id,
                    {product_expr} AS product_id,
                    {_number_expr(fields.amount)} AS amount,
                    {_number_expr(fields.duration_sec)} AS duration_sec,
                    {_number_expr(fields.wait_time_sec)} AS wait_time_sec,
                    {result_expr} AS result
                FROM raw_in
            ),
            classified AS (
                SELECT
                    normalized_raw.*,
                    COALESCE(event_labels_in.event_label, normalized_raw.event_raw) AS event_label,
                    COALESCE(event_labels_in.event_type, 'event') AS event_type,
                    CASE
                        WHEN COALESCE(event_labels_in.language_source, 'inferred') = 'dictionary'
                        THEN event_labels_in.event_group
                        ELSE COALESCE(event_labels_in.event_group, normalized_raw.content_id)
                    END AS "group",
                    COALESCE(event_labels_in.language_source, 'inferred') AS language_source
                FROM normalized_raw
                LEFT JOIN event_labels_in USING (event_raw)
            )
            SELECT
                classified.uid,
                classified.ts,
                classified.event_raw,
                classified.event_label,
                classified.event_type,
                classified."group",
                classified.content_id,
                CASE
                    WHEN classified.content_id IS NULL THEN classified."group"
                    WHEN content_labels_in.configured_content_label IS NOT NULL
                    THEN content_labels_in.configured_content_label
                    ELSE COALESCE(classified."group", classified.content_id)
                END AS content_label,
                content_labels_in.configured_content_type AS content_type,
                classified.product_id,
                product_labels_in.product_label_value AS product_label,
                product_labels_in.product_category_value AS product_category,
                classified.amount,
                classified.duration_sec,
                classified.wait_time_sec,
                classified.result,
                {_sql_literal(path.name)} AS source_file,
                classified.language_source
            FROM classified
            LEFT JOIN content_labels_in
              ON content_labels_in.content_id_key = classified.content_id
            LEFT JOIN product_labels_in
              ON product_labels_in.product_id_key = classified.product_id
            """
        ).fetchdf()
    finally:
        con.close()

    info = RawTableInfo(
        source_file=path.name,
        row_count=row_count,
        columns=columns,
    )
    report = {
        "source_file": path.name,
        "rows": row_count,
        "fields": fields.to_dict(),
        "normalize_engine": "duckdb",
        "missing_uid_rows": missing_uid_rows,
    }
    return info, normalized[STANDARD_COLUMNS], report


def normalize_csv_with_duckdb(
    inputs: list[str | Path],
    config: LanguageConfig,
) -> tuple[list[RawTableInfo], pd.DataFrame, list[dict[str, Any]]]:
    files = discover_files(inputs)
    unsupported = [path for path in files if path.suffix.lower() not in CSV_SUFFIXES]
    if unsupported:
        names = ", ".join(path.name for path in unsupported[:3])
        raise DuckDBNormalizeUnsupported(f"DuckDB normalize only handles CSV/TSV/TXT files in this path: {names}")

    raw_tables: list[RawTableInfo] = []
    normalized_frames: list[pd.DataFrame] = []
    field_reports: list[dict[str, Any]] = []
    for path in files:
        info, normalized, report = _normalize_file(path, config)
        raw_tables.append(info)
        normalized_frames.append(normalized)
        field_reports.append(report)

    events = pd.concat(normalized_frames, ignore_index=True) if normalized_frames else pd.DataFrame(columns=STANDARD_COLUMNS)
    events = events[events["uid"].notna() & (events["uid"].astype(str) != "")]
    events = events.sort_values(["uid", "ts"], na_position="last").reset_index(drop=True)
    return raw_tables, events, field_reports
