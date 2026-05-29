from __future__ import annotations

from typing import Any

import pandas as pd

from .config import FieldMapping, LanguageConfig
from .language import classify_event, infer_event_type_from_shape, infer_fields, label_content, label_product


STANDARD_COLUMNS = [
    "uid",
    "ts",
    "event_raw",
    "event_label",
    "event_type",
    "group",
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
]


def _series_or_default(frame: pd.DataFrame, column: str | None, default: Any = None) -> pd.Series:
    if column and column in frame.columns:
        return frame[column]
    return pd.Series([default] * len(frame), index=frame.index)


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0)


def normalize_frame(frame: pd.DataFrame, config: LanguageConfig) -> tuple[pd.DataFrame, FieldMapping]:
    fields = infer_fields(frame, config)
    normalized = pd.DataFrame(index=frame.index)
    uid_values = _series_or_default(frame, fields.uid, "")
    normalized["uid"] = uid_values.where(uid_values.notna(), "").astype(str).str.strip()
    normalized["ts"] = pd.to_datetime(_series_or_default(frame, fields.timestamp, pd.NaT), errors="coerce")

    event_series = _series_or_default(frame, fields.event)
    if event_series.isna().all():
        event_series = pd.Series(["event"] * len(frame), index=frame.index)
    normalized["event_raw"] = event_series.fillna("event").astype(str)

    normalized["content_id"] = _series_or_default(frame, fields.content_id, None)
    normalized["product_id"] = _series_or_default(frame, fields.product_id, None)
    normalized["amount"] = _numeric(_series_or_default(frame, fields.amount, 0))
    normalized["duration_sec"] = _numeric(_series_or_default(frame, fields.duration_sec, 0))
    normalized["wait_time_sec"] = _numeric(_series_or_default(frame, fields.wait_time_sec, 0))
    normalized["result"] = _series_or_default(frame, fields.result, None)
    normalized["source_file"] = _series_or_default(frame, "_source_file", "unknown")

    classifications: dict[str, dict[str, Any]] = {}
    for raw_value in normalized["event_raw"].dropna().unique():
        classifications[str(raw_value)] = classify_event(str(raw_value), config)

    labels = normalized["event_raw"].map(lambda value: classifications[str(value)]["label"])
    event_types = normalized["event_raw"].map(lambda value: classifications[str(value)]["event_type"])
    groups = []
    sources = []
    for _, row in normalized.iterrows():
        classification = classify_event(str(row["event_raw"]), config, row["content_id"])
        groups.append(classification["group"])
        sources.append(classification["source"])
    normalized["event_label"] = labels
    normalized["event_type"] = event_types
    normalized["group"] = groups
    normalized["language_source"] = sources

    content_labels = normalized.apply(
        lambda row: label_content(row["content_id"], row["group"], config), axis=1, result_type="expand"
    )
    normalized["content_label"] = content_labels[0]
    normalized["content_type"] = content_labels[1]
    inferred_with_content_label = (
        (normalized["language_source"] != "dictionary")
        & normalized["content_label"].notna()
        & (normalized["content_label"].astype(str) != "")
    )
    normalized.loc[inferred_with_content_label, "group"] = normalized.loc[
        inferred_with_content_label,
        "content_label",
    ]
    normalized["event_type"] = normalized.apply(
        lambda row: infer_event_type_from_shape(
            row["event_type"],
            product_id=row["product_id"],
            amount=row["amount"],
            wait_time_sec=row["wait_time_sec"],
            result=row["result"],
            content_id=row["content_id"],
            group=row["group"],
        ),
        axis=1,
    )

    product_labels = normalized["product_id"].map(lambda value: label_product(value, config))
    normalized["product_label"] = product_labels.map(lambda value: value[0])
    normalized["product_category"] = product_labels.map(lambda value: value[1])

    normalized = normalized[STANDARD_COLUMNS]
    return normalized, fields


def normalize(frames: list[pd.DataFrame], config: LanguageConfig) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    normalized_frames: list[pd.DataFrame] = []
    field_reports: list[dict[str, Any]] = []
    for frame in frames:
        normalized, fields = normalize_frame(frame, config)
        normalized_frames.append(normalized)
        field_reports.append(
            {
                "source_file": str(frame["_source_file"].iloc[0]) if "_source_file" in frame else "unknown",
                "rows": int(len(frame)),
                "fields": fields.to_dict(),
            }
        )
    events = pd.concat(normalized_frames, ignore_index=True)
    events = events[events["uid"].notna() & (events["uid"].astype(str) != "")]
    events = events.sort_values(["uid", "ts"], na_position="last").reset_index(drop=True)
    return events, field_reports
