from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .config import FieldMapping, LanguageConfig


FIELD_ALIASES = {
    "uid": [
        "uid",
        "user_id",
        "userid",
        "player_id",
        "account_id",
        "member_id",
        "account_no",
        "acct",
        "user_no",
        "player_no",
        "character_id",
        "char_id",
        "유저ID",
        "유저아이디",
        "사용자ID",
        "계정ID",
        "회원ID",
        "캐릭터ID",
    ],
    "timestamp": [
        "event_time",
        "timestamp",
        "time",
        "created_at",
        "datetime",
        "log_dt",
        "log_at",
        "log_time",
        "event_dt",
        "occurred_at",
        "reg_dt",
        "dt",
        "date",
        "일시",
        "시간",
        "로그시간",
        "이벤트시간",
        "발생시간",
        "생성일시",
        "등록일시",
    ],
    "event": [
        "event_name",
        "event",
        "action",
        "action_type",
        "logtype",
        "log_type",
        "event_code",
        "e_code",
        "log_name",
        "log_code",
        "log_id",
        "event_type",
        "behavior",
        "activity",
        "이벤트명",
        "이벤트",
        "로그명",
        "로그코드",
        "행동",
        "액션",
    ],
    "content_id": [
        "content_id",
        "content",
        "mode",
        "area",
        "place",
        "stage_id",
        "stage_no",
        "dungeon_id",
        "raid_id",
        "level_id",
        "chapter_id",
        "map_id",
        "mode_id",
        "콘텐츠ID",
        "컨텐츠ID",
        "스테이지ID",
        "던전ID",
        "레이드ID",
        "레벨ID",
        "챕터ID",
    ],
    "product_id": [
        "product_id",
        "package_id",
        "sku",
        "shop_item",
        "item_id",
        "goods_id",
        "상품ID",
        "패키지ID",
        "아이템ID",
        "상점아이템",
        "상품코드",
    ],
    "amount": [
        "amount",
        "price",
        "paid_amount",
        "revenue",
        "purchase_value",
        "cash_amount",
        "krw",
        "won",
        "currency",
        "sales",
        "cost",
        "금액",
        "가격",
        "매출",
        "결제금액",
        "구매금액",
        "판매금액",
    ],
    "duration_sec": [
        "duration_sec",
        "duration",
        "play_time",
        "stay_time",
        "elapsed_sec",
        "elapsed_time",
        "소요시간",
        "플레이시간",
        "체류시간",
        "진행시간",
    ],
    "wait_time_sec": [
        "wait_time",
        "wait_time_sec",
        "matching_wait",
        "match_wait",
        "queue_time",
        "queue_sec",
        "대기시간",
        "매칭대기",
        "매칭대기시간",
        "큐시간",
    ],
    "result": [
        "result",
        "status",
        "state",
        "outcome",
        "win_lose",
        "success",
        "result_code",
        "결과",
        "상태",
        "성공여부",
        "승패",
    ],
}

FAIL_RESULTS = {"fail", "failed", "failure", "lose", "loss", "lost", "error", "drop", "cancel", "abandon", "실패", "패배", "이탈"}
SUCCESS_RESULTS = {"success", "succeed", "succeeded", "clear", "cleared", "win", "won", "complete", "completed", "성공", "클리어", "승리", "완료"}
TIMEOUT_RESULTS = {"timeout", "timedout", "time_out", "시간초과", "타임아웃"}

TOKEN_LABELS = {
    "evt": "",
    "event": "",
    "pvp": "PvP",
    "arena": "아레나",
    "raid": "레이드",
    "stage": "스테이지",
    "stg": "스테이지",
    "tutorial": "튜토리얼",
    "shop": "상점",
    "pkg": "패키지",
    "package": "패키지",
    "starter": "초보자",
    "gem": "유료 재화",
    "gold": "골드",
    "match": "매칭",
    "wait": "대기",
    "timeout": "시간 초과",
    "enter": "입장",
    "start": "시작",
    "end": "종료",
    "exit": "종료",
    "drop": "이탈",
    "clear": "클리어",
    "fail": "실패",
    "failed": "실패",
    "success": "성공",
    "win": "승리",
    "lose": "패배",
    "loss": "패배",
    "buy": "구매",
    "purchase": "구매",
    "pay": "결제",
    "iap": "결제",
    "view": "노출",
    "click": "클릭",
    "reward": "보상",
    "claim": "수령",
    "spend": "사용",
    "gain": "획득",
    "use": "사용",
}


