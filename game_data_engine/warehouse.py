from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
import json

import duckdb
import pandas as pd

from .ingest import RawTable, describe_raw_table


TABLE_COLUMNS = {
    "raw.files": [
        "run_id",
        "source_file",
        "source_sheet",
        "row_count",
        "columns_json",
        "stored_at",
    ],
    "mart.normalized_events": [
        "run_id",
        "event_index",
        "uid",
        "event_ts",
        "event_raw",
        "event_label",
        "event_type",
        "content_group",
        "content_id",
        "content_label",
        "content_type",
        "product_id",
        "product_label",
        "product_category",
        "amount",
        "duration_sec",
        "wait_time_sec",
        "result",
        "source_file",
        "language_source",
        "session_index",
        "session_id",
        "stored_at",
    ],
    "mart.session_facts": [
        "run_id",
        "session_id",
        "uid",
        "start_ts",
        "end_ts",
        "duration_sec",
        "event_count",
        "first_event",
        "last_event",
        "last_event_type",
        "content_groups_json",
        "purchase_count",
        "revenue",
        "ended_after_failure",
        "stored_at",
    ],
    "mart.user_journeys": [
        "run_id",
        "uid",
        "first_seen",
        "last_seen",
        "event_count",
        "session_count",
        "first_event",
        "first_content",
        "first_failure",
        "first_failure_group",
        "first_purchase_product",
        "purchase_count",
        "revenue",
        "last_event",
        "last_group",
        "last_event_type",
        "stored_at",
    ],
    "mart.content_facts": [
        "run_id",
        "content_group",
        "participant_users",
        "participant_rate",
        "event_count",
        "avg_duration_sec",
        "success_events",
        "fail_events",
        "failure_rate",
        "reward_claim_rate",
        "avg_wait_sec",
        "retry_after_failure_rate",
        "revenue_after_content",
        "stored_at",
    ],
    "mart.product_facts": [
        "run_id",
        "product",
        "buyers",
        "purchase_count",
        "revenue",
        "avg_amount",
        "top_context_groups_json",
        "stored_at",
    ],
    "mart.run_summaries": [
        "run_id",
        "active_users",
        "events",
        "sessions",
        "avg_session_duration_sec",
        "revenue",
        "paying_users",
        "conversion_rate",
        "arppu",
        "quality_score",
        "normalized_rows",
        "missing_uid_rows",
        "missing_timestamp_rows",
        "duplicate_event_rows",
        "inferred_language_rows",
        "source_files_json",
        "stored_at",
    ],
    "mart.daily_summaries": [
        "run_id",
        "event_date",
        "active_users",
        "events",
        "sessions",
        "avg_session_duration_sec",
        "revenue",
        "paying_users",
        "conversion_rate",
        "arppu",
        "stored_at",
    ],
    "mart.run_artifacts": [
        "run_id",
        "artifact_type",
        "payload_json",
        "stored_at",
    ],
}
RUN_TABLES = tuple(TABLE_COLUMNS)


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _json_text(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False)


def _quote_table(name: str) -> str:
    return ".".join(f'"{part}"' for part in name.split("."))


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_columns(columns: list[str]) -> str:
    return ", ".join(f'"{column}"' for column in columns)


def _insert_frame(con: duckdb.DuckDBPyConnection, table: str, frame: pd.DataFrame, columns: list[str]) -> int:
    prepared = frame.reindex(columns=columns)
    if prepared.empty:
        return 0
    con.register("_warehouse_frame", prepared)
    try:
        quoted_columns = _quote_columns(columns)
        con.execute(
            f"INSERT INTO {_quote_table(table)} ({quoted_columns}) "
            f"SELECT {quoted_columns} FROM _warehouse_frame"
        )
    finally:
        con.unregister("_warehouse_frame")
    return int(len(prepared))


def _count_run_rows(con: duckdb.DuckDBPyConnection, table: str, run_id: str) -> int:
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM {_quote_table(table)} WHERE run_id = ?", [run_id]).fetchone()[0])
    except duckdb.CatalogException:
        return 0


