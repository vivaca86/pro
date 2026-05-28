from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import pandas as pd


def daily_summary(events: pd.DataFrame, sessions: pd.DataFrame) -> dict[str, Any]:
    purchases = events[events["event_type"] == "purchase"]
    active_users = int(events["uid"].nunique())
    paying_users = int(purchases["uid"].nunique())
    revenue = float(purchases["amount"].sum())
    session_count = int(len(sessions))
    avg_session_duration = float(sessions["duration_sec"].mean()) if not sessions.empty else 0.0
    return {
        "active_users": active_users,
        "events": int(len(events)),
        "sessions": session_count,
        "avg_session_duration_sec": round(avg_session_duration, 2),
        "revenue": round(revenue, 2),
        "paying_users": paying_users,
        "conversion_rate": round(paying_users / active_users, 4) if active_users else 0,
        "arppu": round(revenue / paying_users, 2) if paying_users else 0,
    }


def _revenue_after_content(events: pd.DataFrame) -> dict[str, float]:
    revenue_by_group: dict[str, float] = defaultdict(float)
    for _, session_events in events.groupby("session_id", sort=False):
        session_events = session_events.sort_values("ts").reset_index(drop=True)
        seen_groups: set[str] = set()
        for _, event in session_events.iterrows():
            if pd.notna(event["group"]) and str(event["group"]):
                seen_groups.add(str(event["group"]))
            if event["event_type"] == "purchase" and seen_groups:
                share = float(event["amount"]) / len(seen_groups)
                for group in seen_groups:
                    revenue_by_group[group] += share
    return revenue_by_group


def content_health(events: pd.DataFrame, failures: pd.DataFrame) -> pd.DataFrame:
    active_users = events["uid"].nunique()
    revenue_map = _revenue_after_content(events)
    failure_lookup = {row["group"]: row for row in failures.to_dict("records")} if not failures.empty else {}
    rows: list[dict[str, Any]] = []

    for group, group_events in events[events["group"].notna()].groupby("group"):
        participants = int(group_events["uid"].nunique())
        success_events = int((group_events["event_type"] == "content_success").sum())
        fail_events = int(group_events["event_type"].isin({"content_fail", "match_issue"}).sum())
        outcome_events = success_events + fail_events
        reward_users = int(group_events.loc[group_events["event_type"] == "reward_claim", "uid"].nunique())
        success_users = int(group_events.loc[group_events["event_type"] == "content_success", "uid"].nunique())
        wait_rows = group_events[
            (group_events["event_type"] == "match_issue") | (group_events["wait_time_sec"] > 0)
        ]
        wait_values = wait_rows["wait_time_sec"]
        if (wait_values <= 0).all() and not wait_rows.empty:
            wait_values = wait_rows["duration_sec"]
        failure_info = failure_lookup.get(group, {})
        rows.append(
            {
                "group": group,
                "participant_users": participants,
                "participant_rate": round(participants / active_users, 4) if active_users else 0,
                "event_count": int(len(group_events)),
                "avg_duration_sec": round(float(group_events["duration_sec"].replace(0, pd.NA).dropna().mean() or 0), 2),
                "success_events": success_events,
                "fail_events": fail_events,
                "failure_rate": round(fail_events / outcome_events, 4) if outcome_events else 0,
                "reward_claim_rate": round(reward_users / success_users, 4) if success_users else 0,
                "avg_wait_sec": round(float(wait_values.replace(0, pd.NA).dropna().mean() or 0), 2),
                "retry_after_failure_rate": round(float(failure_info.get("retry_after_failure_rate", 0)), 4),
                "revenue_after_content": round(revenue_map.get(str(group), 0), 2),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(
        ["participant_rate", "revenue_after_content"], ascending=[True, False]
    )


def product_performance(events: pd.DataFrame, purchase_contexts: dict[str, Any]) -> pd.DataFrame:
    purchases = events[events["event_type"] == "purchase"].copy()
    if purchases.empty:
        return pd.DataFrame(columns=["product", "buyers", "purchase_count", "revenue", "avg_amount", "top_context_groups"])
    rows: list[dict[str, Any]] = []
    contexts = purchase_contexts.get("product_contexts", {})
    for product, product_rows in purchases.groupby(purchases["product_label"].fillna(purchases["product_id"])):
        rows.append(
            {
                "product": product,
                "buyers": int(product_rows["uid"].nunique()),
                "purchase_count": int(len(product_rows)),
                "revenue": round(float(product_rows["amount"].sum()), 2),
                "avg_amount": round(float(product_rows["amount"].mean()), 2),
                "top_context_groups": contexts.get(str(product), []),
            }
        )
    return pd.DataFrame(rows).sort_values("revenue", ascending=False)


def segment_compare(events: pd.DataFrame) -> dict[str, Any]:
    purchases = events[events["event_type"] == "purchase"]
    buyers = set(purchases["uid"].astype(str))
    all_users = set(events["uid"].astype(str))
    non_buyers = all_users - buyers

    def group_rates(user_set: set[str]) -> list[tuple[str, float]]:
        if not user_set:
            return []
        user_events = events[events["uid"].astype(str).isin(user_set)]
        counts = Counter(user_events["group"].dropna().astype(str))
        return [(group, round(count / len(user_set), 4)) for group, count in counts.most_common(10)]

    return {
        "buyer_count": len(buyers),
        "non_buyer_count": len(non_buyers),
        "buyer_group_touch_rate": group_rates(buyers),
        "non_buyer_group_touch_rate": group_rates(non_buyers),
    }


def whale_concentration(events: pd.DataFrame) -> dict[str, Any]:
    purchases = events[events["event_type"] == "purchase"]
    if purchases.empty:
        return {"top_1_user_share": 0, "top_5pct_share": 0, "top_users": []}
    revenue_by_user = purchases.groupby("uid")["amount"].sum().sort_values(ascending=False)
    total = float(revenue_by_user.sum())
    top_n = max(1, int(len(revenue_by_user) * 0.05))
    return {
        "top_1_user_share": round(float(revenue_by_user.iloc[0]) / total, 4) if total else 0,
        "top_5pct_share": round(float(revenue_by_user.head(top_n).sum()) / total, 4) if total else 0,
        "top_users": [{"uid": str(uid), "revenue": round(float(value), 2)} for uid, value in revenue_by_user.head(5).items()],
    }
