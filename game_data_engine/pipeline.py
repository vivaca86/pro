from __future__ import annotations

from pathlib import Path
from typing import Any
import json

import pandas as pd

from .config import LanguageConfig
from .diagnosis import build_alerts, diagnose_content, diagnose_revenue
from .ingest import ingest
from .journey import (
    add_sessions,
    build_failure_contexts,
    build_purchase_contexts,
    build_session_flows,
    build_user_journeys,
)
from .language import build_language_suggestions
from .metrics import (
    content_health,
    daily_summary,
    product_performance,
    segment_compare,
    whale_concentration,
)
from .normalize import normalize


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
    if pd.isna(value) if not isinstance(value, (list, dict, tuple)) else False:
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _records(frame: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    selected = frame.head(limit) if limit else frame
    return [_jsonable(row) for row in selected.to_dict("records")]


def assess_quality(raw_frames: list[pd.DataFrame], events: pd.DataFrame, field_reports: list[dict[str, Any]]) -> dict[str, Any]:
    missing_uid = int((events["uid"].astype(str) == "").sum()) if not events.empty else 0
    missing_ts = int(events["ts"].isna().sum()) if not events.empty else 0
    duplicate_events = int(events.duplicated(subset=["uid", "ts", "event_raw", "content_id", "product_id", "amount"]).sum())
    inferred_language = int((events["language_source"] != "dictionary").sum()) if not events.empty else 0
    return {
        "raw_files": [
            {
                "source_file": str(frame["_source_file"].iloc[0]) if "_source_file" in frame else "unknown",
                "rows": int(len(frame)),
                "columns": [str(column) for column in frame.columns if not str(column).startswith("_source")],
            }
            for frame in raw_frames
        ],
        "normalized_rows": int(len(events)),
        "field_reports": field_reports,
        "missing_uid_rows": missing_uid,
        "missing_timestamp_rows": missing_ts,
        "duplicate_event_rows": duplicate_events,
        "inferred_language_rows": inferred_language,
        "quality_score": max(
            0,
            round(
                1
                - (
                    (missing_uid / max(len(events), 1)) * 0.35
                    + (missing_ts / max(len(events), 1)) * 0.25
                    + (duplicate_events / max(len(events), 1)) * 0.2
                    + (inferred_language / max(len(events), 1)) * 0.2
                ),
                3,
            ),
        ),
    }


def run_pipeline(
    inputs: list[str | Path],
    dictionary_path: str | Path | None = None,
    out: str | Path | None = None,
    normalized_out: str | Path | None = None,
) -> dict[str, Any]:
    config = LanguageConfig.load(dictionary_path)
    raw_frames = ingest(inputs)
    events, field_reports = normalize(raw_frames, config)
    events = add_sessions(events, config.session_gap_minutes)

    sessions = build_session_flows(events)
    journeys = build_user_journeys(events)
    failures = build_failure_contexts(events)
    purchases = build_purchase_contexts(events)

    summary = daily_summary(events, sessions)
    content = content_health(events, failures)
    products = product_performance(events, purchases)
    segments = segment_compare(events)
    concentration = whale_concentration(events)
    diagnosis = {
        "content": diagnose_content(content),
        "revenue": diagnose_revenue(summary, products, purchases, concentration),
    }
    payload = {
        "engine_version": "0.1.0",
        "data_quality": assess_quality(raw_frames, events, field_reports),
        "summary": summary,
        "language": {
            "suggestions": build_language_suggestions(events),
            "needs_confirmation_count": int((events["language_source"] != "dictionary").sum()),
        },
        "journeys": {
            "user_count": int(len(journeys)),
            "sample": _records(journeys, 20),
        },
        "sessions": {
            "session_count": int(len(sessions)),
            "sample": _records(sessions, 20),
        },
        "failure_contexts": _records(failures),
        "purchase_contexts": _jsonable(purchases),
        "content_health": _records(content),
        "product_performance": _records(products),
        "segments": _jsonable(segments),
        "revenue_concentration": _jsonable(concentration),
        "diagnosis": _jsonable(diagnosis),
        "alerts": build_alerts(diagnosis),
        "ai_context": {
            "briefing_rules": [
                "Do not claim a definitive cause from one-day data.",
                "Explain cause candidates using evidence numbers.",
                "Separate confirmed facts, likely hypotheses, and missing data.",
                "Prefer UID journey, session flow, and purchase context over raw count-only claims.",
            ],
            "top_alerts": build_alerts(diagnosis),
        },
    }

    if normalized_out:
        normalized_path = Path(normalized_out)
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        events.to_csv(normalized_path, index=False, encoding="utf-8-sig")

    if out:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as handle:
            json.dump(_jsonable(payload), handle, ensure_ascii=False, indent=2)
    return _jsonable(payload)
