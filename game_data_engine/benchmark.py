from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any
import csv
import json

from .pipeline import run_pipeline


EVENT_PATTERNS = [
    ("login", "", "", 0, 0, 0, "success"),
    ("arena_enter", "arena", "", 0, 120, 8, "start"),
    ("arena_match_wait_timeout", "arena", "", 0, 0, 54, "fail"),
    ("arena_win", "arena", "", 0, 210, 0, "success"),
    ("raid_enter", "raid", "", 0, 180, 0, "start"),
    ("raid_clear_fail", "raid", "", 0, 360, 0, "fail"),
    ("raid_clear", "raid", "", 0, 420, 0, "success"),
    ("shop_view", "", "", 0, 30, 0, "view"),
    ("pkg_starter_buy", "", "starter_pack", 9900, 0, 0, "success"),
    ("pkg_raid_buy", "", "raid_pack", 55000, 0, 0, "success"),
    ("reward_claim", "raid", "", 0, 15, 0, "success"),
]


def generate_synthetic_events(path: str | Path, rows: int, users: int | None = None) -> dict[str, Any]:
    if rows <= 0:
        raise ValueError("rows must be greater than 0")
    user_count = users or min(max(rows // 12, 10), 50_000)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 5, 28, 0, 0, 0)
    headers = [
        "uid",
        "event_time",
        "event_name",
        "content_id",
        "product_id",
        "amount",
        "duration_sec",
        "wait_time_sec",
        "result",
    ]

    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for index in range(rows):
            user_index = index % user_count
            event_round = index // user_count
            uid = f"u{user_index:06d}"
            event_name, content_id, product_id, amount, duration_sec, wait_time_sec, result = EVENT_PATTERNS[
                index % len(EVENT_PATTERNS)
            ]
            timestamp = start + timedelta(seconds=event_round * 7 + user_index % 5)
            writer.writerow(
                [
                    uid,
                    timestamp.isoformat(sep=" "),
                    event_name,
                    content_id,
                    product_id,
                    amount,
                    duration_sec,
                    wait_time_sec,
                    result,
                ]
            )

    return {
        "path": str(target),
        "rows": rows,
        "users": user_count,
        "size_bytes": target.stat().st_size,
    }


def run_benchmark(
    rows: int,
    output_dir: str | Path,
    dictionary_path: str | Path | None = None,
    warehouse_path: str | Path | None = None,
    users: int | None = None,
    sample_limit: int = 5,
    keep_input: bool = True,
) -> dict[str, Any]:
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"bench-{rows}-{stamp}"
    input_path = target_dir / f"{run_id}.csv"
    analysis_path = target_dir / f"{run_id}-analysis.json"
    duckdb_path = Path(warehouse_path) if warehouse_path else target_dir / "benchmark.duckdb"

    started = perf_counter()
    generated = generate_synthetic_events(input_path, rows=rows, users=users)
    generated_at = perf_counter()
    payload = run_pipeline(
        inputs=[input_path],
        dictionary_path=dictionary_path,
        out=analysis_path,
        warehouse_path=duckdb_path,
        run_id=run_id,
        sample_limit=sample_limit,
    )
    finished = perf_counter()

    result = {
        "run_id": run_id,
        "rows": rows,
        "users": generated["users"],
        "input_path": str(input_path),
        "analysis_path": str(analysis_path),
        "warehouse_path": str(duckdb_path),
        "input_size_mb": round(generated["size_bytes"] / 1024 / 1024, 3),
        "generate_sec": round(generated_at - started, 3),
        "pipeline_sec": round(finished - generated_at, 3),
        "total_sec": round(finished - started, 3),
        "summary": payload["summary"],
        "warehouse": payload.get("warehouse", {}),
    }

    if not keep_input:
        input_path.unlink(missing_ok=True)
        result["input_path"] = None

    return result


def print_benchmark(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
