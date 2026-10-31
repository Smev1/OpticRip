import tempfile
import unittest
from pathlib import Path

from opticrip.settings import JsonBackend, Settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"
        self.settings = Settings(JsonBackend(self.path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults(self):
        self.assertEqual(self.settings.get_str("app_DestinationType"), "Auto")
        self.assertEqual(self.settings.get_int("app_MinTitleLength"), 120)
        self.assertTrue(self.settings.get_bool("app_UpdateEnable"))
        self.assertEqual(self.settings.get_str("app_DvdcssPath"), "")
        self.assertEqual(self.settings.get_str("app_AacsPath"), "")
        self.assertEqual(self.settings.get_str("app_BdplusPath"), "")
        self.assertEqual(self.settings.get_str("app_AacsKeyFile"), "")

    def test_round_trip(self):
        self.settings.set("app_Proxy", "127.0.0.1:8888")
        self.settings.save()
        other = Settings(JsonBackend(self.path))
        self.assertEqual(other.get_str("app_Proxy"), "127.0.0.1:8888")

    def test_unknown_values_are_preserved(self):
        self.settings.backend.set("app_FutureSetting", "keep-me")
        self.assertEqual(Settings(JsonBackend(self.path)).get_str("app_FutureSetting"),
                         "keep-me")

    def test_window_state_is_base64_text(self):
        self.settings.save_window_state("860x640+10+10", '{"vertical": 300}')
        self.settings.save()
        raw = self.settings.backend.get("screen_geometry")
        self.assertIsInstance(raw, str)
        geometry, state = Settings(JsonBackend(self.path)).load_window_state()
        self.assertEqual(geometry, "860x640+10+10")
        self.assertIn("vertical", state)

    def test_launch_counter_increments(self):
        first = self.settings.bump_launch_counter()
        second = self.settings.bump_launch_counter()
        self.assertEqual(second, first + 1)
        keys = [k for k in self.settings.backend.values() if k.startswith("kslc_")]
        self.assertEqual(len(keys), 1)

    def test_output_dir(self):
        self.settings.set_output_dir("/tmp/out")
        self.assertEqual(self.settings.output_dir, Path("/tmp/out"))
        self.assertEqual(self.settings.get_str("path_DestDirMRU"), "/tmp/out")


if __name__ == "__main__":
    unittest.main()