def _row_dict(cursor: duckdb.DuckDBPyConnection, row: tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    columns = [description[0] for description in cursor.description]
    return dict(zip(columns, row))


def compute_sql_summary(con: duckdb.DuckDBPyConnection, run_id: str) -> dict[str, Any]:
    cursor = con.execute(
        """
        WITH event_summary AS (
            SELECT
                COUNT(*)::BIGINT AS events,
                COUNT(DISTINCT uid)::BIGINT AS active_users,
                COUNT(DISTINCT CASE WHEN event_type = 'purchase' THEN uid END)::BIGINT AS paying_users,
                COALESCE(SUM(CASE WHEN event_type = 'purchase' THEN amount ELSE 0 END), 0)::DOUBLE AS revenue,
                COUNT(*)::BIGINT AS normalized_rows
            FROM mart.normalized_events
            WHERE run_id = ?
        ),
        session_summary AS (
            SELECT
                COUNT(*)::BIGINT AS sessions,
                COALESCE(AVG(duration_sec), 0)::DOUBLE AS avg_session_duration_sec
            FROM mart.session_facts
            WHERE run_id = ?
        )
        SELECT
            event_summary.active_users,
            event_summary.events,
            session_summary.sessions,
            ROUND(session_summary.avg_session_duration_sec, 2) AS avg_session_duration_sec,
            ROUND(event_summary.revenue, 2) AS revenue,
            event_summary.paying_users,
            ROUND(
                CASE
                    WHEN event_summary.active_users = 0 THEN 0
                    ELSE event_summary.paying_users::DOUBLE / event_summary.active_users
                END,
                4
            ) AS conversion_rate,
            ROUND(
                CASE
                    WHEN event_summary.paying_users = 0 THEN 0
                    ELSE event_summary.revenue / event_summary.paying_users
                END,
                2
            ) AS arppu,
            event_summary.normalized_rows
        FROM event_summary
        CROSS JOIN session_summary
        """,
        [run_id, run_id],
    )
    summary = _row_dict(cursor, cursor.fetchone()) or {}
    return _jsonable(summary)


def _stored_summary(con: duckdb.DuckDBPyConnection, run_id: str) -> dict[str, Any] | None:
    cursor = con.execute(
        """
        SELECT
            run_id,
            active_users,
            events,
            sessions,
            avg_session_duration_sec,
            revenue,
            paying_users,
            conversion_rate,
            arppu,
            quality_score,
            normalized_rows,
            missing_uid_rows,
            missing_timestamp_rows,
            duplicate_event_rows,
            inferred_language_rows,
            stored_at
        FROM mart.run_summaries
        WHERE run_id = ?
        ORDER BY stored_at DESC
        LIMIT 1
        """,
        [run_id],
    )
    return _jsonable(_row_dict(cursor, cursor.fetchone()))


def compare_summaries(stored: dict[str, Any] | None, computed: dict[str, Any]) -> dict[str, Any]:
    if not stored:
        return {"status": "missing", "differences": {}}
    tolerances = {
        "avg_session_duration_sec": 0.01,
        "revenue": 0.01,
        "conversion_rate": 0.0001,
        "arppu": 0.01,
    }
    keys = [
        "active_users",
        "events",
        "sessions",
        "avg_session_duration_sec",
        "revenue",
        "paying_users",
        "conversion_rate",
        "arppu",
        "normalized_rows",
    ]
    differences = {}
    for key in keys:
        stored_value = stored.get(key)
        computed_value = computed.get(key)
        tolerance = tolerances.get(key, 0)
        if tolerance:
            matched = abs(float(stored_value or 0) - float(computed_value or 0)) <= tolerance
        else:
            matched = stored_value == computed_value
        if not matched:
            differences[key] = {"stored": stored_value, "computed": computed_value}
    return {
        "status": "match" if not differences else "mismatch",
        "differences": differences,
    }


def _raw_files_frame(raw_frames: list[RawTable], run_id: str, stored_at: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for frame in raw_frames:
        info = describe_raw_table(frame)
        rows.append(
            {
                "run_id": run_id,
                "source_file": info.source_file,
                "source_sheet": info.source_sheet,
                "row_count": info.row_count,
                "columns_json": _json_text(info.columns),
                "stored_at": stored_at,
            }
        )
    return pd.DataFrame(rows)


def _as_text_column(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series([None] * len(frame), index=frame.index)
    return frame[column].map(lambda value: None if pd.isna(value) else str(value))


def _normalized_frame(events: pd.DataFrame, run_id: str, stored_at: pd.Timestamp) -> pd.DataFrame:
    frame = events.copy()
    if "group" in frame.columns:
        frame = frame.rename(columns={"group": "content_group"})
    if "ts" in frame.columns:
        frame = frame.rename(columns={"ts": "event_ts"})
    frame.insert(0, "event_index", range(len(frame)))
    frame.insert(0, "run_id", run_id)
    frame["stored_at"] = stored_at

    for column in [
        "uid",
        "event_raw",
        "event_label",
        "event_type",
        "content_group",
        "content_id",
        "content_label",
        "content_type",
        "product_id",
        "product_label",
        "product_category",
        "result",
        "source_file",
        "language_source",
        "session_id",
    ]:
        frame[column] = _as_text_column(frame, column)
    return frame


def _session_frame(sessions: pd.DataFrame, run_id: str, stored_at: pd.Timestamp) -> pd.DataFrame:
    frame = sessions.copy()
    if "start" in frame.columns:
        frame = frame.rename(columns={"start": "start_ts"})
    if "end" in frame.columns:
        frame = frame.rename(columns={"end": "end_ts"})
    frame.insert(0, "run_id", run_id)
    frame["content_groups_json"] = frame.get("content_groups", pd.Series([], dtype=object)).map(_json_text)
    frame["stored_at"] = stored_at
    return frame


def _journey_frame(journeys: pd.DataFrame, run_id: str, stored_at: pd.Timestamp) -> pd.DataFrame:
    frame = journeys.copy()
    frame.insert(0, "run_id", run_id)
    frame["stored_at"] = stored_at
    return frame


def _content_frame(content: pd.DataFrame, run_id: str, stored_at: pd.Timestamp) -> pd.DataFrame:
    frame = content.copy()
    if "group" in frame.columns:
        frame = frame.rename(columns={"group": "content_group"})
    frame.insert(0, "run_id", run_id)
    frame["stored_at"] = stored_at
    return frame


def _product_frame(products: pd.DataFrame, run_id: str, stored_at: pd.Timestamp) -> pd.DataFrame:
    frame = products.copy()
    frame.insert(0, "run_id", run_id)
    frame["top_context_groups_json"] = frame.get("top_context_groups", pd.Series([], dtype=object)).map(_json_text)
    frame["stored_at"] = stored_at
    return frame


def _summary_frame(
    run_id: str,
    summary: dict[str, Any],
    data_quality: dict[str, Any],
    stored_at: pd.Timestamp,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "run_id": run_id,
                "active_users": int(summary.get("active_users", 0)),
                "events": int(summary.get("events", 0)),
                "sessions": int(summary.get("sessions", 0)),
                "avg_session_duration_sec": float(summary.get("avg_session_duration_sec", 0)),
                "revenue": float(summary.get("revenue", 0)),
                "paying_users": int(summary.get("paying_users", 0)),
                "conversion_rate": float(summary.get("conversion_rate", 0)),
                "arppu": float(summary.get("arppu", 0)),
                "quality_score": float(data_quality.get("quality_score", 0)),
                "normalized_rows": int(data_quality.get("normalized_rows", 0)),
                "missing_uid_rows": int(data_quality.get("missing_uid_rows", 0)),
                "missing_timestamp_rows": int(data_quality.get("missing_timestamp_rows", 0)),
                "duplicate_event_rows": int(data_quality.get("duplicate_event_rows", 0)),
                "inferred_language_rows": int(data_quality.get("inferred_language_rows", 0)),
                "source_files_json": _json_text(data_quality.get("raw_files", [])),
                "stored_at": stored_at,
            }
        ]
    )


def _daily_summary_frame(run_id: str, summary_by_date: dict[str, Any], stored_at: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for item in summary_by_date.get("dates", []):
        rows.append(
            {
                "run_id": run_id,
                "event_date": item.get("date"),
                "active_users": int(item.get("active_users", 0)),
                "events": int(item.get("events", 0)),
                "sessions": int(item.get("sessions", 0)),
                "avg_session_duration_sec": float(item.get("avg_session_duration_sec", 0)),
                "revenue": float(item.get("revenue", 0)),
                "paying_users": int(item.get("paying_users", 0)),
                "conversion_rate": float(item.get("conversion_rate", 0)),
                "arppu": float(item.get("arppu", 0)),
                "stored_at": stored_at,
            }
        )
    return pd.DataFrame(rows, columns=TABLE_COLUMNS["mart.daily_summaries"])


def _artifact_frame(run_id: str, artifacts: dict[str, Any], stored_at: pd.Timestamp) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "run_id": run_id,
                "artifact_type": artifact_type,
                "payload_json": _json_text(payload),
                "stored_at": stored_at,
            }
            for artifact_type, payload in artifacts.items()
        ]
    )


