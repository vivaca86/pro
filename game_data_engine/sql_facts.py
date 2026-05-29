from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd


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


def _connect_events(events: pd.DataFrame, session_gap_minutes: int) -> duckdb.DuckDBPyConnection:
    frame = events.copy()
    frame["_source_order"] = range(len(frame))
    con = duckdb.connect(":memory:")
    con.register("events_in", frame)
    con.execute(
        """
        CREATE TEMP TABLE events_ordered AS
        WITH ordered AS (
            SELECT
                *,
                LAG(ts) OVER (
                    PARTITION BY uid
                    ORDER BY ts NULLS LAST, _source_order
                ) AS previous_ts
            FROM events_in
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
    return con


def _events_frame(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(
        """
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
        """
    ).fetchdf()


def _sessions_frame(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    frame = con.execute(
        """
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
    ).fetchdf()
    if "content_groups" in frame.columns:
        frame["content_groups"] = frame["content_groups"].map(_list_value)
    return frame


def _journeys_frame(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(
        """
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
    ).fetchdf()


def _failures_frame(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(
        """
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
    ).fetchdf()


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


def _purchase_contexts(con: duckdb.DuckDBPyConnection, lookback_events: int, lookback_minutes: int) -> dict[str, Any]:
    con.execute(
        """
        CREATE TEMP TABLE purchase_prior_events AS
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
    top_preceding_events = _top_pairs(con, "event_label")
    top_preceding_groups = _top_pairs(con, "content_group")
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

    example_rows = con.execute(
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
    examples = [
        {
            "uid": str(uid),
            "product": product,
            "amount": float(amount or 0),
            "previous_events": [] if previous_events is None else list(previous_events),
        }
        for uid, product, amount, previous_events in example_rows
    ]
    return {
        "top_preceding_events": top_preceding_events,
        "top_preceding_groups": top_preceding_groups,
        "product_contexts": product_contexts,
        "examples": examples,
    }


def build_sql_facts(
    events: pd.DataFrame,
    session_gap_minutes: int = 30,
    lookback_events: int = 5,
    lookback_minutes: int = 60,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    if events.empty:
        empty_events = events.copy()
        empty_events["session_index"] = []
        empty_events["session_id"] = []
        return (
            empty_events,
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(columns=["group", "failure_users", "failure_events", "retry_users", "retry_after_failure_rate"]),
            {"top_preceding_events": [], "top_preceding_groups": [], "product_contexts": {}, "examples": []},
        )

    con = _connect_events(events, session_gap_minutes)
    try:
        events_with_sessions = _events_frame(con)
        sessions = _sessions_frame(con)
        journeys = _journeys_frame(con)
        failures = _failures_frame(con)
        purchases = _purchase_contexts(con, lookback_events, lookback_minutes)
    finally:
        con.close()
    return events_with_sessions, sessions, journeys, failures, purchases
