from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import pandas as pd


def add_sessions(events: pd.DataFrame, session_gap_minutes: int = 30) -> pd.DataFrame:
    if events.empty:
        events = events.copy()
        events["session_index"] = []
        events["session_id"] = []
        return events
    events = events.sort_values(["uid", "ts"], na_position="last").copy()
    previous_ts = events.groupby("uid")["ts"].shift()
    gap = (events["ts"] - previous_ts).dt.total_seconds().div(60)
    new_session = previous_ts.isna() | gap.isna() | (gap > session_gap_minutes)
    events["session_index"] = new_session.groupby(events["uid"]).cumsum().astype(int) - 1
    events["session_id"] = events["uid"].astype(str) + "-" + events["session_index"].astype(str)
    return events.reset_index(drop=True)


def _first_value(frame: pd.DataFrame, mask: pd.Series, column: str) -> Any:
    values = frame.loc[mask, column]
    if values.empty:
        return None
    value = values.iloc[0]
    return None if pd.isna(value) else value


def build_user_journeys(events: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    purchase_types = {"purchase"}
    failure_types = {"content_fail", "match_issue"}
    enter_types = {"content_enter", "session_start"}

    for uid, user_events in events.groupby("uid", sort=False):
        user_events = user_events.sort_values("ts")
        first_content_mask = user_events["group"].notna() & user_events["event_type"].isin(enter_types | failure_types)
        first_failure_mask = user_events["event_type"].isin(failure_types)
        first_purchase_mask = user_events["event_type"].isin(purchase_types)
        first_event = user_events.iloc[0]
        last_event = user_events.iloc[-1]
        revenue = float(user_events.loc[first_purchase_mask, "amount"].sum())
        rows.append(
            {
                "uid": uid,
                "first_seen": first_event["ts"],
                "last_seen": last_event["ts"],
                "event_count": int(len(user_events)),
                "session_count": int(user_events["session_id"].nunique()),
                "first_event": first_event["event_label"],
                "first_content": _first_value(user_events, first_content_mask, "content_label"),
                "first_failure": _first_value(user_events, first_failure_mask, "event_label"),
                "first_failure_group": _first_value(user_events, first_failure_mask, "group"),
                "first_purchase_product": _first_value(user_events, first_purchase_mask, "product_label"),
                "purchase_count": int(first_purchase_mask.sum()),
                "revenue": revenue,
                "last_event": last_event["event_label"],
                "last_group": None if pd.isna(last_event["group"]) else last_event["group"],
                "last_event_type": last_event["event_type"],
            }
        )
    return pd.DataFrame(rows)


def build_session_flows(events: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for session_id, session_events in events.groupby("session_id", sort=False):
        session_events = session_events.sort_values("ts")
        first = session_events.iloc[0]
        last = session_events.iloc[-1]
        duration_sec = 0
        if pd.notna(first["ts"]) and pd.notna(last["ts"]):
            duration_sec = max(0, int((last["ts"] - first["ts"]).total_seconds()))
        groups = [group for group in session_events["group"].dropna().astype(str).unique().tolist() if group]
        purchases = session_events[session_events["event_type"] == "purchase"]
        rows.append(
            {
                "session_id": session_id,
                "uid": first["uid"],
                "start": first["ts"],
                "end": last["ts"],
                "duration_sec": duration_sec,
                "event_count": int(len(session_events)),
                "first_event": first["event_label"],
                "last_event": last["event_label"],
                "last_event_type": last["event_type"],
                "content_groups": groups,
                "purchase_count": int(len(purchases)),
                "revenue": float(purchases["amount"].sum()),
                "ended_after_failure": bool(last["event_type"] in {"content_fail", "match_issue"}),
            }
        )
    return pd.DataFrame(rows)


def build_purchase_contexts(events: pd.DataFrame, lookback_events: int = 5, lookback_minutes: int = 60) -> dict[str, Any]:
    preceding_labels: Counter[str] = Counter()
    preceding_groups: Counter[str] = Counter()
    product_contexts: dict[str, Counter[str]] = defaultdict(Counter)
    examples: list[dict[str, Any]] = []

    for uid, user_events in events.groupby("uid", sort=False):
        user_events = user_events.sort_values("ts").reset_index(drop=True)
        purchase_indexes = user_events.index[user_events["event_type"] == "purchase"].tolist()
        for purchase_index in purchase_indexes:
            purchase = user_events.loc[purchase_index]
            prior = user_events.iloc[max(0, purchase_index - lookback_events) : purchase_index].copy()
            if pd.notna(purchase["ts"]):
                cutoff = purchase["ts"] - pd.Timedelta(minutes=lookback_minutes)
                prior = prior[prior["ts"].isna() | (prior["ts"] >= cutoff)]
            for label in prior["event_label"].dropna().astype(str):
                preceding_labels[label] += 1
            for group in prior["group"].dropna().astype(str):
                preceding_groups[group] += 1
                product_contexts[str(purchase["product_label"] or purchase["product_id"])][group] += 1
            if len(examples) < 10:
                examples.append(
                    {
                        "uid": uid,
                        "product": purchase["product_label"] or purchase["product_id"],
                        "amount": float(purchase["amount"]),
                        "previous_events": prior["event_label"].dropna().astype(str).tolist(),
                    }
                )

    return {
        "top_preceding_events": preceding_labels.most_common(10),
        "top_preceding_groups": preceding_groups.most_common(10),
        "product_contexts": {
            product: counter.most_common(5) for product, counter in sorted(product_contexts.items())
        },
        "examples": examples,
    }


def build_failure_contexts(events: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    failure_events = events[events["event_type"].isin({"content_fail", "match_issue"})]
    if failure_events.empty:
        return pd.DataFrame(
            columns=["group", "failure_users", "failure_events", "retry_users", "retry_after_failure_rate"]
        )
    group_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"failure_events": 0, "failure_users": set(), "retry_users": set()})

    for uid, user_events in events.groupby("uid", sort=False):
        user_events = user_events.sort_values("ts").reset_index(drop=True)
        for index, event in user_events.iterrows():
            if event["event_type"] not in {"content_fail", "match_issue"}:
                continue
            group = event["group"] or event["content_label"] or "unknown"
            stats = group_stats[str(group)]
            stats["failure_events"] += 1
            stats["failure_users"].add(uid)
            later = user_events.iloc[index + 1 :]
            retried = later[later["group"].astype(str) == str(group)]
            if not retried.empty:
                stats["retry_users"].add(uid)

    for group, stats in group_stats.items():
        failure_users = len(stats["failure_users"])
        retry_users = len(stats["retry_users"])
        rows.append(
            {
                "group": group,
                "failure_users": failure_users,
                "failure_events": int(stats["failure_events"]),
                "retry_users": retry_users,
                "retry_after_failure_rate": retry_users / failure_users if failure_users else 0,
            }
        )
    return pd.DataFrame(rows).sort_values("failure_events", ascending=False)
