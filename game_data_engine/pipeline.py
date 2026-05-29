from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
import json

import pandas as pd

from .config import LanguageConfig
from .diagnosis import build_alerts, diagnose_content, diagnose_revenue
from .ingest import RawTable, describe_raw_table, ingest
from .language import build_language_suggestions
from .metrics import (
    daily_summary,
    daily_summary_by_date,
    segment_compare,
    whale_concentration,
)
from .normalize import normalize
from .sql_facts import build_sql_facts
from .sql_metrics import content_health, product_performance
from .sql_normalize import DuckDBNormalizeUnsupported, normalize_csv_with_duckdb
from .sql_staged import build_staged_analysis, export_normalized_events
from .warehouse import store_run, store_staged_run


ProgressCallback = Callable[[float, str, str], None]


def _report_progress(callback: ProgressCallback | None, progress: float, stage: str, message: str) -> None:
    if callback is None:
        return
    callback(max(0.0, min(progress, 1.0)), stage, message)


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


def _artifact_payloads(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary.json": {
            "engine_version": payload["engine_version"],
            "data_quality": payload["data_quality"],
            "summary": payload["summary"],
            "summary_by_date": payload["summary_by_date"],
            "language": payload["language"],
        },
        "issues.json": {
            "diagnosis": payload["diagnosis"],
            "alerts": payload["alerts"],
            "ai_context": payload["ai_context"],
        },
        "journeys_sample.json": payload["journeys"],
        "sessions_sample.json": payload["sessions"],
        "content_health.json": payload["content_health"],
        "product_performance.json": payload["product_performance"],
        "purchase_contexts.json": payload["purchase_contexts"],
    }


def _ingest_and_normalize(
    inputs: list[str | Path],
    config: LanguageConfig,
) -> tuple[list[RawTable], pd.DataFrame, list[dict[str, Any]]]:
    try:
        return normalize_csv_with_duckdb(inputs, config)
    except DuckDBNormalizeUnsupported as exc:
        fallback_reason = str(exc)
    except Exception as exc:
        fallback_reason = f"DuckDB normalize failed: {exc}"

    raw_frames = ingest(inputs)
    events, field_reports = normalize(raw_frames, config)
    for report in field_reports:
        report["normalize_engine"] = "pandas"
        report["duckdb_fallback_reason"] = fallback_reason
    return raw_frames, events, field_reports


def _staging_db_path(
    warehouse_path: str | Path | None,
    out: str | Path | None,
    run_id: str | None,
) -> Path:
    identifier = run_id or f"manual-{pd.Timestamp.now().strftime('%Y%m%d-%H%M%S')}"
    if warehouse_path:
        warehouse_parent = Path(warehouse_path).parent
        root = warehouse_parent.parent / "staging" if warehouse_parent.name == "warehouse" else warehouse_parent / "staging"
    elif out:
        root = Path(out).parent / "staging"
    else:
        root = Path("data") / "staging"
    return root / f"{identifier}.duckdb"


def _write_outputs(
    payload: dict[str, Any],
    artifact_payloads: dict[str, Any],
    out: str | Path | None,
    artifacts_dir: str | Path | None,
) -> None:
    if artifacts_dir:
        artifacts_path = Path(artifacts_dir)
        artifacts_path.mkdir(parents=True, exist_ok=True)
        for name, artifact_payload in artifact_payloads.items():
            with (artifacts_path / name).open("w", encoding="utf-8") as handle:
                json.dump(_jsonable(artifact_payload), handle, ensure_ascii=False, indent=2)

    if out:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as handle:
            json.dump(_jsonable(payload), handle, ensure_ascii=False, indent=2)


def _missing_uid_rows_from_raw(raw_frames: list[RawTable], field_reports: list[dict[str, Any]]) -> int:
    missing = 0
    for index, frame in enumerate(raw_frames):
        if not isinstance(frame, pd.DataFrame):
            continue
        report = field_reports[index] if index < len(field_reports) else {}
        fields = report.get("fields", {})
        uid_column = fields.get("uid") if isinstance(fields, dict) else None
        if not uid_column or uid_column not in frame.columns:
            missing += int(len(frame))
            continue
        values = frame[uid_column]
        text_values = values.astype(str).str.strip().str.lower()
        missing += int((values.isna() | text_values.isin({"", "nan", "none", "null"})).sum())
    return missing


