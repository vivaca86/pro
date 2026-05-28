from pathlib import Path
import tempfile
import unittest

from game_data_engine import run_pipeline


class PipelineTest(unittest.TestCase):
    def test_pipeline_builds_uid_journey_and_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "analysis.json"
            payload = run_pipeline(
                inputs=[Path("examples/sample_events.csv")],
                dictionary_path=Path("examples/log_language.json"),
                out=out,
            )
            self.assertTrue(out.exists())

            self.assertEqual(payload["summary"]["active_users"], 10)
            self.assertEqual(payload["summary"]["paying_users"], 3)
            self.assertEqual(payload["journeys"]["user_count"], 10)

            content_groups = {row["group"] for row in payload["content_health"]}
            self.assertIn("아레나", content_groups)
            self.assertIn("레이드", content_groups)

            alert_titles = [alert["title"] for alert in payload["alerts"]]
            self.assertTrue(any("아레나" in title for title in alert_titles))
            self.assertTrue(payload["purchase_contexts"]["top_preceding_events"])


if __name__ == "__main__":
    unittest.main()