def normalized_name(name: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]+", "", str(name).lower())


def guess_field(columns: list[str], aliases: list[str]) -> str | None:
    normalized_columns = {normalized_name(column): column for column in columns}
    for alias in aliases:
        key = normalized_name(alias)
        if key in normalized_columns:
            return normalized_columns[key]
    for column in columns:
        column_key = normalized_name(column)
        if any(normalized_name(alias) in column_key for alias in aliases):
            return column
    return None


def _sample_text(series: pd.Series, limit: int = 1000) -> pd.Series:
    return series.dropna().astype(str).str.strip().replace({"": pd.NA}).dropna().head(limit)


def _numeric_ratio(series: pd.Series) -> float:
    sample = _sample_text(series)
    if sample.empty:
        return 0.0
    return float(pd.to_numeric(sample, errors="coerce").notna().mean())


def _datetime_ratio(series: pd.Series) -> float:
    sample = _sample_text(series)
    if sample.empty:
        return 0.0
    date_like = sample.str.contains(r"\d{4}[-/년]|\d{1,2}:\d{2}", regex=True).mean()
    if date_like < 0.4:
        return 0.0
    return float(pd.to_datetime(sample, errors="coerce").notna().mean())


def _result_ratio(series: pd.Series) -> float:
    sample = _sample_text(series)
    if sample.empty:
        return 0.0
    normalized = sample.str.lower().str.replace(r"[^a-z0-9가-힣]+", "", regex=True)
    known = FAIL_RESULTS | SUCCESS_RESULTS | TIMEOUT_RESULTS | {"true", "false", "yes", "no", "y", "n", "0", "1"}
    return float(normalized.isin(known).mean())


def _text_signal_ratio(series: pd.Series) -> float:
    sample = _sample_text(series)
    if sample.empty:
        return 0.0
    return float(sample.str.contains(r"[A-Za-z가-힣_]", regex=True).mean())


def _unique_ratio(series: pd.Series) -> float:
    sample = _sample_text(series)
    if sample.empty:
        return 0.0
    return float(sample.nunique(dropna=True) / len(sample))


def _unused_columns(frame: pd.DataFrame, guessed: FieldMapping) -> list[str]:
    used = {value for value in guessed.to_dict().values() if value}
    return [str(column) for column in frame.columns if str(column) not in used]


def _best_column(frame: pd.DataFrame, candidates: list[str], scorer: Any, minimum: float) -> str | None:
    scored = []
    for column in candidates:
        score = scorer(frame[column])
        if score >= minimum:
            scored.append((score, column))
    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][1]


def _date_part_column(name: Any) -> bool:
    key = normalized_name(str(name))
    if key in {"year", "yyyy", "yy", "month", "mm", "day", "dd", "dt", "date"}:
        return True
    return "date" in key or "time" in key


def _non_amount_identifier_column(name: Any) -> bool:
    key = normalized_name(str(name))
    if key in {"game", "gameid", "logtype", "logid", "logcode", "ecode", "type", "code"}:
        return True
    return key.endswith("id") or key.endswith("code") or key.endswith("type")


