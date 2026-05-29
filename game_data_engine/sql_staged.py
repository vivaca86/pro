from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from .config import LanguageConfig
from .ingest import RawTableInfo, discover_files
from .language import infer_fields
from .sql_normalize import (
    CSV_SUFFIXES,
    DuckDBNormalizeUnsupported,
    _content_label_frame,
    _csv_relation,
    _describe_columns,
    _distinct_event_values,
    _distinct_text_values,
    _empty_frame,
    _event_label_frame,
    _number_expr,
    _product_label_frame,
    _quote_identifier,
    _require_fields,
    _sql_literal,
    _text_expr,
)


@dataclass
class StagedAnalysis:
    db_path: Path
    raw_tables: list[RawTableInfo]
    field_reports: list[dict[str, Any]]
    summary: dict[str, Any]
    summary_by_date: dict[str, Any]
    data_quality: dict[str, Any]
    language_suggestions: list[dict[str, Any]]
    sessions: dict[str, Any]
    journeys: dict[str, Any]
    failures: pd.DataFrame
    purchases: dict[str, Any]
    content: pd.DataFrame
    products: pd.DataFrame
    segments: dict[str, Any]
    concentration: dict[str, Any]


def _list_value(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if hasattr(value, "tolist"):
        return value.tolist()
    try:
        if pd.isna(value):
            return []
    except (TypeError, ValueError):
        pass
    return [value]


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
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


def _create_base_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE normalized_base (
            _source_order BIGINT,
            uid VARCHAR,
            ts TIMESTAMP,
            event_raw VARCHAR,
            event_label VARCHAR,
            event_type VARCHAR,
            "group" VARCHAR,
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
            language_source VARCHAR
        )
        """
    )


def _unregister(con: duckdb.DuckDBPyConnection, name: str) -> None:
    try:
        con.unregister(name)
    except duckdb.CatalogException:
        pass


def _register_frame(con: duckdb.DuckDBPyConnection, name: str, frame: pd.DataFrame) -> None:
    _unregister(con, name)
    con.register(name, frame)


def _insert_file(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    config: LanguageConfig,
    source_offset: int,
) -> tuple[RawTableInfo, dict[str, Any], int]:
    con.execute("DROP VIEW IF EXISTS raw_in")
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
    _register_frame(con, "event_labels_in", _event_label_frame(event_values, config))
    _register_frame(con, "content_labels_in", _content_label_frame(config))
    _register_frame(con, "product_labels_in", _product_label_frame(product_values, config))

    uid_expr = _text_expr(fields.uid, "''", blank_to_null=False)
    timestamp_expr = _text_expr(fields.timestamp)
    event_expr = f"COALESCE({_text_expr(fields.event)}, 'event')"
    content_expr = _text_expr(fields.content_id)
    product_expr = _text_expr(fields.product_id)
    result_expr = _text_expr(fields.result)

    con.execute(
        f"""
        INSERT INTO normalized_base
        WITH raw_numbered AS (
            SELECT
                ROW_NUMBER() OVER () - 1 + ? AS _source_order,
                *
            FROM raw_in
        ),
        normalized_raw AS (
            SELECT
                _source_order,
                COALESCE({uid_expr}, '') AS uid,
                TRY_CAST({timestamp_expr} AS TIMESTAMP) AS ts,
                {event_expr} AS event_raw,
                {content_expr} AS content_id,
                {product_expr} AS product_id,
                {_number_expr(fields.amount)} AS amount,
                {_number_expr(fields.duration_sec)} AS duration_sec,
                {_number_expr(fields.wait_time_sec)} AS wait_time_sec,
                {result_expr} AS result
            FROM raw_numbered
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
            classified._source_order,
            classified.uid,
            classified.ts,
            classified.event_raw,
            classified.event_label,
            classified.event_type,
            CASE
                WHEN classified.language_source != 'dictionary'
                 AND content_labels_in.configured_content_label IS NOT NULL
                THEN content_labels_in.configured_content_label
                ELSE classified."group"
            END AS "group",
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
        """,
        [source_offset],
    )

    info = RawTableInfo(source_file=path.name, row_count=row_count, columns=columns)
    report = {
        "source_file": path.name,
        "rows": row_count,
        "fields": fields.to_dict(),
        "normalize_engine": "duckdb",
        "execution_engine": "duckdb_staged",
        "missing_uid_rows": missing_uid_rows,
    }
    return info, report, source_offset + row_count


def _create_events_ordered(con: duckdb.DuckDBPyConnection, session_gap_minutes: int) -> None:
    con.execute(
        """
        CREATE TABLE events_ordered AS
        WITH ordered AS (
            SELECT
                *,
                LAG(ts) OVER (
                    PARTITION BY uid
                    ORDER BY ts NULLS LAST, _source_order
                ) AS previous_ts
            FROM normalized_base
            WHERE uid IS NOT NULL AND TRIM(CAST(uid AS VARCHAR)) <> ''
        ),
        flagged AS (
            SELECT
                *,
                CASE
                    WHEN previous_ts IS NULL THEN 1
                    WHEN ts IS NULL THEN 1
                    WHEN DATE_DIFF('second', previous_ts, ts) / 60.0 > ? THEN 1
                    ELSE 0
                END AS new_session
            FROM ordered
        ),
        indexed AS (
            SELECT
                *,
                SUM(new_session) OVER (
                    PARTITION BY uid
                    ORDER BY ts NULLS LAST, _source_order
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) - 1 AS session_index
            FROM flagged
        )
        SELECT
            ROW_NUMBER() OVER (ORDER BY uid, ts NULLS LAST, _source_order) - 1 AS event_order,
            ROW_NUMBER() OVER (PARTITION BY uid ORDER BY ts NULLS LAST, _source_order) - 1 AS user_event_index,
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
            CAST(session_index AS BIGINT) AS session_index,
            uid || '-' || CAST(CAST(session_index AS BIGINT) AS VARCHAR) AS session_id
        FROM indexed
        ORDER BY uid, ts NULLS LAST, _source_order
        """,
        [session_gap_minutes],
    )


def _create_session_facts(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE session_facts AS
        WITH ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY event_order) AS first_rank,
                ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY event_order DESC) AS last_rank
            FROM events_ordered
        ),
        session_base AS (
            SELECT
                session_id,
                ANY_VALUE(uid) AS uid,
                MAX(CASE WHEN first_rank = 1 THEN ts END) AS start,
                MAX(CASE WHEN last_rank = 1 THEN ts END) AS "end",
                COUNT(*)::BIGINT AS event_count,
                MAX(CASE WHEN first_rank = 1 THEN event_label END) AS first_event,
                MAX(CASE WHEN last_rank = 1 THEN event_label END) AS last_event,
                MAX(CASE WHEN last_rank = 1 THEN event_type END) AS last_event_type,
                SUM(CASE WHEN event_type = 'purchase' THEN 1 ELSE 0 END)::BIGINT AS purchase_count,
                COALESCE(SUM(CASE WHEN event_type = 'purchase' THEN amount ELSE 0 END), 0)::DOUBLE AS revenue,
                MAX(CASE WHEN last_rank = 1 AND event_type IN ('content_fail', 'match_issue') THEN TRUE ELSE FALSE END) AS ended_after_failure
            FROM ranked
            GROUP BY session_id
        ),
        group_first_seen AS (
            SELECT
                session_id,
                "group" AS content_group,
                MIN(event_order) AS first_seen_order
            FROM events_ordered
            WHERE "group" IS NOT NULL AND CAST("group" AS VARCHAR) <> ''
            GROUP BY session_id, "group"
        ),
        session_groups AS (
            SELECT
                session_id,
                LIST(content_group ORDER BY first_seen_order) AS content_groups
            FROM group_first_seen
            GROUP BY session_id
        )
        SELECT
            session_base.session_id,
            session_base.uid,
            session_base.start,
            session_base."end",
            CASE
                WHEN session_base.start IS NULL OR session_base."end" IS NULL THEN 0
                ELSE GREATEST(0, DATE_DIFF('second', session_base.start, session_base."end"))
            END::BIGINT AS duration_sec,
            session_base.event_count,
            session_base.first_event,
            session_base.last_event,
            session_base.last_event_type,
            session_groups.content_groups,
            session_base.purchase_count,
            session_base.revenue,
            session_base.ended_after_failure
        FROM session_base
        LEFT JOIN session_groups USING (session_id)
        ORDER BY session_id
        """
    )


def _create_user_journeys(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE user_journeys AS
        WITH ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (PARTITION BY uid ORDER BY event_order) AS first_rank,
                ROW_NUMBER() OVER (PARTITION BY uid ORDER BY event_order DESC) AS last_rank
            FROM events_ordered
        ),
        user_base AS (
            SELECT
                uid,
                MAX(CASE WHEN first_rank = 1 THEN ts END) AS first_seen,
                MAX(CASE WHEN last_rank = 1 THEN ts END) AS last_seen,
                COUNT(*)::BIGINT AS event_count,
                COUNT(DISTINCT session_id)::BIGINT AS session_count,
                MAX(CASE WHEN first_rank = 1 THEN event_label END) AS first_event,
                SUM(CASE WHEN event_type = 'purchase' THEN 1 ELSE 0 END)::BIGINT AS purchase_count,
                COALESCE(SUM(CASE WHEN event_type = 'purchase' THEN amount ELSE 0 END), 0)::DOUBLE AS revenue,
                MAX(CASE WHEN last_rank = 1 THEN event_label END) AS last_event,
                MAX(CASE WHEN last_rank = 1 THEN "group" END) AS last_group,
                MAX(CASE WHEN last_rank = 1 THEN event_type END) AS last_event_type
            FROM ranked
            GROUP BY uid
        ),
        first_content AS (
            SELECT uid, content_label AS first_content
            FROM (
                SELECT
                    uid,
                    content_label,
                    ROW_NUMBER() OVER (PARTITION BY uid ORDER BY event_order) AS rank
                FROM events_ordered
                WHERE "group" IS NOT NULL
                  AND event_type IN ('content_enter', 'session_start', 'content_fail', 'match_issue')
            )
            WHERE rank = 1
        ),
        first_failure AS (
            SELECT uid, event_label AS first_failure, "group" AS first_failure_group
            FROM (
                SELECT
                    uid,
                    event_label,
                    "group",
                    ROW_NUMBER() OVER (PARTITION BY uid ORDER BY event_order) AS rank
                FROM events_ordered
                WHERE event_type IN ('content_fail', 'match_issue')
            )
            WHERE rank = 1
        ),
        first_purchase AS (
            SELECT uid, product_label AS first_purchase_product
            FROM (
                SELECT
                    uid,
                    product_label,
                    ROW_NUMBER() OVER (PARTITION BY uid ORDER BY event_order) AS rank
                FROM events_ordered
                WHERE event_type = 'purchase'
            )
            WHERE rank = 1
        )
        SELECT
            user_base.uid,
            user_base.first_seen,
            user_base.last_seen,
            user_base.event_count,
            user_base.session_count,
            user_base.first_event,
            first_content.first_content,
            first_failure.first_failure,
            first_failure.first_failure_group,
            first_purchase.first_purchase_product,
            user_base.purchase_count,
            user_base.revenue,
            user_base.last_event,
            user_base.last_group,
            user_base.last_event_type
        FROM user_base
        LEFT JOIN first_content USING (uid)
        LEFT JOIN first_failure USING (uid)
        LEFT JOIN first_purchase USING (uid)
        ORDER BY uid
        """
    )


def _create_failure_contexts(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE failure_contexts AS
        WITH failures AS (
            SELECT
                uid,
                event_order,
                COALESCE(NULLIF(CAST("group" AS VARCHAR), ''), NULLIF(CAST(content_label AS VARCHAR), ''), 'unknown') AS failure_group
            FROM events_ordered
            WHERE event_type IN ('content_fail', 'match_issue')
        ),
        retries AS (
            SELECT DISTINCT
                failures.uid,
                failures.failure_group
            FROM failures
            JOIN events_ordered later
              ON later.uid = failures.uid
             AND later.event_order > failures.event_order
             AND CAST(later."group" AS VARCHAR) = failures.failure_group
        )
        SELECT
            failures.failure_group AS "group",
            COUNT(DISTINCT failures.uid)::BIGINT AS failure_users,
            COUNT(*)::BIGINT AS failure_events,
            COUNT(DISTINCT retries.uid)::BIGINT AS retry_users,
            CASE
                WHEN COUNT(DISTINCT failures.uid) = 0 THEN 0
                ELSE COUNT(DISTINCT retries.uid)::DOUBLE / COUNT(DISTINCT failures.uid)
            END AS retry_after_failure_rate
        FROM failures
        LEFT JOIN retries
          ON retries.uid = failures.uid
         AND retries.failure_group = failures.failure_group
        GROUP BY failures.failure_group
        ORDER BY failure_events DESC, "group"
        """
    )


