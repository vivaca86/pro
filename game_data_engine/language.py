from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .config import FieldMapping, LanguageConfig


FIELD_ALIASES = {
    "uid": ["uid", "user_id", "userid", "player_id", "account_id", "member_id"],
    "timestamp": ["event_time", "timestamp", "time", "created_at", "log_dt", "dt", "date"],
    "event": ["event_name", "event", "action", "action_type", "log_name", "event_type"],
    "content_id": ["content_id", "stage_id", "stage_no", "dungeon_id", "raid_id", "level_id", "chapter_id"],
    "product_id": ["product_id", "package_id", "sku", "shop_item", "item_id", "goods_id"],
    "amount": ["amount", "price", "paid_amount", "revenue", "purchase_value", "cash_amount"],
    "duration_sec": ["duration_sec", "duration", "play_time", "stay_time", "elapsed_sec"],
    "wait_time_sec": ["wait_time", "wait_time_sec", "matching_wait", "queue_time", "queue_sec"],
    "result": ["result", "status", "outcome", "win_lose", "success"],
}

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
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


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


def infer_fields(frame: pd.DataFrame, config: LanguageConfig) -> FieldMapping:
    columns = [str(column) for column in frame.columns]
    explicit = config.fields
    guessed = FieldMapping()
    for field_name, aliases in FIELD_ALIASES.items():
        configured = getattr(explicit, field_name)
        value = configured if configured in frame.columns else guess_field(columns, aliases)
        setattr(guessed, field_name, value)
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