def _infer_fields_from_values(frame: pd.DataFrame, guessed: FieldMapping) -> None:
    if frame.empty:
        return

    if not guessed.timestamp:
        guessed.timestamp = _best_column(frame, _unused_columns(frame, guessed), _datetime_ratio, 0.7)

    if not guessed.result:
        guessed.result = _best_column(frame, _unused_columns(frame, guessed), _result_ratio, 0.45)

    if not guessed.uid:
        def uid_score(series: pd.Series) -> float:
            sample = _sample_text(series)
            if sample.empty or _numeric_ratio(series) > 0.95 or _datetime_ratio(series) > 0.2:
                return 0.0
            return _unique_ratio(series) * _text_signal_ratio(series)

        guessed.uid = _best_column(frame, _unused_columns(frame, guessed), uid_score, 0.55)

    if not guessed.event:
        def event_score(series: pd.Series) -> float:
            sample = _sample_text(series)
            if sample.empty or _numeric_ratio(series) > 0.8 or _datetime_ratio(series) > 0.2:
                return 0.0
            unique = sample.nunique(dropna=True)
            if unique < 2:
                return 0.0
            unique_ratio = unique / len(sample)
            if unique_ratio > 0.55:
                return 0.0
            return _text_signal_ratio(series) * (1 - abs(unique_ratio - 0.08))

        guessed.event = _best_column(frame, _unused_columns(frame, guessed), event_score, 0.45)

    if not guessed.amount:
        def amount_score(series: pd.Series) -> float:
            if _date_part_column(series.name) or _non_amount_identifier_column(series.name):
                return 0.0
            sample = pd.to_numeric(_sample_text(series), errors="coerce").dropna()
            if sample.empty:
                return 0.0
            positive_ratio = float((sample > 0).mean())
            return _numeric_ratio(series) * positive_ratio

        guessed.amount = _best_column(frame, _unused_columns(frame, guessed), amount_score, 0.25)


def infer_fields(frame: pd.DataFrame, config: LanguageConfig) -> FieldMapping:
    columns = [str(column) for column in frame.columns]
    explicit = config.fields
    guessed = FieldMapping()
    for field_name, aliases in FIELD_ALIASES.items():
        configured = getattr(explicit, field_name)
        value = configured if configured in frame.columns else guess_field(columns, aliases)
        setattr(guessed, field_name, value)
    _infer_fields_from_values(frame, guessed)
    return guessed


def split_tokens(raw: str) -> list[str]:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(raw))
    return [token.lower() for token in re.split(r"[^A-Za-z0-9가-힣]+", snake) if token]


def infer_group(raw: str, content_id: Any = None) -> str | None:
    text = " ".join(split_tokens(raw))
    candidates = [
        ("arena", "아레나"),
        ("pvp", "아레나"),
        ("raid", "레이드"),
        ("tutorial", "튜토리얼"),
        ("stage", "스테이지"),
        ("stg", "스테이지"),
        ("dungeon", "던전"),
        ("shop", "상점"),
        ("pkg", "상품"),
        ("package", "상품"),
        ("gacha", "가챠"),
    ]
    for token, group in candidates:
        if token in text:
            return group
    if content_id is not None and str(content_id) and str(content_id).lower() != "nan":
        return str(content_id)
    return None


def infer_event_type(raw: str) -> str:
    tokens = set(split_tokens(raw))
    text = "_".join(tokens)
    if {"purchase", "buy", "pay", "iap"} & tokens:
        return "purchase"
    if "click" in tokens:
        return "product_click" if {"pkg", "package", "shop", "product"} & tokens else "click"
    if "view" in tokens:
        return "product_view" if {"pkg", "package", "shop", "product"} & tokens else "view"
    if {"timeout"} & tokens or ("wait" in tokens and "match" in tokens):
        return "match_issue"
    if {"fail", "failed", "lose", "loss"} & tokens:
        return "content_fail"
    if {"clear", "success", "win"} & tokens:
        return "content_success"
    if {"reward", "claim"} <= tokens or "claim" in tokens:
        return "reward_claim"
    if "spend" in tokens:
        return "currency_spend"
    if {"gain", "earn"} & tokens:
        return "currency_gain"
    if {"enter", "start", "open"} & tokens:
        return "content_enter" if infer_group(text) else "session_start"
    if {"exit", "end", "drop"} & tokens:
        return "exit"
    return "event"