def initialize_warehouse(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute("CREATE SCHEMA IF NOT EXISTS mart")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS raw.files (
            run_id VARCHAR,
            source_file VARCHAR,
            source_sheet VARCHAR,
            row_count BIGINT,
            columns_json VARCHAR,
            stored_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS mart.normalized_events (
            run_id VARCHAR,
            event_index BIGINT,
            uid VARCHAR,
            event_ts TIMESTAMP,
            event_raw VARCHAR,
            event_label VARCHAR,
            event_type VARCHAR,
            content_group VARCHAR,
            content_id VARCHAR,
            content_label VARCHAR,
            content_type VARCHAR,
            product_id VARCHAR,
            product_label VARCHAR,
            product_category VARCHAR,
            amount DOUBLE,
            duration_sec DOUBLE,
            wait_time_sec DOUBLE,
            result VARCHAR,
            source_file VARCHAR,
            language_source VARCHAR,
            session_index BIGINT,
            session_id VARCHAR,
            stored_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS mart.session_facts (
            run_id VARCHAR,
            session_id VARCHAR,
            uid VARCHAR,
            start_ts TIMESTAMP,
            end_ts TIMESTAMP,
            duration_sec BIGINT,
            event_count BIGINT,
            first_event VARCHAR,
            last_event VARCHAR,
            last_event_type VARCHAR,
            content_groups_json VARCHAR,
            purchase_count BIGINT,
            revenue DOUBLE,
            ended_after_failure BOOLEAN,
            stored_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS mart.user_journeys (
            run_id VARCHAR,
            uid VARCHAR,
            first_seen TIMESTAMP,
            last_seen TIMESTAMP,
            event_count BIGINT,
            session_count BIGINT,
            first_event VARCHAR,
            first_content VARCHAR,
            first_failure VARCHAR,
            first_failure_group VARCHAR,
            first_purchase_product VARCHAR,
            purchase_count BIGINT,
            revenue DOUBLE,
            last_event VARCHAR,
            last_group VARCHAR,
            last_event_type VARCHAR,
            stored_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS mart.content_facts (
            run_id VARCHAR,
            content_group VARCHAR,
            participant_users BIGINT,
            participant_rate DOUBLE,
            event_count BIGINT,
            avg_duration_sec DOUBLE,
            success_events BIGINT,
            fail_events BIGINT,
            failure_rate DOUBLE,
            reward_claim_rate DOUBLE,
            avg_wait_sec DOUBLE,
            retry_after_failure_rate DOUBLE,
            revenue_after_content DOUBLE,
            stored_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS mart.product_facts (
            run_id VARCHAR,
            product VARCHAR,
            buyers BIGINT,
            purchase_count BIGINT,
            revenue DOUBLE,
            avg_amount DOUBLE,
            top_context_groups_json VARCHAR,
            stored_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS mart.run_summaries (
            run_id VARCHAR,
            active_users BIGINT,
            events BIGINT,
            sessions BIGINT,
            avg_session_duration_sec DOUBLE,
            revenue DOUBLE,
            paying_users BIGINT,
            conversion_rate DOUBLE,
            arppu DOUBLE,
            quality_score DOUBLE,
            normalized_rows BIGINT,
            missing_uid_rows BIGINT,
            missing_timestamp_rows BIGINT,
            duplicate_event_rows BIGINT,
            inferred_language_rows BIGINT,
            source_files_json VARCHAR,
            stored_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS mart.daily_summaries (
            run_id VARCHAR,
            event_date DATE,
            active_users BIGINT,
            events BIGINT,
            sessions BIGINT,
            avg_session_duration_sec DOUBLE,
            revenue DOUBLE,
            paying_users BIGINT,
            conversion_rate DOUBLE,
            arppu DOUBLE,
            stored_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS mart.run_artifacts (
            run_id VARCHAR,
            artifact_type VARCHAR,
            payload_json VARCHAR,
            stored_at TIMESTAMP
        )
        """
    )


def store_run(
    warehouse_path: str | Path,
    run_id: str,
    raw_frames: list[RawTable],
    events: pd.DataFrame,
    sessions: pd.DataFrame,
    journeys: pd.DataFrame,
    content: pd.DataFrame,
    products: pd.DataFrame,
    summary: dict[str, Any],
    summary_by_date: dict[str, Any],
    data_quality: dict[str, Any],
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    path = Path(warehouse_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stored_at = pd.Timestamp.now()
    frames = {
        "raw.files": _raw_files_frame(raw_frames, run_id, stored_at),
        "mart.normalized_events": _normalized_frame(events, run_id, stored_at),
        "mart.session_facts": _session_frame(sessions, run_id, stored_at),
        "mart.user_journeys": _journey_frame(journeys, run_id, stored_at),
        "mart.content_facts": _content_frame(content, run_id, stored_at),
        "mart.product_facts": _product_frame(products, run_id, stored_at),
        "mart.run_summaries": _summary_frame(run_id, summary, data_quality, stored_at),
        "mart.daily_summaries": _daily_summary_frame(run_id, summary_by_date, stored_at),
        "mart.run_artifacts": _artifact_frame(run_id, artifacts, stored_at),
    }

    con = duckdb.connect(str(path))
    try:
        initialize_warehouse(con)
        con.execute("BEGIN TRANSACTION")
        try:
            for table in RUN_TABLES:
                con.execute(f"DELETE FROM {_quote_table(table)} WHERE run_id = ?", [run_id])
            table_rows = {
                table: _insert_frame(con, table, frame, TABLE_COLUMNS[table])
                for table, frame in frames.items()
            }
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        table_counts = {
            table: _count_run_rows(con, table, run_id)
            for table in RUN_TABLES
        }
        sql_summary = compute_sql_summary(con, run_id)
        summary_validation = compare_summaries(_stored_summary(con, run_id), sql_summary)
    finally:
        con.close()

    return {
        "path": str(path),
        "run_id": run_id,
        "inserted_rows": table_rows,
        "table_counts": table_counts,
        "sql_summary": sql_summary,
        "summary_validation": summary_validation,
    }


def store_staged_run(
    warehouse_path: str | Path,
    run_id: str,
    staged_db_path: str | Path,
    raw_frames: list[RawTable],
    products: pd.DataFrame,
    summary: dict[str, Any],
    summary_by_date: dict[str, Any],
    data_quality: dict[str, Any],
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    path = Path(warehouse_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stored_at = pd.Timestamp.now()

    con = duckdb.connect(str(path))
    try:
        initialize_warehouse(con)
        con.execute(f"ATTACH {_sql_literal(str(Path(staged_db_path)))} AS staged_db (READ_ONLY)")
        con.execute("BEGIN TRANSACTION")
        try:
            for table in RUN_TABLES:
                con.execute(f"DELETE FROM {_quote_table(table)} WHERE run_id = ?", [run_id])

            _insert_frame(con, "raw.files", _raw_files_frame(raw_frames, run_id, stored_at), TABLE_COLUMNS["raw.files"])
            con.execute(
                """
                INSERT INTO mart.normalized_events (
                    run_id,
                    event_index,
                    uid,
                    event_ts,
                    event_raw,
                    event_label,
                    event_type,
                    content_group,
                    content_id,
                    content_label,
                    content_type,
                    product_id,
                    product_label,
                    product_category,
                    amount,
                    duration_sec,
                    wait_time_sec,
                    result,
                    source_file,
                    language_source,
                    session_index,
                    session_id,
                    stored_at
                )
                SELECT
                    ?,
                    event_order,
                    uid,
                    ts,
                    event_raw,
                    event_label,
                    event_type,
                    "group",
                    content_id,
                    content_label,
                    content_type,
                    product_id,
                    product_label,
                    product_category,
                    amount,
                    duration_sec,
                    wait_time_sec,
                    result,
                    source_file,
                    language_source,
                    session_index,
                    session_id,
                    ?
                FROM staged_db.events_ordered
                ORDER BY event_order
                """,
                [run_id, stored_at],
            )
            con.execute(
                """
                INSERT INTO mart.session_facts (
                    run_id,
                    session_id,
                    uid,
                    start_ts,
                    end_ts,
                    duration_sec,
                    event_count,
                    first_event,
                    last_event,
                    last_event_type,
                    content_groups_json,
                    purchase_count,
                    revenue,
                    ended_after_failure,
                    stored_at
                )
                SELECT
                    ?,
                    session_id,
                    uid,
                    start,
                    "end",
                    duration_sec,
                    event_count,
                    first_event,
                    last_event,
                    last_event_type,
                    CAST(to_json(content_groups) AS VARCHAR),
                    purchase_count,
                    revenue,
                    ended_after_failure,
                    ?
                FROM staged_db.session_facts
                ORDER BY session_id
                """,
                [run_id, stored_at],
            )
            con.execute(
                """
                INSERT INTO mart.user_journeys (
                    run_id,
                    uid,
                    first_seen,
                    last_seen,
                    event_count,
                    session_count,
                    first_event,
                    first_content,
                    first_failure,
                    first_failure_group,
                    first_purchase_product,
                    purchase_count,
                    revenue,
                    last_event,
                    last_group,
                    last_event_type,
                    stored_at
                )
                SELECT
                    ?,
                    uid,
                    first_seen,
                    last_seen,
                    event_count,
                    session_count,
                    first_event,
                    first_content,
                    first_failure,
                    first_failure_group,
                    first_purchase_product,
                    purchase_count,
                    revenue,
                    last_event,
                    last_group,
                    last_event_type,
                    ?
                FROM staged_db.user_journeys
                ORDER BY uid
                """,
                [run_id, stored_at],
            )
            con.execute(
                """
                INSERT INTO mart.content_facts (
                    run_id,
                    content_group,
                    participant_users,
                    participant_rate,
                    event_count,
                    avg_duration_sec,
                    success_events,
                    fail_events,
                    failure_rate,
                    reward_claim_rate,
                    avg_wait_sec,
                    retry_after_failure_rate,
                    revenue_after_content,
                    stored_at
                )
                SELECT
                    ?,
                    "group",
                    participant_users,
                    participant_rate,
                    event_count,
                    avg_duration_sec,
                    success_events,
                    fail_events,
                    failure_rate,
                    reward_claim_rate,
                    avg_wait_sec,
                    retry_after_failure_rate,
                    revenue_after_content,
                    ?
                FROM staged_db.content_facts
                ORDER BY participant_rate ASC, revenue_after_content DESC
                """,
                [run_id, stored_at],
            )
            _insert_frame(
                con,
                "mart.product_facts",
                _product_frame(products, run_id, stored_at),
                TABLE_COLUMNS["mart.product_facts"],
            )
            _insert_frame(
                con,
                "mart.run_summaries",
                _summary_frame(run_id, summary, data_quality, stored_at),
                TABLE_COLUMNS["mart.run_summaries"],
            )
            _insert_frame(
                con,
                "mart.daily_summaries",
                _daily_summary_frame(run_id, summary_by_date, stored_at),
                TABLE_COLUMNS["mart.daily_summaries"],
            )
            _insert_frame(
                con,
                "mart.run_artifacts",
                _artifact_frame(run_id, artifacts, stored_at),
                TABLE_COLUMNS["mart.run_artifacts"],
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        table_counts = {
            table: _count_run_rows(con, table, run_id)
            for table in RUN_TABLES
        }
        sql_summary = compute_sql_summary(con, run_id)
        summary_validation = compare_summaries(_stored_summary(con, run_id), sql_summary)
    finally:
        con.close()

    return {
        "path": str(path),
        "run_id": run_id,
        "inserted_rows": table_counts,
        "table_counts": table_counts,
        "sql_summary": sql_summary,
        "summary_validation": summary_validation,
    }


def fetch_run_snapshot(warehouse_path: str | Path, run_id: str) -> dict[str, Any]:
    path = Path(warehouse_path)
    if not path.exists():
        return {"status": "empty", "path": str(path), "run_id": run_id}

    con = duckdb.connect(str(path), read_only=True)
    try:
        table_counts = {table: _count_run_rows(con, table, run_id) for table in RUN_TABLES}
        summary = _stored_summary(con, run_id)
        sql_summary = compute_sql_summary(con, run_id)
        summary_validation = compare_summaries(summary, sql_summary)
        source_files = con.execute(
            """
            SELECT source_file, source_sheet, row_count
            FROM raw.files
            WHERE run_id = ?
            ORDER BY source_file, source_sheet
            """,
            [run_id],
        ).fetchall()
        artifact_types = [
            row[0]
            for row in con.execute(
                """
                SELECT artifact_type
                FROM mart.run_artifacts
                WHERE run_id = ?
                ORDER BY artifact_type
                """,
                [run_id],
            ).fetchall()
        ]
        try:
            daily_rows = con.execute(
                """
                SELECT
                    event_date,
                    active_users,
                    events,
                    sessions,
                    avg_session_duration_sec,
                    revenue,
                    paying_users,
                    conversion_rate,
                    arppu
                FROM mart.daily_summaries
                WHERE run_id = ?
                ORDER BY event_date
                """,
                [run_id],
            ).fetchall()
        except duckdb.CatalogException:
            daily_rows = []
    finally:
        con.close()

    return _jsonable(
        {
            "status": "found" if summary else "missing",
            "path": str(path),
            "run_id": run_id,
            "table_counts": table_counts,
            "summary": summary,
            "summary_by_date": {
                "date_count": len(daily_rows),
                "dates": [
                    {
                        "date": event_date,
                        "active_users": active_users,
                        "events": events,
                        "sessions": sessions,
                        "avg_session_duration_sec": avg_session_duration_sec,
                        "revenue": revenue,
                        "paying_users": paying_users,
                        "conversion_rate": conversion_rate,
                        "arppu": arppu,
                    }
                    for (
                        event_date,
                        active_users,
                        events,
                        sessions,
                        avg_session_duration_sec,
                        revenue,
                        paying_users,
                        conversion_rate,
                        arppu,
                    ) in daily_rows
                ],
            },
            "sql_summary": sql_summary,
            "summary_validation": summary_validation,
            "source_files": [
                {"source_file": source_file, "source_sheet": source_sheet, "row_count": row_count}
                for source_file, source_sheet, row_count in source_files
            ],
            "artifact_types": artifact_types,
        }
    )
