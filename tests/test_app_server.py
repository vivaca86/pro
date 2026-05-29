from pathlib import Path
import tempfile
import unittest

import app_server


class AppServerStorageTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
