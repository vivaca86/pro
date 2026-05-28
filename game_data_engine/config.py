from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass
class FieldMapping:
    uid: str | None = None
    timestamp: str | None = None
    event: str | None = None
    content_id: str | None = None
    product_id: str | None = None
    amount: str | None = None
    duration_sec: str | None = None
    wait_time_sec: str | None = None
    result: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FieldMapping":
        if not data:
            return cls()
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def to_dict(self) -> dict[str, str | None]:
        return {
            "uid": self.uid,
            "timestamp": self.timestamp,
            "event": self.event,
            "content_id": self.content_id,
            "product_id": self.product_id,
            "amount": self.amount,
            "duration_sec": self.duration_sec,
            "wait_time_sec": self.wait_time_sec,
            "result": self.result,
        }


@dataclass
class LanguageConfig:
    fields: FieldMapping = field(default_factory=FieldMapping)
    event_labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    content_labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    product_labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_gap_minutes: int = 30
    timezone: str = "Asia/Seoul"

    @classmethod
    def load(cls, path: str | Path | None) -> "LanguageConfig":
        if not path:
            return cls()
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return cls(
            fields=FieldMapping.from_dict(data.get("fields")),
            event_labels=data.get("event_labels", {}),
            content_labels=data.get("content_labels", {}),
            product_labels=data.get("product_labels", {}),
            session_gap_minutes=int(data.get("session_gap_minutes", 30)),
            timezone=data.get("timezone", "Asia/Seoul"),
        )
