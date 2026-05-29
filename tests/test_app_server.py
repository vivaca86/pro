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

    def test_language_presets_keep_mappings_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_dictionary = app_server.DICTIONARY
            old_presets_dir = app_server.PRESETS_DIR
            old_active_preset = app_server.ACTIVE_PRESET
            try:
                app_server.DICTIONARY = root / "log_language.json"
                app_server.PRESETS_DIR = root / "presets"
                app_server.ACTIVE_PRESET = root / "active_preset.json"
                app_server.write_json(
                    app_server.DICTIONARY,
                    {
                        "timezone": "Asia/Seoul",
                        "session_gap_minutes": 30,
                        "fields": {},
                        "event_labels": {},
                        "content_labels": {},
                        "product_labels": {},
                    },
                )

                created = app_server.create_language_preset("Dragon Realm")
                preset_id = str(created["active_preset_id"])
                app_server.update_language_dictionary(
                    [
                        {
                            "raw": "unique_preset_code",
                            "label": "프리셋 전용 로그",
                            "event_type": "event",
                            "group": "테스트",
                        }
                    ],
                    preset_id=preset_id,
                )

                default_dictionary = app_server.read_json(app_server.DICTIONARY)
                preset_dictionary = app_server.read_json(app_server.PRESETS_DIR / f"{preset_id}.json")
                presets = app_server.list_language_presets()

                self.assertEqual(app_server.read_active_preset_id(), preset_id)
                self.assertEqual(presets["active_preset_id"], preset_id)
                self.assertNotIn("unique_preset_code", default_dictionary["event_labels"])
                self.assertEqual(
                    preset_dictionary["event_labels"]["unique_preset_code"]["label"],
                    "프리셋 전용 로그",
                )
            finally:
                app_server.DICTIONARY = old_dictionary
                app_server.PRESETS_DIR = old_presets_dir
                app_server.ACTIVE_PRESET = old_active_preset


if __name__ == "__main__":
    unittest.main()
