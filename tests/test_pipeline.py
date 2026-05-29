from pathlib import Path
import tempfile
import unittest

import duckdb
import pandas as pd

from game_data_engine import run_pipeline
from game_data_engine.benchmark import run_benchmark
from game_data_engine.config import LanguageConfig
from game_data_engine.ingest import ingest
from game_data_engine.journey import (
    add_sessions,
    build_failure_contexts,
    build_purchase_contexts,
    build_session_flows,
    build_user_journeys,
)
from game_data_engine.metrics import (
    content_health as pandas_content_health,
    daily_summary,
    daily_summary_by_date,
    product_performance as pandas_product_performance,
)
from game_data_engine.pipeline import assess_quality
from game_data_engine.normalize import normalize
from game_data_engine.sql_facts import build_sql_facts
from game_data_engine.sql_metrics import (
    content_health as sql_content_health,
    product_performance as sql_product_performance,
)
from game_data_engine.sql_normalize import normalize_csv_with_duckdb
from game_data_engine.warehouse import fetch_run_snapshot


class PipelineTest(unittest.TestCase):
    def test_pipeline_builds_uid_journey_and_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "analysis.json"
            normalized_out = Path(directory) / "normalized.csv"
            payload = run_pipeline(
                inputs=[Path("examples/sample_events.csv")],
                dictionary_path=Path("examples/log_language.json"),
                out=out,
                normalized_out=normalized_out,
            )
            self.assertTrue(out.exists())
            self.assertTrue(normalized_out.exists())

            self.assertEqual(payload["summary"]["active_users"], 10)
            self.assertEqual(payload["summary"]["paying_users"], 3)
            self.assertEqual(payload["summary_by_date"]["date_count"], 1)
            self.assertEqual(payload["journeys"]["user_count"], 10)
            self.assertEqual(payload["data_quality"]["field_reports"][0]["normalize_engine"], "duckdb")
            self.assertEqual(payload["data_quality"]["field_reports"][0]["execution_engine"], "duckdb_staged")

            content_groups = {row["group"] for row in payload["content_health"]}
            self.assertIn("아레나", content_groups)
            self.assertIn("레이드", content_groups)

            alert_titles = [alert["title"] for alert in payload["alerts"]]
            self.assertTrue(any("아레나" in title for title in alert_titles))
            self.assertTrue(payload["purchase_contexts"]["top_preceding_events"])

    def test_pipeline_writes_duckdb_warehouse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            warehouse = Path(directory) / "game.duckdb"
            payload = run_pipeline(
                inputs=[Path("examples/sample_events.csv")],
                dictionary_path=Path("examples/log_language.json"),
                warehouse_path=warehouse,
                run_id="test-run",
            )

            self.assertTrue(warehouse.exists())
            self.assertEqual(payload["warehouse"]["table_counts"]["mart.normalized_events"], 45)
            self.assertEqual(payload["warehouse"]["table_counts"]["mart.user_journeys"], 10)
            self.assertEqual(payload["warehouse"]["summary_validation"]["status"], "match")
            self.assertEqual(payload["warehouse"]["sql_summary"]["events"], 45)
            self.assertEqual(payload["data_quality"]["field_reports"][0]["execution_engine"], "duckdb_staged")

            con = duckdb.connect(str(warehouse), read_only=True)
            try:
                normalized_count = con.execute(
                    "SELECT COUNT(*) FROM mart.normalized_events WHERE run_id = ?",
                    ["test-run"],
                ).fetchone()[0]
                summary = con.execute(
                    """
                    SELECT active_users, events, paying_users
                    FROM mart.run_summaries
                    WHERE run_id = ?
                    """,
                    ["test-run"],
                ).fetchone()
                daily_count = con.execute(
                    "SELECT COUNT(*) FROM mart.daily_summaries WHERE run_id = ?",
                    ["test-run"],
                ).fetchone()[0]
            finally:
                con.close()

            self.assertEqual(normalized_count, 45)
            self.assertEqual(summary, (10, 45, 3))
            self.assertEqual(daily_count, 1)

            snapshot = fetch_run_snapshot(warehouse, "test-run")
            self.assertEqual(snapshot["status"], "found")
            self.assertEqual(snapshot["summary"]["active_users"], 10)
            self.assertEqual(snapshot["sql_summary"]["paying_users"], 3)
            self.assertEqual(snapshot["summary_validation"]["status"], "match")
            self.assertEqual(snapshot["table_counts"]["mart.run_summaries"], 1)
            self.assertEqual(snapshot["summary_by_date"]["date_count"], 1)
            self.assertEqual(snapshot["source_files"][0]["row_count"], 45)

    def test_benchmark_generates_synthetic_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_benchmark(
                rows=120,
                users=12,
                output_dir=directory,
                dictionary_path=Path("examples/log_language.json"),
                warehouse_path=Path(directory) / "benchmark.duckdb",
                sample_limit=3,
            )

            self.assertEqual(result["rows"], 120)
            self.assertEqual(result["summary"]["events"], 120)
            self.assertEqual(result["warehouse"]["table_counts"]["mart.normalized_events"], 120)
            self.assertEqual(result["warehouse"]["summary_validation"]["status"], "match")
            self.assertTrue(Path(result["input_path"]).exists())

    def test_sql_facts_match_pandas_core_counts(self) -> None:
        config = LanguageConfig.load(Path("examples/log_language.json"))
        raw_frames = ingest([Path("examples/sample_events.csv")])
        normalized, _ = normalize(raw_frames, config)

        pandas_events = add_sessions(normalized, config.session_gap_minutes)
        pandas_sessions = build_session_flows(pandas_events)
        pandas_journeys = build_user_journeys(pandas_events)
        pandas_failures = build_failure_contexts(pandas_events)
        pandas_purchases = build_purchase_contexts(pandas_events)
        pandas_summary = daily_summary(pandas_events, pandas_sessions)

        sql_events, sql_sessions, sql_journeys, sql_failures, sql_purchases = build_sql_facts(
            normalized,
            config.session_gap_minutes,
        )
        sql_summary = daily_summary(sql_events, sql_sessions)

        self.assertEqual(sql_summary, pandas_summary)
        self.assertEqual(len(sql_sessions), len(pandas_sessions))
        self.assertEqual(len(sql_journeys), len(pandas_journeys))
        self.assertEqual(len(sql_failures), len(pandas_failures))
        self.assertEqual(sql_purchases["top_preceding_events"][0][1], pandas_purchases["top_preceding_events"][0][1])

    def test_duckdb_csv_normalize_matches_pandas_sample(self) -> None:
        config = LanguageConfig.load(Path("examples/log_language.json"))
        raw_frames = ingest([Path("examples/sample_events.csv")])
        expected_events, _ = normalize(raw_frames, config)

        raw_tables, actual_events, actual_reports = normalize_csv_with_duckdb(
            [Path("examples/sample_events.csv")],
            config,
        )

        self.assertEqual(raw_tables[0].row_count, 45)
        expected_columns = [column for column in raw_frames[0].columns if not str(column).startswith("_source")]
        self.assertEqual(raw_tables[0].columns, expected_columns)
        self.assertEqual(actual_reports[0]["normalize_engine"], "duckdb")
        pd.testing.assert_frame_equal(actual_events, expected_events, check_dtype=False)

    def test_sql_metrics_match_pandas_metrics(self) -> None:
        config = LanguageConfig.load(Path("examples/log_language.json"))
        raw_frames = ingest([Path("examples/sample_events.csv")])
        normalized, _ = normalize(raw_frames, config)

        events, _, _, failures, purchases = build_sql_facts(
            normalized,
            config.session_gap_minutes,
        )

        expected_content = pandas_content_health(events, failures).reset_index(drop=True)
        actual_content = sql_content_health(events, failures).reset_index(drop=True)
        pd.testing.assert_frame_equal(actual_content, expected_content, check_dtype=False)

        expected_products = pandas_product_performance(events, purchases).reset_index(drop=True)
        actual_products = sql_product_performance(events, purchases).reset_index(drop=True)
        pd.testing.assert_frame_equal(actual_products, expected_products, check_dtype=False)

    def test_missing_uid_rows_affect_quality(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing_uid.csv"
            path.write_text(
                "\n".join(
                    [
                        "uid,event_time,event_name,content_id,product_id,amount,duration_sec,wait_time_sec,result",
                        ",2026-05-28 00:00:00,login,,,0,0,0,success",
                        "u1,2026-05-28 00:01:00,login,,,0,0,0,success",
                    ]
                ),
                encoding="utf-8",
            )

            payload = run_pipeline(
                inputs=[path],
                dictionary_path=Path("examples/log_language.json"),
                sample_limit=1,
            )

            self.assertEqual(payload["data_quality"]["input_rows"], 2)
            self.assertEqual(payload["data_quality"]["normalized_rows"], 1)
            self.assertEqual(payload["data_quality"]["missing_uid_rows"], 1)
            self.assertLess(payload["data_quality"]["quality_score"], 1)

    def test_staged_pipeline_matches_pandas_quality_and_daily_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parity.csv"
            path.write_text(
                "\n".join(
                    [
                        "uid,event_time,event_name,content_id,product_id,amount,duration_sec,wait_time_sec,result",
                        "u1,2026-05-28 00:00:00,login,,,0,0,0,success",
                        "u1,2026-05-28 00:00:00,login,,,0,0,0,success",
                        "u1,2026-05-28 00:05:00,pkg_starter_buy,,starter_pack,9900,0,0,success",
                        "u2,2026-05-29 00:00:00,login,,,0,0,0,success",
                        "u2,2026-05-29 00:07:00,pkg_raid_buy,,raid_pack,55000,0,0,success",
                        ",2026-05-29 00:08:00,login,,,0,0,0,success",
                        "u3,,login,,,0,0,0,success",
                    ]
                ),
                encoding="utf-8",
            )
            config = LanguageConfig.load(Path("examples/log_language.json"))

            staged = run_pipeline(
                inputs=[path],
                dictionary_path=Path("examples/log_language.json"),
                sample_limit=2,
            )
            raw_frames = ingest([path])
            normalized, reports = normalize(raw_frames, config)
            events, sessions, _, _, _ = build_sql_facts(normalized, config.session_gap_minutes)
            pandas_summary = daily_summary(events, sessions)
            pandas_summary_by_date = daily_summary_by_date(events, sessions)
            pandas_quality = assess_quality(raw_frames, events, reports)

            self.assertEqual(staged["summary"], pandas_summary)
            self.assertEqual(staged["summary_by_date"], pandas_summary_by_date)
            for key in [
                "input_rows",
                "normalized_rows",
                "missing_uid_rows",
                "missing_timestamp_rows",
                "duplicate_event_rows",
                "inferred_language_rows",
                "quality_score",
            ]:
                self.assertEqual(staged["data_quality"][key], pandas_quality[key], key)


if __name__ == "__main__":
    unittest.main()
