"""Failure diagnosis: every error names a probable cause and a next step."""

import errno
import unittest

from opticrip import diagnose
from opticrip.aacs import AacsError
from opticrip.bdmv import BdmvError
from opticrip.ffmpeg import FFmpegError
from opticrip.gui.app import MainWindow


class WindowsErrorTests(unittest.TestCase):
    @staticmethod
    def _error(code: int, message: str) -> OSError:
        exc = OSError(code, message)
        exc.winerror = code
        return exc

    def test_dropped_drive_mentions_the_connection(self):
        result = diagnose.explain(self._error(21, "The device is not ready"),
                                  "saving", "title #23")
        self.assertIn("stopped responding", result.headline)
        self.assertIn("title #23", result.problem)
        self.assertIn("USB", result.causes[0])
        self.assertTrue(result.advice)

    def test_read_error_blames_the_disc_surface(self):
        result = diagnose.explain(self._error(23, "The device has a bad block"))
        self.assertIn("could not be read", result.headline)
        self.assertIn("scratch", " ".join(result.causes).lower())

    def test_locked_drive_suggests_closing_other_tools(self):
        result = diagnose.explain(self._error(5, "Access is denied"))
        self.assertIn("locked", result.headline)
        self.assertIn("MakeMKV", " ".join(result.causes))

    def test_full_disk_is_named(self):
        result = diagnose.explain(OSError(errno.ENOSPC, "No space left on device"))
        self.assertIn("full", result.headline)
        self.assertIn("space", result.advice.lower())

    def test_opening_uses_open_wording(self):
        result = diagnose.explain(OSError(0, "unspecified"), "opening the disc")
        self.assertIn("first sectors", " ".join(result.causes))


class DomainErrorTests(unittest.TestCase):
    def test_unknown_aacs_key_points_at_the_key_file(self):
        result = diagnose.explain(AacsError("The volume key is unknown for this disc"))
        self.assertIn("encryption key", result.headline)
        self.assertIn("key file", " ".join(result.causes).lower())
        self.assertIn("MakeMKV", result.advice)

    def test_ffmpeg_invalid_data_blames_the_clip(self):
        result = diagnose.explain(
            FFmpegError("ffmpeg exited 1: Invalid data found when processing input"))
        self.assertIn("decode", result.headline)
        self.assertIn("damaged", " ".join(result.causes).lower())

    def test_empty_output_is_explained(self):
        result = diagnose.explain(FFmpegError("empty output"))
        self.assertIn("rejected", result.headline)

    def test_structure_error_points_at_the_disc_layout(self):
        result = diagnose.explain(BdmvError("bad playlist"))
        self.assertIn("inconsistent", result.headline)


class UnknownErrorTests(unittest.TestCase):
    def test_unhandled_error_says_so_and_offers_the_traceback(self):
        result = diagnose.explain(ZeroDivisionError(), "saving")
        self.assertIn("unexpected", result.headline)
        self.assertIn("ZeroDivisionError", result.problem)
        self.assertIn("debug logging", result.advice)

    def test_every_diagnosis_is_actionable(self):
        for exc in (OSError(errno.EIO, "io"), AacsError("x"), FFmpegError("y"),
                    ValueError("z")):
            result = diagnose.explain(exc, "saving")
            self.assertTrue(result.headline)
            self.assertTrue(result.causes)
            self.assertTrue(all(cause.strip() for cause in result.causes))
            self.assertTrue(result.advice)
            self.assertIn(result.headline, result.summary())


class LogFormatTests(unittest.TestCase):
    def test_failure_block_leads_with_the_cause(self):
        lines = MainWindow._failure_lines({
            "stage": "saving",
            "headline": "the drive stopped responding",
            "problem": "[WinError 21] (title #23)",
            "causes": ["the cable", "the tray"],
            "advice": "re-seat it",
        })
        self.assertEqual(lines[0], "!! Saving failed: the drive stopped responding")
        self.assertIn("what happened: [WinError 21] (title #23)", lines[1])
        self.assertIn("   most likely: the cable", lines)
        self.assertIn("   also possible: the tray", lines)
        self.assertIn("   what to try: re-seat it", lines)


if __name__ == "__main__":
    unittest.main()