def infer_event_type_from_shape(
    current_event_type: str,
    product_id: Any = None,
    amount: Any = 0,
    wait_time_sec: Any = 0,
    result: Any = None,
    content_id: Any = None,
    group: Any = None,
) -> str:
    if current_event_type != "event":
        return current_event_type

    product_text = "" if product_id is None or pd.isna(product_id) else str(product_id).strip()
    content_text = "" if content_id is None or pd.isna(content_id) else str(content_id).strip()
    group_text = "" if group is None or pd.isna(group) else str(group).strip()
    result_text = "" if result is None or pd.isna(result) else str(result).strip().lower()
    result_key = re.sub(r"[^a-z0-9가-힣]+", "", result_text)
    numeric_amount = pd.to_numeric(pd.Series([amount]), errors="coerce").fillna(0).iloc[0]
    numeric_wait = pd.to_numeric(pd.Series([wait_time_sec]), errors="coerce").fillna(0).iloc[0]
    has_product = bool(product_text and product_text.lower() != "nan")
    has_content = bool((content_text and content_text.lower() != "nan") or (group_text and group_text.lower() != "nan"))

    if has_product and float(numeric_amount) > 0:
        return "purchase"
    if float(numeric_wait) > 0 or result_key in TIMEOUT_RESULTS:
        return "match_issue"
    if has_content and result_key in FAIL_RESULTS:
        return "content_fail"
    if has_content and result_key in SUCCESS_RESULTS:
        return "content_success"
    if has_product:
        return "product_view"
    if has_content:
        return "content_enter"
    return current_event_type


def readable_label(raw: str) -> str:
    tokens = split_tokens(raw)
    labels = [TOKEN_LABELS.get(token, token) for token in tokens]
    labels = [label for label in labels if label]
    if not labels:
        return str(raw)
    return " ".join(labels)


def classify_event(raw: str, config: LanguageConfig, content_id: Any = None) -> dict[str, Any]:
    key = str(raw)
    configured = config.event_labels.get(key)
    if configured:
        return {
            "label": configured.get("label", key),
            "event_type": configured.get("event_type", infer_event_type(key)),
            "group": configured.get("group", infer_group(key, content_id)),
            "confidence": float(configured.get("confidence", 1.0)),
            "source": "dictionary",
        }
    event_type = infer_event_type(key)
    return {
        "label": readable_label(key),
        "event_type": event_type,
        "group": infer_group(key, content_id),
        "confidence": 0.55 if event_type == "event" else 0.75,
        "source": "inferred",
    }


def label_content(content_id: Any, group: str | None, config: LanguageConfig) -> tuple[str | None, str | None]:
    if content_id is None or pd.isna(content_id) or str(content_id) == "":
        return group, None
    key = str(content_id)
    configured = config.content_labels.get(key)
    if configured:
        return configured.get("label", key), configured.get("type")
    return group or key, None


def label_product(product_id: Any, config: LanguageConfig) -> tuple[str | None, str | None]:
    if product_id is None or pd.isna(product_id) or str(product_id) == "":
        return None, None
    key = str(product_id)
    configured = config.product_labels.get(key)
    if configured:
        return configured.get("label", key), configured.get("category")
    return readable_label(key), None


def build_language_suggestions(events: pd.DataFrame) -> list[dict[str, Any]]:
    if events.empty:
        return []
    grouped = (
        events.groupby(["event_raw", "event_label", "event_type", "group", "language_source"], dropna=False)
        .size()
        .reset_index(name="count")
        .sort_values(["language_source", "count"], ascending=[False, False])
    )
    suggestions: list[dict[str, Any]] = []
    for row in grouped.to_dict("records"):
        needs_confirmation = row["language_source"] != "dictionary" or row["event_type"] == "event"
        suggestions.append(
            {
                "raw": row["event_raw"],
                "suggested_label": row["event_label"],
                "event_type": row["event_type"],
                "group": None if pd.isna(row["group"]) else row["group"],
                "count": int(row["count"]),
                "needs_confirmation": bool(needs_confirmation),
            }
        )
    return suggestions
