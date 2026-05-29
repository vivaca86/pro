from pathlib import Path
import tempfile
import unittest

import app_server


class AppServerStorageTest(unittest.TestCase):
    def test_header_parameter_parser_handles_boundary_and_filename(self) -> None:
        media_type, params = app_server.parse_header_parameters(
            'multipart/form-data; boundary="----abc"; filename="sample.xlsx"'
        )

        self.assertEqual(media_type, "multipart/form-data")
        self.assertEqual(params["boundary"], "----abc")
        self.assertEqual(params["filename"], "sample.xlsx")

    def test_railway_volume_mount_wins_over_default_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as volume, tempfile.TemporaryDirectory() as fallback:
            data_dir, source = app_server.resolve_data_dir(
                {
                    "RAILWAY_VOLUME_MOUNT_PATH": volume,
                    "APP_DEFAULT_DATA_DIR": fallback,
                }
            )

            self.assertEqual(data_dir, Path(volume).resolve())
            self.assertEqual(source, "railway_volume")

    def test_explicit_app_data_dir_is_used_without_railway_volume(self) -> None:
        with tempfile.TemporaryDirectory() as configured:
            data_dir, source = app_server.resolve_data_dir({"APP_DATA_DIR": configured})

            self.assertEqual(data_dir, Path(configured).resolve())
            self.assertEqual(source, "APP_DATA_DIR")

    def test_output_dir_defaults_under_resolved_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory).resolve()
            output_dir, source = app_server.resolve_output_dir(data_dir, {})

            self.assertEqual(output_dir, data_dir / "output")
            self.assertEqual(source, "data_dir")

    def test_language_dictionary_update_merges_event_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dictionary = Path(directory) / "log_language.json"
            app_server.write_json(
                dictionary,
                {
                    "timezone": "Asia/Seoul",
                    "session_gap_minutes": 30,
                    "fields": {},
                    "event_labels": {},
                    "content_labels": {},
                    "product_labels": {},
                },
            )

            result = app_server.update_language_dictionary(
                [
                    {
                        "raw": "510101",
                        "label": "접속 시작",
                        "event_type": "session_start",
                        "group": "",
                    }
                ],
                dictionary,
            )

            saved = app_server.read_json(dictionary)
            self.assertEqual(result["updated"], 1)
            self.assertEqual(saved["event_labels"]["510101"]["label"], "접속 시작")
            self.assertEqual(saved["event_labels"]["510101"]["event_type"], "session_start")
            self.assertIsNone(saved["event_labels"]["510101"]["group"])


if __name__ == "__main__":
    unittest.main()
