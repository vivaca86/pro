from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd


CONTENT_HEALTH_COLUMNS = [
    "group",
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
]

PRODUCT_PERFORMANCE_COLUMNS = [
    "product",
    "buyers",
    "purchase_count",
    "revenue",
    "avg_amount",
    "top_context_groups",
]


def content_health(events: pd.DataFrame, failures: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame(columns=CONTENT_HEALTH_COLUMNS)

    frame = events.copy()
    frame["_source_order"] = range(len(frame))
    failure_frame = failures.copy()
    if failure_frame.empty:
        failure_frame = pd.DataFrame(columns=["group", "retry_after_failure_rate"])

    con = duckdb.connect(":memory:")
    try:
        con.register("events_in", frame)
        con.register("failures_in", failure_frame)
        result = con.execute(
            """
            WITH ordered AS (
                SELECT
                    ROW_NUMBER() OVER (ORDER BY _source_order) - 1 AS event_order,
                    *
                FROM events_in
            ),
            active AS (
                SELECT COUNT(DISTINCT uid)::DOUBLE AS active_users
                FROM ordered
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
                FROM ordered
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
                FROM ordered
                WHERE "group" IS NOT NULL
                GROUP BY "group"
            ),
            first_group_seen AS (
                SELECT
                    session_id,
                    "group",
                    MIN(event_order) AS first_seen_order
                FROM ordered
                WHERE "group" IS NOT NULL
                  AND CAST("group" AS VARCHAR) <> ''
                GROUP BY session_id, "group"
            ),
            purchase_groups AS (
                SELECT
                    purchase.event_order,
                    first_group_seen."group",
                    COALESCE(purchase.amount, 0)::DOUBLE AS amount
                FROM ordered purchase
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
            ),
            failure_lookup AS (
                SELECT
                    "group",
                    ANY_VALUE(retry_after_failure_rate)::DOUBLE AS retry_after_failure_rate
                FROM failures_in
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
                ROUND(COALESCE(failure_lookup.retry_after_failure_rate, 0), 4)::DOUBLE AS retry_after_failure_rate,
                ROUND(COALESCE(revenue_after.revenue_after_content, 0), 2)::DOUBLE AS revenue_after_content
            FROM content_base
            CROSS JOIN active
            LEFT JOIN wait_stats USING ("group")
            LEFT JOIN revenue_after USING ("group")
            LEFT JOIN failure_lookup USING ("group")
            ORDER BY participant_rate ASC, revenue_after_content DESC
            """
        ).fetchdf()
    finally:
        con.close()

    return result[CONTENT_HEALTH_COLUMNS]


def product_performance(events: pd.DataFrame, purchase_contexts: dict[str, Any]) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame(columns=PRODUCT_PERFORMANCE_COLUMNS)

    frame = events.copy()
    con = duckdb.connect(":memory:")
    try:
        con.register("events_in", frame)
        result = con.execute(
            """
            WITH purchases AS (
                SELECT
                    COALESCE(product_label, product_id) AS product,
                    uid,
                    COALESCE(amount, 0)::DOUBLE AS amount
                FROM events_in
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
    finally:
        con.close()

    if result.empty:
        return pd.DataFrame(columns=PRODUCT_PERFORMANCE_COLUMNS)

    contexts = purchase_contexts.get("product_contexts", {})
    result["top_context_groups"] = result["product"].map(lambda product: contexts.get(str(product), []))
    return result[PRODUCT_PERFORMANCE_COLUMNS]
