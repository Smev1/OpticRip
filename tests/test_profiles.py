import tempfile
import unittest
from pathlib import Path

from opticrip.model import Stream
from opticrip.profiles import ProfileError, builtin_default, discover, load_named, load_profile


class ProfileTests(unittest.TestCase):
    def test_bundled_profiles_are_discoverable(self):
        names = discover()
        for name in ("default", "flac", "aac-st", "wdtv"):
            self.assertIn(name, names)

    def test_default_profile_copies_everything(self):
        profile = load_named("default")
        self.assertEqual(profile.output_for(Stream(kind="audio", index=0, codec="AC-3")).output_format,
                         "directCopy")

    def test_flac_profile_targets_lpcm(self):
        profile = load_named("flac")
        lpcm = Stream(kind="audio", index=0, codec="LPCM", channels=2)
        ac3 = Stream(kind="audio", index=1, codec="AC-3", channels=6)
        self.assertEqual(profile.output_for(lpcm).output_format, "FLAC")
        self.assertEqual(profile.output_for(ac3).output_format, "directCopy")

    def test_mkv_settings_are_parsed(self):
        profile = load_named("default")
        self.assertEqual(profile.mkv_settings["insertFirstChapter00IfMissing"], "true")

    def test_parse_error_is_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.mmcp.xml"
            path.write_text("<profile><name>oops</name>", encoding="utf-8")
            with self.assertRaises(ProfileError):
                load_profile(path)

    def test_wrong_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrong.xml"
            path.write_text("<notprofile/>", encoding="utf-8")
            with self.assertRaises(ProfileError):
                load_profile(path)

    def test_builtin_default_has_copy_output(self):
        profile = builtin_default()
        self.assertEqual(profile.output_for(Stream(kind="video", index=0)).output_format,
                         "directCopy")


if __name__ == "__main__":
    unittest.main()