def _run_staged_pipeline(
    inputs: list[str | Path],
    config: LanguageConfig,
    out: str | Path | None,
    normalized_out: str | Path | None,
    artifacts_dir: str | Path | None,
    warehouse_path: str | Path | None,
    run_id: str | None,
    sample_limit: int,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    _report_progress(progress_callback, 0.28, "normalize", "DuckDB 테이블에 정규화 중")
    stage_path = _staging_db_path(warehouse_path, out, run_id)
    staged = build_staged_analysis(
        inputs=inputs,
        config=config,
        db_path=stage_path,
        sample_limit=sample_limit,
    )

    _report_progress(progress_callback, 0.78, "diagnosis", "원인 후보 계산 중")
    diagnosis = {
        "content": diagnose_content(staged.content),
        "revenue": diagnose_revenue(staged.summary, staged.products, staged.purchases, staged.concentration),
    }
    alerts = build_alerts(diagnosis)
    payload = {
        "engine_version": "0.1.0",
        "data_quality": staged.data_quality,
        "summary": staged.summary,
        "summary_by_date": staged.summary_by_date,
        "language": {
            "suggestions": staged.language_suggestions,
            "needs_confirmation_count": int(staged.data_quality.get("inferred_language_rows", 0)),
        },
        "journeys": staged.journeys,
        "sessions": staged.sessions,
        "failure_contexts": _records(staged.failures),
        "purchase_contexts": _jsonable(staged.purchases),
        "content_health": _records(staged.content),
        "product_performance": _records(staged.products),
        "segments": _jsonable(staged.segments),
        "revenue_concentration": _jsonable(staged.concentration),
        "diagnosis": _jsonable(diagnosis),
        "alerts": alerts,
        "ai_context": {
            "briefing_rules": [
                "Do not claim a definitive cause from one-day data.",
                "Explain cause candidates using evidence numbers.",
                "Separate confirmed facts, likely hypotheses, and missing data.",
                "Prefer UID journey, session flow, and purchase context over raw count-only claims.",
            ],
            "top_alerts": alerts,
        },
    }
    artifact_payloads = _artifact_payloads(payload)

    if warehouse_path:
        _report_progress(progress_callback, 0.91, "warehouse", "DuckDB warehouse에 적재 중")
        warehouse_run_id = run_id or f"manual-{pd.Timestamp.now().strftime('%Y%m%d-%H%M%S')}"
        payload["warehouse"] = store_staged_run(
            warehouse_path=warehouse_path,
            run_id=warehouse_run_id,
            staged_db_path=staged.db_path,
            raw_frames=staged.raw_tables,
            products=staged.products,
            summary=staged.summary,
            summary_by_date=staged.summary_by_date,
            data_quality=staged.data_quality,
            artifacts=artifact_payloads,
        )

    _report_progress(progress_callback, 0.94, "artifacts", "결과 파일 저장 중")
    if normalized_out:
        export_normalized_events(staged.db_path, normalized_out)
    _write_outputs(payload, artifact_payloads, out, artifacts_dir)
    _report_progress(progress_callback, 0.98, "finalizing", "마무리 중")
    return _jsonable(payload)


def assess_quality(raw_frames: list[RawTable], events: pd.DataFrame, field_reports: list[dict[str, Any]]) -> dict[str, Any]:
    raw_file_infos = [describe_raw_table(frame) for frame in raw_frames]
    input_rows = sum(info.row_count for info in raw_file_infos)
    if any("missing_uid_rows" in report for report in field_reports):
        missing_uid = sum(int(report.get("missing_uid_rows", 0)) for report in field_reports)
    else:
        missing_uid = _missing_uid_rows_from_raw(raw_frames, field_reports)
    missing_ts = int(events["ts"].isna().sum()) if not events.empty else 0
    duplicate_events = int(events.duplicated(subset=["uid", "ts", "event_raw", "content_id", "product_id", "amount"]).sum())
    inferred_language = int((events["language_source"] != "dictionary").sum()) if not events.empty else 0
    denominator = max(input_rows, len(events), 1)
    return {
        "raw_files": [
            {
                "source_file": info.source_file,
                "rows": info.row_count,
                "columns": info.columns,
            }
            for info in raw_file_infos
        ],
        "input_rows": int(input_rows),
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
                    (missing_uid / denominator) * 0.35
                    + (missing_ts / denominator) * 0.25
                    + (duplicate_events / denominator) * 0.2
                    + (inferred_language / denominator) * 0.2
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
    artifacts_dir: str | Path | None = None,
    warehouse_path: str | Path | None = None,
    run_id: str | None = None,
    sample_limit: int = 20,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    _report_progress(progress_callback, 0.08, "config", "설정 확인 중")
    config = LanguageConfig.load(dictionary_path)

    _report_progress(progress_callback, 0.14, "ingest", "파일 읽는 중")
    try:
        return _run_staged_pipeline(
            inputs=inputs,
            config=config,
            out=out,
            normalized_out=normalized_out,
            artifacts_dir=artifacts_dir,
            warehouse_path=warehouse_path,
            run_id=run_id,
            sample_limit=sample_limit,
            progress_callback=progress_callback,
        )
    except DuckDBNormalizeUnsupported:
        pass

    raw_frames: list[RawTable]

    _report_progress(progress_callback, 0.28, "normalize", "UID 기준으로 정리 중")
    raw_frames, events, field_reports = _ingest_and_normalize(inputs, config)

    _report_progress(progress_callback, 0.44, "journey", "세션과 유저 여정 구성 중")
    events, sessions, journeys, failures, purchases = build_sql_facts(events, config.session_gap_minutes)

    _report_progress(progress_callback, 0.62, "metrics", "지표 계산 중")
    summary = daily_summary(events, sessions)
    summary_by_date = daily_summary_by_date(events, sessions)
    content = content_health(events, failures)
    products = product_performance(events, purchases)
    segments = segment_compare(events)
    concentration = whale_concentration(events)

    _report_progress(progress_callback, 0.78, "diagnosis", "원인 후보 계산 중")
    diagnosis = {
        "content": diagnose_content(content),
        "revenue": diagnose_revenue(summary, products, purchases, concentration),
    }
    alerts = build_alerts(diagnosis)
    data_quality = assess_quality(raw_frames, events, field_reports)
    language_suggestions = build_language_suggestions(events)

    _report_progress(progress_callback, 0.88, "packaging", "결과 구성 중")
    payload = {
        "engine_version": "0.1.0",
        "data_quality": data_quality,
        "summary": summary,
        "summary_by_date": summary_by_date,
        "language": {
            "suggestions": language_suggestions,
            "needs_confirmation_count": int((events["language_source"] != "dictionary").sum()),
        },
        "journeys": {
            "user_count": int(len(journeys)),
            "sample": _records(journeys, sample_limit),
        },
        "sessions": {
            "session_count": int(len(sessions)),
            "sample": _records(sessions, sample_limit),
        },
        "failure_contexts": _records(failures),
        "purchase_contexts": _jsonable(purchases),
        "content_health": _records(content),
        "product_performance": _records(products),
        "segments": _jsonable(segments),
        "revenue_concentration": _jsonable(concentration),
        "diagnosis": _jsonable(diagnosis),
        "alerts": alerts,
        "ai_context": {
            "briefing_rules": [
                "Do not claim a definitive cause from one-day data.",
                "Explain cause candidates using evidence numbers.",
                "Separate confirmed facts, likely hypotheses, and missing data.",
                "Prefer UID journey, session flow, and purchase context over raw count-only claims.",
            ],
            "top_alerts": alerts,
        },
    }
    artifact_payloads = _artifact_payloads(payload)

    if warehouse_path:
        _report_progress(progress_callback, 0.91, "warehouse", "DuckDB 저장 중")
        warehouse_run_id = run_id or f"manual-{pd.Timestamp.now().strftime('%Y%m%d-%H%M%S')}"
        payload["warehouse"] = store_run(
            warehouse_path=warehouse_path,
            run_id=warehouse_run_id,
            raw_frames=raw_frames,
            events=events,
            sessions=sessions,
            journeys=journeys,
            content=content,
            products=products,
            summary=summary,
            summary_by_date=summary_by_date,
            data_quality=data_quality,
            artifacts=artifact_payloads,
        )

    _report_progress(progress_callback, 0.94, "artifacts", "결과 파일 저장 중")
    if artifacts_dir:
        artifacts_path = Path(artifacts_dir)
        artifacts_path.mkdir(parents=True, exist_ok=True)
        for name, artifact_payload in artifact_payloads.items():
            with (artifacts_path / name).open("w", encoding="utf-8") as handle:
                json.dump(_jsonable(artifact_payload), handle, ensure_ascii=False, indent=2)

    if normalized_out:
        normalized_path = Path(normalized_out)
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        events.to_csv(normalized_path, index=False, encoding="utf-8-sig")

    if out:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as handle:
            json.dump(_jsonable(payload), handle, ensure_ascii=False, indent=2)
    _report_progress(progress_callback, 0.98, "finalizing", "마무리 중")
    return _jsonable(payload)