def _create_purchase_prior_events(
    con: duckdb.DuckDBPyConnection,
    lookback_events: int = 5,
    lookback_minutes: int = 60,
) -> None:
    con.execute(
        """
        CREATE TABLE purchase_prior_events AS
        SELECT
            purchase.event_order AS purchase_order,
            purchase.uid,
            COALESCE(NULLIF(CAST(purchase.product_label AS VARCHAR), ''), CAST(purchase.product_id AS VARCHAR)) AS product,
            purchase.amount,
            prior.event_label,
            prior."group" AS content_group,
            prior.user_event_index AS prior_index
        FROM events_ordered purchase
        JOIN events_ordered prior
          ON prior.uid = purchase.uid
         AND prior.user_event_index >= purchase.user_event_index - ?
         AND prior.user_event_index < purchase.user_event_index
         AND (
              purchase.ts IS NULL
              OR prior.ts IS NULL
              OR prior.ts >= purchase.ts - (? * INTERVAL '1 minute')
         )
        WHERE purchase.event_type = 'purchase'
        """,
        [lookback_events, lookback_minutes],
    )


def _create_content_facts(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE content_facts AS
        WITH active AS (
            SELECT COUNT(DISTINCT uid)::DOUBLE AS active_users
            FROM events_ordered
        ),
        content_base AS (
            SELECT
                "group",
                COUNT(DISTINCT uid)::BIGINT AS participant_users,
                COUNT(*)::BIGINT AS event_count,
                AVG(NULLIF(duration_sec, 0))::DOUBLE AS avg_duration_sec,
                SUM(CASE WHEN event_type = 'content_success' THEN 1 ELSE 0 END)::BIGINT AS success_events,
                SUM(CASE WHEN event_type IN ('content_fail', 'match_issue') THEN 1 ELSE 0 END)::BIGINT AS fail_events,
                COUNT(DISTINCT CASE WHEN event_type = 'reward_claim' THEN uid END)::BIGINT AS reward_users,
                COUNT(DISTINCT CASE WHEN event_type = 'content_success' THEN uid END)::BIGINT AS success_users
            FROM events_ordered
            WHERE "group" IS NOT NULL
            GROUP BY "group"
        ),
        wait_stats AS (
            SELECT
                "group",
                COUNT(*) FILTER (
                    WHERE event_type = 'match_issue' OR COALESCE(wait_time_sec, 0) > 0
                )::BIGINT AS wait_rows,
                COUNT(*) FILTER (
                    WHERE (event_type = 'match_issue' OR COALESCE(wait_time_sec, 0) > 0)
                      AND COALESCE(wait_time_sec, 0) > 0
                )::BIGINT AS positive_wait_rows,
                AVG(NULLIF(
                    CASE
                        WHEN event_type = 'match_issue' OR COALESCE(wait_time_sec, 0) > 0
                        THEN COALESCE(wait_time_sec, 0)
                        ELSE NULL
                    END,
                    0
                ))::DOUBLE AS avg_positive_wait_sec,
                AVG(NULLIF(
                    CASE
                        WHEN event_type = 'match_issue' OR COALESCE(wait_time_sec, 0) > 0
                        THEN COALESCE(duration_sec, 0)
                        ELSE NULL
                    END,
                    0
                ))::DOUBLE AS avg_wait_duration_sec
            FROM events_ordered
            WHERE "group" IS NOT NULL
            GROUP BY "group"
        ),
        first_group_seen AS (
            SELECT
                session_id,
                "group",
                MIN(event_order) AS first_seen_order
            FROM events_ordered
            WHERE "group" IS NOT NULL
              AND CAST("group" AS VARCHAR) <> ''
            GROUP BY session_id, "group"
        ),
        purchase_groups AS (
            SELECT
                purchase.event_order,
                first_group_seen."group",
                COALESCE(purchase.amount, 0)::DOUBLE AS amount
            FROM events_ordered purchase
            JOIN first_group_seen
              ON first_group_seen.session_id = purchase.session_id
             AND first_group_seen.first_seen_order <= purchase.event_order
            WHERE purchase.event_type = 'purchase'
        ),
        purchase_shares AS (
            SELECT
                "group",
                amount / COUNT(*) OVER (PARTITION BY event_order) AS share
            FROM purchase_groups
        ),
        revenue_after AS (
            SELECT
                "group",
                SUM(share)::DOUBLE AS revenue_after_content
            FROM purchase_shares
            GROUP BY "group"
        )
        SELECT
            content_base."group",
            content_base.participant_users,
            CASE
                WHEN active.active_users = 0 THEN 0
                ELSE ROUND(content_base.participant_users::DOUBLE / active.active_users, 4)
            END::DOUBLE AS participant_rate,
            content_base.event_count,
            ROUND(content_base.avg_duration_sec, 2)::DOUBLE AS avg_duration_sec,
            content_base.success_events,
            content_base.fail_events,
            CASE
                WHEN content_base.success_events + content_base.fail_events = 0 THEN 0
                ELSE ROUND(
                    content_base.fail_events::DOUBLE
                    / (content_base.success_events + content_base.fail_events),
                    4
                )
            END::DOUBLE AS failure_rate,
            CASE
                WHEN content_base.success_users = 0 THEN 0
                ELSE ROUND(content_base.reward_users::DOUBLE / content_base.success_users, 4)
            END::DOUBLE AS reward_claim_rate,
            ROUND(
                CASE
                    WHEN wait_stats.wait_rows = 0 THEN NULL
                    WHEN wait_stats.positive_wait_rows > 0 THEN wait_stats.avg_positive_wait_sec
                    ELSE wait_stats.avg_wait_duration_sec
                END,
                2
            )::DOUBLE AS avg_wait_sec,
            ROUND(COALESCE(failure_contexts.retry_after_failure_rate, 0), 4)::DOUBLE AS retry_after_failure_rate,
            ROUND(COALESCE(revenue_after.revenue_after_content, 0), 2)::DOUBLE AS revenue_after_content
        FROM content_base
        CROSS JOIN active
        LEFT JOIN wait_stats USING ("group")
        LEFT JOIN revenue_after USING ("group")
        LEFT JOIN failure_contexts USING ("group")
        ORDER BY participant_rate ASC, revenue_after_content DESC
        """
    )


def _top_pairs(con: duckdb.DuckDBPyConnection, column: str) -> list[tuple[str, int]]:
    rows = con.execute(
        f"""
        SELECT {column}, COUNT(*)::BIGINT AS count
        FROM purchase_prior_events
        WHERE {column} IS NOT NULL AND CAST({column} AS VARCHAR) <> ''
        GROUP BY {column}
        ORDER BY count DESC, {column}
        LIMIT 10
        """
    ).fetchall()
    return [(str(label), int(count)) for label, count in rows]


def _purchase_contexts(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    product_rows = con.execute(
        """
        SELECT product, content_group, COUNT(*)::BIGINT AS count
        FROM purchase_prior_events
        WHERE content_group IS NOT NULL AND CAST(content_group AS VARCHAR) <> ''
        GROUP BY product, content_group
        ORDER BY product, count DESC, content_group
        """
    ).fetchall()
    product_contexts: dict[str, list[tuple[str, int]]] = {}
    for product, group, count in product_rows:
        bucket = product_contexts.setdefault(str(product), [])
        if len(bucket) < 5:
            bucket.append((str(group), int(count)))

    examples = [
        {
            "uid": str(uid),
            "product": product,
            "amount": float(amount or 0),
            "previous_events": [] if previous_events is None else list(previous_events),
        }
        for uid, product, amount, previous_events in con.execute(
            """
            WITH purchases AS (
                SELECT
                    event_order,
                    uid,
                    COALESCE(NULLIF(CAST(product_label AS VARCHAR), ''), CAST(product_id AS VARCHAR)) AS product,
                    amount
                FROM events_ordered
                WHERE event_type = 'purchase'
                ORDER BY event_order
                LIMIT 10
            )
            SELECT
                purchases.uid,
                purchases.product,
                purchases.amount,
                LIST(purchase_prior_events.event_label ORDER BY purchase_prior_events.prior_index)
                    FILTER (WHERE purchase_prior_events.event_label IS NOT NULL) AS previous_events
            FROM purchases
            LEFT JOIN purchase_prior_events
              ON purchase_prior_events.purchase_order = purchases.event_order
            GROUP BY purchases.event_order, purchases.uid, purchases.product, purchases.amount
            ORDER BY purchases.event_order
            """
        ).fetchall()
    ]
    return {
        "top_preceding_events": _top_pairs(con, "event_label"),
        "top_preceding_groups": _top_pairs(con, "content_group"),
        "product_contexts": product_contexts,
        "examples": examples,
    }


def _product_facts(con: duckdb.DuckDBPyConnection, purchases: dict[str, Any]) -> pd.DataFrame:
    frame = con.execute(
        """
        WITH purchases AS (
            SELECT
                COALESCE(product_label, product_id) AS product,
                uid,
                COALESCE(amount, 0)::DOUBLE AS amount
            FROM events_ordered
            WHERE event_type = 'purchase'
        )
        SELECT
            product,
            COUNT(DISTINCT uid)::BIGINT AS buyers,
            COUNT(*)::BIGINT AS purchase_count,
            ROUND(SUM(amount), 2)::DOUBLE AS revenue,
            ROUND(AVG(amount), 2)::DOUBLE AS avg_amount
        FROM purchases
        WHERE product IS NOT NULL
        GROUP BY product
        ORDER BY revenue DESC
        """
    ).fetchdf()
    if frame.empty:
        return pd.DataFrame(
            columns=["product", "buyers", "purchase_count", "revenue", "avg_amount", "top_context_groups"]
        )
    contexts = purchases.get("product_contexts", {})
    frame["top_context_groups"] = frame["product"].map(lambda product: contexts.get(str(product), []))
    return frame


def _summary(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    row = con.execute(
        """
        WITH event_summary AS (
            SELECT
                COUNT(*)::BIGINT AS events,
                COUNT(DISTINCT uid)::BIGINT AS active_users,
                COUNT(DISTINCT CASE WHEN event_type = 'purchase' THEN uid END)::BIGINT AS paying_users,
                COALESCE(SUM(CASE WHEN event_type = 'purchase' THEN amount ELSE 0 END), 0)::DOUBLE AS revenue
            FROM events_ordered
        ),
        session_summary AS (
            SELECT
                COUNT(*)::BIGINT AS sessions,
                COALESCE(AVG(duration_sec), 0)::DOUBLE AS avg_session_duration_sec
            FROM session_facts
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
            ) AS arppu
        FROM event_summary
        CROSS JOIN session_summary
        """
    ).fetchone()
    columns = [
        "active_users",
        "events",
        "sessions",
        "avg_session_duration_sec",
        "revenue",
        "paying_users",
        "conversion_rate",
        "arppu",
    ]
    return _jsonable(dict(zip(columns, row or [0] * len(columns))))


def _summary_by_date(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    rows = con.execute(
        """
        WITH event_days AS (
            SELECT
                CAST(ts AS DATE) AS event_date,
                COUNT(*)::BIGINT AS events,
                COUNT(DISTINCT uid)::BIGINT AS active_users,
                COUNT(DISTINCT CASE WHEN event_type = 'purchase' THEN uid END)::BIGINT AS paying_users,
                COALESCE(SUM(CASE WHEN event_type = 'purchase' THEN amount ELSE 0 END), 0)::DOUBLE AS revenue
            FROM events_ordered
            WHERE ts IS NOT NULL
            GROUP BY CAST(ts AS DATE)
        ),
        session_days AS (
            SELECT
                CAST(start AS DATE) AS event_date,
                COUNT(*)::BIGINT AS sessions,
                COALESCE(AVG(duration_sec), 0)::DOUBLE AS avg_session_duration_sec
            FROM session_facts
            WHERE start IS NOT NULL
            GROUP BY CAST(start AS DATE)
        )
        SELECT
            CAST(event_days.event_date AS VARCHAR) AS date,
            event_days.active_users,
            event_days.events,
            COALESCE(session_days.sessions, 0)::BIGINT AS sessions,
            ROUND(COALESCE(session_days.avg_session_duration_sec, 0), 2)::DOUBLE AS avg_session_duration_sec,
            ROUND(event_days.revenue, 2)::DOUBLE AS revenue,
            event_days.paying_users,
            ROUND(
                CASE
                    WHEN event_days.active_users = 0 THEN 0
                    ELSE event_days.paying_users::DOUBLE / event_days.active_users
                END,
                4
            )::DOUBLE AS conversion_rate,
            ROUND(
                CASE
                    WHEN event_days.paying_users = 0 THEN 0
                    ELSE event_days.revenue / event_days.paying_users
                END,
                2
            )::DOUBLE AS arppu
        FROM event_days
        LEFT JOIN session_days USING (event_date)
        ORDER BY event_days.event_date
        """
    ).fetchall()
    unknown_timestamp_events = int(
        con.execute("SELECT COUNT(*)::BIGINT FROM events_ordered WHERE ts IS NULL").fetchone()[0]
    )
    columns = [
        "date",
        "active_users",
        "events",
        "sessions",
        "avg_session_duration_sec",
        "revenue",
        "paying_users",
        "conversion_rate",
        "arppu",
    ]
    date_rows = [_jsonable(dict(zip(columns, row))) for row in rows]
    return {
        "date_count": len(date_rows),
        "dates": date_rows,
        "unknown_timestamp_events": unknown_timestamp_events,
    }


def _data_quality(
    con: duckdb.DuckDBPyConnection,
    raw_tables: list[RawTableInfo],
    field_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    base_row = con.execute(
        """
        SELECT
            COUNT(*)::BIGINT AS input_rows,
            COUNT(*) FILTER (
                WHERE uid IS NULL OR TRIM(CAST(uid AS VARCHAR)) = ''
            )::BIGINT AS missing_uid_rows
        FROM normalized_base
        """
    ).fetchone()
    row = con.execute(
        """
        SELECT
            COUNT(*)::BIGINT AS normalized_rows,
            COUNT(*) FILTER (WHERE ts IS NULL)::BIGINT AS missing_timestamp_rows,
            COUNT(*) FILTER (WHERE language_source != 'dictionary')::BIGINT AS inferred_language_rows
        FROM events_ordered
        """
    ).fetchone()
    duplicate_events = int(
        con.execute(
            """
            SELECT COALESCE(SUM(count - 1), 0)::BIGINT
            FROM (
                SELECT
                    uid,
                    ts,
                    event_raw,
                    content_id,
                    product_id,
                    amount,
                    COUNT(*) AS count
                FROM events_ordered
                GROUP BY uid, ts, event_raw, content_id, product_id, amount
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
    )
    input_rows = int(base_row[0] or 0)
    normalized_rows = int(row[0] or 0)
    missing_uid = int(base_row[1] or 0)
    missing_ts = int(row[1] or 0)
    inferred_language = int(row[2] or 0)
    denominator = max(input_rows, normalized_rows, 1)
    quality_score = max(
        0,
        round(
            1
            - (
                (missing_uid / denominator) * 0.35
                + (missing_ts / denominator) * 0.25
                + (duplicate_events / denominator) * 0.2
                + (inferred_language / denominator) * 0.2
            ),
            3,
        ),
    )
    return {
        "raw_files": [
            {
                "source_file": info.source_file,
                "rows": info.row_count,
                "columns": info.columns,
            }
            for info in raw_tables
        ],
        "input_rows": input_rows,
        "normalized_rows": normalized_rows,
        "field_reports": field_reports,
        "missing_uid_rows": missing_uid,
        "missing_timestamp_rows": missing_ts,
        "duplicate_event_rows": duplicate_events,
        "inferred_language_rows": inferred_language,
        "quality_score": quality_score,
    }


def _language_suggestions(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT
            event_raw,
            event_label,
            event_type,
            "group",
            language_source,
            COUNT(*)::BIGINT AS count
        FROM events_ordered
        GROUP BY event_raw, event_label, event_type, "group", language_source
        ORDER BY language_source DESC, count DESC
        """
    ).fetchall()
    return [
        {
            "raw": event_raw,
            "suggested_label": event_label,
            "event_type": event_type,
            "group": group,
            "count": int(count),
            "needs_confirmation": bool(language_source != "dictionary" or event_type == "event"),
        }
        for event_raw, event_label, event_type, group, language_source, count in rows
    ]


def _segments(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    buyer_count = int(con.execute("SELECT COUNT(DISTINCT uid) FROM events_ordered WHERE event_type = 'purchase'").fetchone()[0])
    non_buyer_count = int(
        con.execute(
            """
            SELECT COUNT(DISTINCT uid)
            FROM events_ordered
            WHERE uid NOT IN (
                SELECT DISTINCT uid
                FROM events_ordered
                WHERE event_type = 'purchase'
            )
            """
        ).fetchone()[0]
    )

    def rates(where_clause: str, denominator: int) -> list[tuple[str, float]]:
        if denominator <= 0:
            return []
        rows = con.execute(
            f"""
            SELECT "group", COUNT(*)::BIGINT AS count
            FROM events_ordered
            WHERE "group" IS NOT NULL
              AND {where_clause}
            GROUP BY "group"
            ORDER BY count DESC
            LIMIT 10
            """
        ).fetchall()
        return [(str(group), round(int(count) / denominator, 4)) for group, count in rows]

    return {
        "buyer_count": buyer_count,
        "non_buyer_count": non_buyer_count,
        "buyer_group_touch_rate": rates(
            "uid IN (SELECT DISTINCT uid FROM events_ordered WHERE event_type = 'purchase')",
            buyer_count,
        ),
        "non_buyer_group_touch_rate": rates(
            "uid NOT IN (SELECT DISTINCT uid FROM events_ordered WHERE event_type = 'purchase')",
            non_buyer_count,
        ),
    }


def _concentration(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    revenue_rows = con.execute(
        """
        SELECT uid, SUM(amount)::DOUBLE AS revenue
        FROM events_ordered
        WHERE event_type = 'purchase'
        GROUP BY uid
        ORDER BY revenue DESC
        """
    ).fetchall()
    if not revenue_rows:
        return {"top_1_user_share": 0, "top_5pct_share": 0, "top_users": []}
    total = sum(float(revenue or 0) for _, revenue in revenue_rows)
    top_n = max(1, int(len(revenue_rows) * 0.05))
    return {
        "top_1_user_share": round(float(revenue_rows[0][1] or 0) / total, 4) if total else 0,
        "top_5pct_share": round(sum(float(value or 0) for _, value in revenue_rows[:top_n]) / total, 4) if total else 0,
        "top_users": [
            {"uid": str(uid), "revenue": round(float(value or 0), 2)}
            for uid, value in revenue_rows[:5]
        ],
    }


def _frame_records(frame: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    selected = frame.head(limit) if limit else frame
    return [_jsonable(row) for row in selected.to_dict("records")]


def _sample_sessions(con: duckdb.DuckDBPyConnection, sample_limit: int) -> dict[str, Any]:
    count = int(con.execute("SELECT COUNT(*) FROM session_facts").fetchone()[0])
    frame = con.execute(
        """
        SELECT *
        FROM session_facts
        ORDER BY session_id
        LIMIT ?
        """,
        [sample_limit],
    ).fetchdf()
    if "content_groups" in frame.columns:
        frame["content_groups"] = frame["content_groups"].map(_list_value)
    return {"session_count": count, "sample": _frame_records(frame)}


def _sample_journeys(con: duckdb.DuckDBPyConnection, sample_limit: int) -> dict[str, Any]:
    count = int(con.execute("SELECT COUNT(*) FROM user_journeys").fetchone()[0])
    frame = con.execute(
        """
        SELECT *
        FROM user_journeys
        ORDER BY uid
        LIMIT ?
        """,
        [sample_limit],
    ).fetchdf()
    return {"user_count": count, "sample": _frame_records(frame)}


def _build_tables(con: duckdb.DuckDBPyConnection, config: LanguageConfig) -> None:
    _create_events_ordered(con, config.session_gap_minutes)
    _create_session_facts(con)
    _create_user_journeys(con)
    _create_failure_contexts(con)
    _create_purchase_prior_events(con)
    _create_content_facts(con)


def build_staged_analysis(
    inputs: list[str | Path],
    config: LanguageConfig,
    db_path: str | Path,
    sample_limit: int = 20,
) -> StagedAnalysis:
    files = discover_files(inputs)
    unsupported = [path for path in files if path.suffix.lower() not in CSV_SUFFIXES]
    if unsupported:
        names = ", ".join(path.name for path in unsupported[:3])
        raise DuckDBNormalizeUnsupported(f"DuckDB staged pipeline only handles CSV/TSV/TXT files: {names}")

    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)

    raw_tables: list[RawTableInfo] = []
    field_reports: list[dict[str, Any]] = []
    con = duckdb.connect(str(target))
    try:
        _create_base_table(con)
        source_offset = 0
        for path in files:
            info, report, source_offset = _insert_file(con, path, config, source_offset)
            raw_tables.append(info)
            field_reports.append(report)
        _build_tables(con, config)

        purchases = _purchase_contexts(con)
        products = _product_facts(con, purchases)
        content = con.execute("SELECT * FROM content_facts ORDER BY participant_rate ASC, revenue_after_content DESC").fetchdf()
        failures = con.execute("SELECT * FROM failure_contexts ORDER BY failure_events DESC, \"group\"").fetchdf()
        summary = _summary(con)
        summary_by_date = _summary_by_date(con)
        data_quality = _data_quality(con, raw_tables, field_reports)
        language_suggestions = _language_suggestions(con)
        sessions = _sample_sessions(con, sample_limit)
        journeys = _sample_journeys(con, sample_limit)
        segments = _segments(con)
        concentration = _concentration(con)
    finally:
        con.close()

    return StagedAnalysis(
        db_path=target,
        raw_tables=raw_tables,
        field_reports=field_reports,
        summary=summary,
        summary_by_date=summary_by_date,
        data_quality=data_quality,
        language_suggestions=language_suggestions,
        sessions=sessions,
        journeys=journeys,
        failures=failures,
        purchases=purchases,
        content=content,
        products=products,
        segments=segments,
        concentration=concentration,
    )


def export_normalized_events(db_path: str | Path, out_path: str | Path) -> None:
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        con.execute(
            f"""
            COPY (
                SELECT
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
                    session_id
                FROM events_ordered
                ORDER BY event_order
            ) TO {_sql_literal(str(target))} (HEADER, DELIMITER ',')
            """
        )
    finally:
        con.close()
