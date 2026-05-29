from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd


UNKNOWN_CODE = "unknown"
EMPTY_VALUES = {"", "nan", "none", "null", "<na>"}


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
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


def _clean_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series("", index=frame.index, dtype="string")
    values = frame[column].astype("string").fillna("").str.strip()
    return values.mask(values.str.lower().isin(EMPTY_VALUES), "")


def _first_non_empty(*series: pd.Series, fallback: str = UNKNOWN_CODE) -> pd.Series:
    if not series:
        return pd.Series([], dtype="string")
    result = pd.Series("", index=series[0].index, dtype="string")
    for values in series:
        empty = result.str.len().eq(0)
        result = result.mask(empty, values)
    return result.mask(result.str.len().eq(0), fallback)


def _records(frame: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    selected = frame.head(limit) if limit else frame
    return [_jsonable(row) for row in selected.to_dict("records")]


def _empty_behavior() -> dict[str, Any]:
    return {
        "event_count": 0,
        "user_count": 0,
        "top_codes": [],
        "participation": [],
        "content_participation": [],
        "common_paths": [],
        "transition_rates": [],
        "loop_patterns": [],
        "entry_events": [],
        "exit_events": [],
        "stop_points": [],
        "outliers": [],
        "top_transitions": [],
        "users": [],
        "sessions": [],
        "note": "코드 의미는 확정하지 않고, 전체 유저의 참여율과 공통 흐름을 보여줍니다.",
    }


def _sequence_text(sequence: list[dict[str, Any]], omitted_runs: int = 0) -> str:
    parts = []
    for item in sequence:
        code = str(item.get("code") or UNKNOWN_CODE)
        count = int(item.get("count") or 0)
        parts.append(f"{code} x{count}" if count > 1 else code)
    if omitted_runs > 0:
        parts.append(f"... +{omitted_runs} more")
    return " -> ".join(parts)


def _sequence_from_frame(frame: pd.DataFrame, sequence_limit: int) -> tuple[list[dict[str, Any]], int]:
    if frame.empty:
        return [], 0
    runs = frame["_event_code"].ne(frame["_event_code"].shift()).fillna(True).cumsum()
    compact = (
        frame.assign(_run_id=runs)
        .groupby("_run_id", sort=False)
        .agg(
            code=("_event_code", "first"),
            label=("_event_label", "first"),
            count=("_event_code", "size"),
            first_ts=("ts", "min"),
            last_ts=("ts", "max"),
        )
        .reset_index(drop=True)
    )
    total_runs = len(compact)
    sequence = _records(compact.head(sequence_limit))
    return sequence, max(0, total_runs - len(sequence))


def _top_code_rows(frame: pd.DataFrame, limit: int, total_users: int | None = None) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    denominator = max(int(total_users or frame["uid"].nunique() or 0), 1)
    top = (
        frame.groupby("_event_code", dropna=False)
        .agg(
            code=("_event_code", "first"),
            label=("_event_label", "first"),
            event_type=("_event_type", "first"),
            group=("_event_group", "first"),
            count=("_event_code", "size"),
            user_count=("uid", "nunique"),
            session_count=("session_id", "nunique"),
            first_seen=("ts", "min"),
            last_seen=("ts", "max"),
        )
        .sort_values(["count", "user_count", "code"], ascending=[False, False, True])
        .head(limit)
        .reset_index(drop=True)
    )
    top["user_rate"] = (top["user_count"] / denominator).round(4)
    top["events_per_user"] = (top["count"] / top["user_count"].clip(lower=1)).round(2)
    return _records(top)


def _transition_rows(frame: pd.DataFrame, limit: int, total_users: int | None = None) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    denominator = max(int(total_users or frame["uid"].nunique() or 0), 1)
    source_users = frame.groupby("_event_code")["uid"].nunique().to_dict()
    with_next = frame.copy()
    with_next["_next_code"] = with_next.groupby("uid", sort=False)["_event_code"].shift(-1)
    with_next = with_next[with_next["_next_code"].notna() & (with_next["_next_code"] != "")]
    if with_next.empty:
        return []
    transitions = (
        with_next.groupby(["_event_code", "_next_code"], dropna=False)
        .agg(count=("_event_code", "size"), user_count=("uid", "nunique"))
        .reset_index()
        .rename(columns={"_event_code": "from", "_next_code": "to"})
        .sort_values(["count", "user_count", "from", "to"], ascending=[False, False, True, True])
    )
    transitions["from_user_count"] = transitions["from"].map(lambda code: int(source_users.get(code, 0)))
    transitions["user_rate"] = (transitions["user_count"] / denominator).round(4)
    transitions["transition_rate"] = (
        transitions["user_count"] / transitions["from_user_count"].clip(lower=1)
    ).round(4)
    return _records(transitions.head(limit))


def _common_path_rows(frame: pd.DataFrame, limit: int, total_users: int, min_length: int = 2, max_length: int = 4) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    path_stats: dict[tuple[str, ...], dict[str, Any]] = {}
    label_lookup = frame.groupby("_event_code")["_event_label"].first().to_dict()
    for uid, user_events in frame.groupby("uid", sort=False):
        compact, _ = _sequence_from_frame(user_events, sequence_limit=10_000)
        codes = [str(item["code"]) for item in compact if item.get("code")]
        if len(codes) < min_length:
            continue
        seen_by_user: set[tuple[str, ...]] = set()
        for length in range(min_length, min(max_length, len(codes)) + 1):
            for index in range(0, len(codes) - length + 1):
                path = tuple(codes[index : index + length])
                stats = path_stats.setdefault(
                    path,
                    {"path": list(path), "path_text": " -> ".join(path), "length": length, "occurrence_count": 0, "users": set()},
                )
                stats["occurrence_count"] += 1
                if path not in seen_by_user:
                    stats["users"].add(uid)
                    seen_by_user.add(path)

    denominator = max(int(total_users or 0), 1)
    rows = []
    for path, stats in path_stats.items():
        labels = [str(label_lookup.get(code) or code) for code in path]
        user_count = len(stats["users"])
        rows.append(
            {
                "path": list(path),
                "labels": labels,
                "path_text": " -> ".join(labels),
                "code_path_text": " -> ".join(path),
                "length": int(stats["length"]),
                "occurrence_count": int(stats["occurrence_count"]),
                "user_count": user_count,
                "user_rate": round(user_count / denominator, 4),
            }
        )
    rows.sort(
        key=lambda row: (
            -int(row["user_count"]),
            -int(row["length"]),
            -int(row["occurrence_count"]),
            str(row["code_path_text"]),
        )
    )
    return _jsonable(rows[:limit])


def _distribution_rows(frame: pd.DataFrame, limit: int, total_users: int, mode: str) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    sorted_frame = frame.sort_values(["uid", "ts", "_row_order"], na_position="last")
    selected = sorted_frame.groupby("uid", sort=False).head(1) if mode == "first" else sorted_frame.groupby("uid", sort=False).tail(1)
    rows = (
        selected.groupby("_event_code", dropna=False)
        .agg(
            code=("_event_code", "first"),
            label=("_event_label", "first"),
            event_type=("_event_type", "first"),
            group=("_event_group", "first"),
            user_count=("uid", "nunique"),
        )
        .sort_values(["user_count", "code"], ascending=[False, True])
        .head(limit)
        .reset_index(drop=True)
    )
    denominator = max(int(total_users or 0), 1)
    rows["user_rate"] = (rows["user_count"] / denominator).round(4)
    return _records(rows)


def _content_participation_rows(frame: pd.DataFrame, limit: int, total_users: int) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    content_frame = frame[frame["_content_group"].str.len().gt(0)].copy()
    if content_frame.empty:
        return []
    rows = (
        content_frame.groupby("_content_group", dropna=False)
        .agg(
            group=("_content_group", "first"),
            event_count=("_content_group", "size"),
            user_count=("uid", "nunique"),
            session_count=("session_id", "nunique"),
            code_count=("_event_code", "nunique"),
            first_seen=("ts", "min"),
            last_seen=("ts", "max"),
        )
        .sort_values(["user_count", "event_count", "group"], ascending=[False, False, True])
        .head(limit)
        .reset_index(drop=True)
    )
    denominator = max(int(total_users or 0), 1)
    rows["user_rate"] = (rows["user_count"] / denominator).round(4)
    rows["events_per_user"] = (rows["event_count"] / rows["user_count"].clip(lower=1)).round(2)
    return _records(rows)


def _loop_rows(transitions: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    loops = [row for row in transitions if row.get("from") == row.get("to")]
    loops.sort(key=lambda row: (-int(row.get("user_count") or 0), -int(row.get("count") or 0), str(row.get("from") or "")))
    return loops[:limit]


def _outlier_rows(
    participation: list[dict[str, Any]],
    transition_rates: list[dict[str, Any]],
    loop_patterns: list[dict[str, Any]],
    stop_points: list[dict[str, Any]],
    total_users: int,
    limit: int = 12,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in participation:
        events_per_user = float(item.get("events_per_user") or 0)
        user_rate = float(item.get("user_rate") or 0)
        if user_rate >= 0.7 and events_per_user >= 8:
            label = str(item.get("label") or item.get("code") or "-")
            rows.append(
                {
                    "type": "high_repeat_common_event",
                    "title": f"{label} 반복량 높음",
                    "code": item.get("code"),
                    "user_count": item.get("user_count"),
                    "user_rate": user_rate,
                    "metric": events_per_user,
                    "evidence": f"{int(item.get('user_count') or 0)}명 참여, 인당 평균 {events_per_user:.1f}건",
                }
            )
        if user_rate <= 0.35 and int(item.get("count") or 0) >= max(5, total_users):
            label = str(item.get("label") or item.get("code") or "-")
            rows.append(
                {
                    "type": "minority_heavy_event",
                    "title": f"{label} 소수 반복",
                    "code": item.get("code"),
                    "user_count": item.get("user_count"),
                    "user_rate": user_rate,
                    "metric": int(item.get("count") or 0),
                    "evidence": f"참여율 {user_rate * 100:.1f}%, 총 {int(item.get('count') or 0)}건",
                }
            )
    for item in loop_patterns:
        user_rate = float(item.get("user_rate") or 0)
        count = int(item.get("count") or 0)
        if user_rate >= 0.4 or count >= max(5, total_users):
            code = str(item.get("from") or "-")
            rows.append(
                {
                    "type": "loop_pattern",
                    "title": f"{code} 반복 루프",
                    "code": code,
                    "user_count": item.get("user_count"),
                    "user_rate": user_rate,
                    "metric": count,
                    "evidence": f"{int(item.get('user_count') or 0)}명에게 {count}회 반복 전환",
                }
            )
    for item in stop_points:
        user_rate = float(item.get("user_rate") or 0)
        if user_rate >= 0.6:
            label = str(item.get("label") or item.get("code") or "-")
            rows.append(
                {
                    "type": "stop_point",
                    "title": f"{label}에서 종료 집중",
                    "code": item.get("code"),
                    "user_count": item.get("user_count"),
                    "user_rate": user_rate,
                    "metric": user_rate,
                    "evidence": f"마지막 행동 기준 {int(item.get('user_count') or 0)}명, {user_rate * 100:.1f}%",
                }
            )
    for item in transition_rates:
        transition_rate = float(item.get("transition_rate") or 0)
        from_users = int(item.get("from_user_count") or 0)
        if from_users >= max(2, total_users // 2) and transition_rate >= 0.8:
            rows.append(
                {
                    "type": "dominant_transition",
                    "title": f"{item.get('from')} → {item.get('to')} 전환 집중",
                    "from": item.get("from"),
                    "to": item.get("to"),
                    "user_count": item.get("user_count"),
                    "user_rate": item.get("user_rate"),
                    "metric": transition_rate,
                    "evidence": f"{from_users}명 중 {int(item.get('user_count') or 0)}명 전환",
                }
            )
    rows.sort(key=lambda row: (-float(row.get("user_rate") or 0), -float(row.get("metric") or 0), str(row.get("title") or "")))
    return _jsonable(rows[:limit])


def build_behavior_flow(events: pd.DataFrame, sample_limit: int = 8, sequence_limit: int = 24) -> dict[str, Any]:
    if events.empty:
        return _empty_behavior()

    frame = events.copy()
    frame["_row_order"] = range(len(frame))
    frame["uid"] = _clean_series(frame, "uid")
    frame = frame[frame["uid"].str.len().gt(0)].copy()
    if frame.empty:
        return _empty_behavior()

    frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce") if "ts" in frame.columns else pd.NaT
    raw = _clean_series(frame, "event_raw")
    label = _clean_series(frame, "event_label")
    event_type = _clean_series(frame, "event_type")
    event_group = _clean_series(frame, "group")
    content_label = _clean_series(frame, "content_label")
    content_id = _clean_series(frame, "content_id")
    frame["_event_code"] = _first_non_empty(raw, label, event_type)
    frame["_event_label"] = _first_non_empty(label, raw, event_type)
    frame["_event_type"] = _first_non_empty(event_type, fallback="event")
    frame["_event_group"] = _first_non_empty(event_group, content_label, content_id, fallback="")
    frame["_content_group"] = _first_non_empty(event_group, content_label, content_id, fallback="")
    if "session_id" not in frame.columns:
        frame["session_id"] = frame["uid"].astype(str) + "-0"
    frame = frame.sort_values(["uid", "ts", "_row_order"], na_position="last").reset_index(drop=True)
    total_users = int(frame["uid"].nunique())

    user_stats = (
        frame.groupby("uid", dropna=False)
        .agg(
            event_count=("uid", "size"),
            session_count=("session_id", "nunique"),
            first_seen=("ts", "min"),
            last_seen=("ts", "max"),
        )
        .reset_index()
        .sort_values(["event_count", "uid"], ascending=[False, True])
        .head(sample_limit)
    )

    users: list[dict[str, Any]] = []
    for row in user_stats.to_dict("records"):
        uid = str(row["uid"])
        user_events = frame[frame["uid"] == uid].copy()
        sequence, omitted = _sequence_from_frame(user_events, sequence_limit)
        top_codes = _top_code_rows(user_events, 5, total_users=1)
        first_seen = row.get("first_seen")
        last_seen = row.get("last_seen")
        duration_sec = 0
        if pd.notna(first_seen) and pd.notna(last_seen):
            duration_sec = max(0, int((last_seen - first_seen).total_seconds()))
        users.append(
            {
                "uid": uid,
                "event_count": int(row.get("event_count") or 0),
                "session_count": int(row.get("session_count") or 0),
                "first_seen": first_seen,
                "last_seen": last_seen,
                "duration_sec": duration_sec,
                "top_codes": top_codes,
                "sequence": sequence,
                "sequence_text": _sequence_text(sequence, omitted),
                "omitted_sequence_runs": omitted,
            }
        )

    session_stats = (
        frame.groupby("session_id", dropna=False)
        .agg(
            uid=("uid", "first"),
            event_count=("session_id", "size"),
            start=("ts", "min"),
            end=("ts", "max"),
        )
        .reset_index()
        .sort_values(["event_count", "session_id"], ascending=[False, True])
        .head(sample_limit)
    )
    sessions: list[dict[str, Any]] = []
    for row in session_stats.to_dict("records"):
        session_id = str(row["session_id"])
        session_events = frame[frame["session_id"].astype(str) == session_id].copy()
        sequence, omitted = _sequence_from_frame(session_events, sequence_limit)
        start = row.get("start")
        end = row.get("end")
        duration_sec = 0
        if pd.notna(start) and pd.notna(end):
            duration_sec = max(0, int((end - start).total_seconds()))
        sessions.append(
            {
                "session_id": session_id,
                "uid": str(row.get("uid")),
                "event_count": int(row.get("event_count") or 0),
                "start": start,
                "end": end,
                "duration_sec": duration_sec,
                "sequence": sequence,
                "sequence_text": _sequence_text(sequence, omitted),
                "omitted_sequence_runs": omitted,
            }
        )

    participation = _top_code_rows(frame, 20, total_users=total_users)
    transition_rates = _transition_rows(frame, 30, total_users=total_users)
    loop_patterns = _loop_rows(transition_rates, 8)
    entry_events = _distribution_rows(frame, 8, total_users, "first")
    exit_events = _distribution_rows(frame, 8, total_users, "last")
    common_paths = _common_path_rows(frame, 12, total_users)
    outliers = _outlier_rows(participation, transition_rates, loop_patterns, exit_events, total_users)

    return _jsonable(
        {
            "event_count": int(len(frame)),
            "user_count": total_users,
            "top_codes": participation[:12],
            "participation": participation,
            "content_participation": _content_participation_rows(frame, 12, total_users),
            "common_paths": common_paths,
            "transition_rates": transition_rates,
            "loop_patterns": loop_patterns,
            "entry_events": entry_events,
            "exit_events": exit_events,
            "stop_points": exit_events,
            "outliers": outliers,
            "top_transitions": transition_rates[:12],
            "users": users,
            "sessions": sessions,
            "note": "코드 의미는 확정하지 않고, 전체 유저의 참여율과 공통 흐름을 보여줍니다.",
        }
    )


CODE_SQL = """
COALESCE(
    NULLIF(TRIM(CAST(event_raw AS VARCHAR)), ''),
    NULLIF(TRIM(CAST(event_label AS VARCHAR)), ''),
    NULLIF(TRIM(CAST(event_type AS VARCHAR)), ''),
    'unknown'
)
"""

LABEL_SQL = """
COALESCE(
    NULLIF(TRIM(CAST(event_label AS VARCHAR)), ''),
    NULLIF(TRIM(CAST(event_raw AS VARCHAR)), ''),
    NULLIF(TRIM(CAST(event_type AS VARCHAR)), ''),
    'unknown'
)
"""

EVENT_TYPE_SQL = """
COALESCE(
    NULLIF(TRIM(CAST(event_type AS VARCHAR)), ''),
    'event'
)
"""

EVENT_GROUP_SQL = """
COALESCE(
    NULLIF(TRIM(CAST("group" AS VARCHAR)), ''),
    NULLIF(TRIM(CAST(content_label AS VARCHAR)), ''),
    NULLIF(TRIM(CAST(content_id AS VARCHAR)), ''),
    ''
)
"""

CONTENT_SQL = EVENT_GROUP_SQL


def _sequence_from_sql(
    con: duckdb.DuckDBPyConnection,
    where_sql: str,
    params: list[Any],
    sequence_limit: int,
) -> tuple[list[dict[str, Any]], int]:
    frame = con.execute(
        f"""
        WITH scoped AS (
            SELECT
                event_order,
                {CODE_SQL} AS code,
                {LABEL_SQL} AS label,
                ts
            FROM events_ordered
            WHERE {where_sql}
        ),
        marked AS (
            SELECT
                *,
                CASE
                    WHEN LAG(code) OVER (ORDER BY event_order) IS NULL THEN 1
                    WHEN LAG(code) OVER (ORDER BY event_order) != code THEN 1
                    ELSE 0
                END AS new_run
            FROM scoped
        ),
        runs AS (
            SELECT
                *,
                SUM(new_run) OVER (
                    ORDER BY event_order
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS run_id
            FROM marked
        ),
        compact AS (
            SELECT
                run_id,
                MIN(event_order) AS sort_order,
                ANY_VALUE(code) AS code,
                ANY_VALUE(label) AS label,
                COUNT(*)::BIGINT AS count,
                MIN(ts) AS first_ts,
                MAX(ts) AS last_ts
            FROM runs
            GROUP BY run_id
        )
        SELECT
            code,
            label,
            count,
            first_ts,
            last_ts,
            COUNT(*) OVER ()::BIGINT AS total_runs
        FROM compact
        ORDER BY sort_order
        LIMIT ?
        """,
        [*params, sequence_limit],
    ).fetchdf()
    if frame.empty:
        return [], 0
    total_runs = int(frame["total_runs"].iloc[0] or 0)
    frame = frame.drop(columns=["total_runs"])
    sequence = _records(frame)
    return sequence, max(0, total_runs - len(sequence))


def build_behavior_flow_from_duckdb(
    con: duckdb.DuckDBPyConnection,
    sample_limit: int = 8,
    sequence_limit: int = 24,
) -> dict[str, Any]:
    event_count, user_count = con.execute(
        """
        SELECT COUNT(*)::BIGINT, COUNT(DISTINCT uid)::BIGINT
        FROM events_ordered
        WHERE uid IS NOT NULL AND TRIM(CAST(uid AS VARCHAR)) <> ''
        """
    ).fetchone()
    if not event_count:
        return _empty_behavior()

    top_codes = _records(
        con.execute(
            f"""
            WITH code_stats AS (
                SELECT
                    {CODE_SQL} AS code,
                    ANY_VALUE({LABEL_SQL}) AS label,
                    ANY_VALUE({EVENT_TYPE_SQL}) AS event_type,
                    ANY_VALUE({EVENT_GROUP_SQL}) AS "group",
                    COUNT(*)::BIGINT AS count,
                    COUNT(DISTINCT uid)::BIGINT AS user_count,
                    COUNT(DISTINCT session_id)::BIGINT AS session_count,
                    MIN(ts) AS first_seen,
                    MAX(ts) AS last_seen
                FROM events_ordered
                WHERE uid IS NOT NULL AND TRIM(CAST(uid AS VARCHAR)) <> ''
                GROUP BY code
            )
            SELECT
                *,
                ROUND(user_count::DOUBLE / NULLIF(?::DOUBLE, 0), 4)::DOUBLE AS user_rate,
                ROUND(count::DOUBLE / NULLIF(user_count::DOUBLE, 0), 2)::DOUBLE AS events_per_user
            FROM code_stats
            ORDER BY count DESC, user_count DESC, code
            LIMIT ?
            """,
            [user_count, 20],
        ).fetchdf()
    )

    transition_rates = _records(
        con.execute(
            f"""
            WITH transitions AS (
                SELECT
                    uid,
                    {CODE_SQL} AS code,
                    LEAD({CODE_SQL}) OVER (PARTITION BY uid ORDER BY event_order) AS next_code
                FROM events_ordered
                WHERE uid IS NOT NULL AND TRIM(CAST(uid AS VARCHAR)) <> ''
            ),
            transition_stats AS (
                SELECT
                    code AS "from",
                    next_code AS "to",
                    COUNT(*)::BIGINT AS count,
                COUNT(DISTINCT uid)::BIGINT AS user_count
                FROM transitions
                WHERE next_code IS NOT NULL AND next_code <> ''
                GROUP BY code, next_code
            )
            SELECT
                *,
                source_users.user_count AS from_user_count,
                ROUND(transition_stats.user_count::DOUBLE / NULLIF(?::DOUBLE, 0), 4)::DOUBLE AS user_rate,
                ROUND(transition_stats.user_count::DOUBLE / NULLIF(source_users.user_count::DOUBLE, 0), 4)::DOUBLE AS transition_rate
            FROM transition_stats
            LEFT JOIN (
                SELECT {CODE_SQL} AS code, COUNT(DISTINCT uid)::BIGINT AS user_count
                FROM events_ordered
                WHERE uid IS NOT NULL AND TRIM(CAST(uid AS VARCHAR)) <> ''
                GROUP BY code
            ) source_users
              ON source_users.code = transition_stats."from"
            ORDER BY count DESC, transition_stats.user_count DESC, "from", "to"
            LIMIT ?
            """,
            [user_count, 30],
        ).fetchdf()
    )

    participation = top_codes
    loop_patterns = _loop_rows(transition_rates, 8)

    common_paths = _records(
        con.execute(
            f"""
            WITH base AS (
                SELECT
                    uid,
                    event_order,
                    {CODE_SQL} AS code,
                    {LABEL_SQL} AS label,
                    CASE
                        WHEN LAG({CODE_SQL}) OVER (PARTITION BY uid ORDER BY event_order) IS NULL THEN 1
                        WHEN LAG({CODE_SQL}) OVER (PARTITION BY uid ORDER BY event_order) != {CODE_SQL} THEN 1
                        ELSE 0
                    END AS new_run
                FROM events_ordered
                WHERE uid IS NOT NULL AND TRIM(CAST(uid AS VARCHAR)) <> ''
            ),
            runs AS (
                SELECT
                    *,
                    SUM(new_run) OVER (
                        PARTITION BY uid
                        ORDER BY event_order
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS run_id
                FROM base
            ),
            compact AS (
                SELECT
                    uid,
                    run_id,
                    MIN(event_order) AS sort_order,
                    ANY_VALUE(code) AS code,
                    ANY_VALUE(label) AS label
                FROM runs
                GROUP BY uid, run_id
            ),
            numbered AS (
                SELECT
                    uid,
                    code,
                    label,
                    ROW_NUMBER() OVER (PARTITION BY uid ORDER BY sort_order) - 1 AS pos
                FROM compact
            ),
            paths AS (
                SELECT
                    2 AS length,
                    a.uid,
                    a.code || ' -> ' || b.code AS code_path_text,
                    a.label || ' -> ' || b.label AS path_text
                FROM numbered a
                JOIN numbered b ON b.uid = a.uid AND b.pos = a.pos + 1
                UNION ALL
                SELECT
                    3 AS length,
                    a.uid,
                    a.code || ' -> ' || b.code || ' -> ' || c.code AS code_path_text,
                    a.label || ' -> ' || b.label || ' -> ' || c.label AS path_text
                FROM numbered a
                JOIN numbered b ON b.uid = a.uid AND b.pos = a.pos + 1
                JOIN numbered c ON c.uid = a.uid AND c.pos = a.pos + 2
                UNION ALL
                SELECT
                    4 AS length,
                    a.uid,
                    a.code || ' -> ' || b.code || ' -> ' || c.code || ' -> ' || d.code AS code_path_text,
                    a.label || ' -> ' || b.label || ' -> ' || c.label || ' -> ' || d.label AS path_text
                FROM numbered a
                JOIN numbered b ON b.uid = a.uid AND b.pos = a.pos + 1
                JOIN numbered c ON c.uid = a.uid AND c.pos = a.pos + 2
                JOIN numbered d ON d.uid = a.uid AND d.pos = a.pos + 3
            ),
            path_stats AS (
                SELECT
                    code_path_text,
                    ANY_VALUE(path_text) AS path_text,
                    length,
                    COUNT(*)::BIGINT AS occurrence_count,
                    COUNT(DISTINCT uid)::BIGINT AS user_count
                FROM paths
                GROUP BY code_path_text, length
            )
            SELECT
                code_path_text,
                path_text,
                length,
                occurrence_count,
                user_count,
                ROUND(user_count::DOUBLE / NULLIF(?::DOUBLE, 0), 4)::DOUBLE AS user_rate
            FROM path_stats
            ORDER BY user_count DESC, length DESC, occurrence_count DESC, code_path_text
            LIMIT ?
            """,
            [user_count, 12],
        ).fetchdf()
    )

    content_participation = _records(
        con.execute(
            f"""
            WITH scoped AS (
                SELECT
                    uid,
                    session_id,
                    ts,
                    {CONTENT_SQL} AS "group",
                    {CODE_SQL} AS code
                FROM events_ordered
                WHERE uid IS NOT NULL AND TRIM(CAST(uid AS VARCHAR)) <> ''
            ),
            content_stats AS (
                SELECT
                    "group",
                    COUNT(*)::BIGINT AS event_count,
                    COUNT(DISTINCT uid)::BIGINT AS user_count,
                    COUNT(DISTINCT session_id)::BIGINT AS session_count,
                    COUNT(DISTINCT code)::BIGINT AS code_count,
                    MIN(ts) AS first_seen,
                    MAX(ts) AS last_seen
                FROM scoped
                WHERE "group" IS NOT NULL AND TRIM(CAST("group" AS VARCHAR)) <> ''
                GROUP BY "group"
            )
            SELECT
                *,
                ROUND(user_count::DOUBLE / NULLIF(?::DOUBLE, 0), 4)::DOUBLE AS user_rate,
                ROUND(event_count::DOUBLE / NULLIF(user_count::DOUBLE, 0), 2)::DOUBLE AS events_per_user
            FROM content_stats
            ORDER BY user_count DESC, event_count DESC, "group"
            LIMIT ?
            """,
            [user_count, 12],
        ).fetchdf()
    )

    entry_events = _records(
        con.execute(
            f"""
            WITH ranked AS (
                SELECT
                    uid,
                    {CODE_SQL} AS code,
                    {LABEL_SQL} AS label,
                    {EVENT_TYPE_SQL} AS event_type,
                    {EVENT_GROUP_SQL} AS "group",
                    ROW_NUMBER() OVER (PARTITION BY uid ORDER BY event_order) AS rank
                FROM events_ordered
                WHERE uid IS NOT NULL AND TRIM(CAST(uid AS VARCHAR)) <> ''
            ),
            entry_stats AS (
                SELECT
                    code,
                    ANY_VALUE(label) AS label,
                    ANY_VALUE(event_type) AS event_type,
                    ANY_VALUE("group") AS "group",
                    COUNT(DISTINCT uid)::BIGINT AS user_count
                FROM ranked
                WHERE rank = 1
                GROUP BY code
            )
            SELECT
                *,
                ROUND(user_count::DOUBLE / NULLIF(?::DOUBLE, 0), 4)::DOUBLE AS user_rate
            FROM entry_stats
            ORDER BY user_count DESC, code
            LIMIT ?
            """,
            [user_count, 8],
        ).fetchdf()
    )

    exit_events = _records(
        con.execute(
            f"""
            WITH ranked AS (
                SELECT
                    uid,
                    {CODE_SQL} AS code,
                    {LABEL_SQL} AS label,
                    {EVENT_TYPE_SQL} AS event_type,
                    {EVENT_GROUP_SQL} AS "group",
                    ROW_NUMBER() OVER (PARTITION BY uid ORDER BY event_order DESC) AS rank
                FROM events_ordered
                WHERE uid IS NOT NULL AND TRIM(CAST(uid AS VARCHAR)) <> ''
            ),
            exit_stats AS (
                SELECT
                    code,
                    ANY_VALUE(label) AS label,
                    ANY_VALUE(event_type) AS event_type,
                    ANY_VALUE("group") AS "group",
                    COUNT(DISTINCT uid)::BIGINT AS user_count
                FROM ranked
                WHERE rank = 1
                GROUP BY code
            )
            SELECT
                *,
                ROUND(user_count::DOUBLE / NULLIF(?::DOUBLE, 0), 4)::DOUBLE AS user_rate
            FROM exit_stats
            ORDER BY user_count DESC, code
            LIMIT ?
            """,
            [user_count, 8],
        ).fetchdf()
    )
    outliers = _outlier_rows(participation, transition_rates, loop_patterns, exit_events, int(user_count or 0))

    top_users = con.execute(
        """
        SELECT
            uid,
            COUNT(*)::BIGINT AS event_count,
            COUNT(DISTINCT session_id)::BIGINT AS session_count,
            MIN(ts) AS first_seen,
            MAX(ts) AS last_seen
        FROM events_ordered
        WHERE uid IS NOT NULL AND TRIM(CAST(uid AS VARCHAR)) <> ''
        GROUP BY uid
        ORDER BY event_count DESC, uid
        LIMIT ?
        """,
        [sample_limit],
    ).fetchdf()

    users: list[dict[str, Any]] = []
    for row in top_users.to_dict("records"):
        uid = str(row["uid"])
        sequence, omitted = _sequence_from_sql(con, "uid = ?", [uid], sequence_limit)
        top_user_codes = _records(
            con.execute(
                f"""
                SELECT
                    {CODE_SQL} AS code,
                    ANY_VALUE({LABEL_SQL}) AS label,
                    COUNT(*)::BIGINT AS count,
                    COUNT(DISTINCT uid)::BIGINT AS user_count,
                    MIN(ts) AS first_seen,
                    MAX(ts) AS last_seen
                FROM events_ordered
                WHERE uid = ?
                GROUP BY code
                ORDER BY count DESC, code
                LIMIT 5
                """,
                [uid],
            ).fetchdf()
        )
        first_seen = row.get("first_seen")
        last_seen = row.get("last_seen")
        duration_sec = 0
        if pd.notna(first_seen) and pd.notna(last_seen):
            duration_sec = max(0, int((last_seen - first_seen).total_seconds()))
        users.append(
            {
                "uid": uid,
                "event_count": int(row.get("event_count") or 0),
                "session_count": int(row.get("session_count") or 0),
                "first_seen": first_seen,
                "last_seen": last_seen,
                "duration_sec": duration_sec,
                "top_codes": top_user_codes,
                "sequence": sequence,
                "sequence_text": _sequence_text(sequence, omitted),
                "omitted_sequence_runs": omitted,
            }
        )

    top_sessions = con.execute(
        """
        SELECT
            session_id,
            ANY_VALUE(uid) AS uid,
            COUNT(*)::BIGINT AS event_count,
            MIN(ts) AS start,
            MAX(ts) AS "end"
        FROM events_ordered
        WHERE session_id IS NOT NULL AND TRIM(CAST(session_id AS VARCHAR)) <> ''
        GROUP BY session_id
        ORDER BY event_count DESC, session_id
        LIMIT ?
        """,
        [sample_limit],
    ).fetchdf()

    sessions: list[dict[str, Any]] = []
    for row in top_sessions.to_dict("records"):
        session_id = str(row["session_id"])
        sequence, omitted = _sequence_from_sql(con, "session_id = ?", [session_id], sequence_limit)
        start = row.get("start")
        end = row.get("end")
        duration_sec = 0
        if pd.notna(start) and pd.notna(end):
            duration_sec = max(0, int((end - start).total_seconds()))
        sessions.append(
            {
                "session_id": session_id,
                "uid": str(row.get("uid")),
                "event_count": int(row.get("event_count") or 0),
                "start": start,
                "end": end,
                "duration_sec": duration_sec,
                "sequence": sequence,
                "sequence_text": _sequence_text(sequence, omitted),
                "omitted_sequence_runs": omitted,
            }
        )

    return _jsonable(
        {
            "event_count": int(event_count or 0),
            "user_count": int(user_count or 0),
            "top_codes": top_codes[:12],
            "participation": participation,
            "content_participation": content_participation,
            "common_paths": common_paths,
            "transition_rates": transition_rates,
            "loop_patterns": loop_patterns,
            "entry_events": entry_events,
            "exit_events": exit_events,
            "stop_points": exit_events,
            "outliers": outliers,
            "top_transitions": transition_rates[:12],
            "users": users,
            "sessions": sessions,
            "note": "코드 의미는 확정하지 않고, 전체 유저의 참여율과 공통 흐름을 보여줍니다.",
        }
    )
