import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from opticrip import cli


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "OPTICRIP_JSON_SETTINGS": "1",
            "OPTICRIP_DATA_DIR": self.tmp.name,
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_no_arguments_prints_usage(self):
        code, out, _ = self.run_cli([])
        self.assertEqual(code, 1)
        self.assertIn("Use: opticrip", out)

    def test_unknown_option(self):
        code, out, err = self.run_cli(["-h"])
        self.assertEqual(code, 1)
        self.assertIn("unrecognized option: h", err)
        self.assertIn("Commands:", out)

    def test_list_is_rejected_like_the_reference(self):
        code, out, _ = self.run_cli(["list"])
        self.assertEqual(code, 127)
        self.assertIn("Invalid command: list", out)

    def test_unknown_command(self):
        code, out, _ = self.run_cli(["bogus"])
        self.assertEqual(code, 127)
        self.assertIn("Invalid command: bogus", out)

    def test_missing_iso_info_exits_zero(self):
        code, out, _ = self.run_cli(["-r", "info", "iso:definitely-missing.iso"])
        self.assertEqual(code, 0)
        self.assertIn("MSG:5006", out)
        self.assertIn("MSG:5010", out)
        self.assertIn("TCOUNT:0", out)

    def test_backup_with_iso_source(self):
        code, out, _ = self.run_cli(["-r", "backup", "iso:x.iso", str(Path(self.tmp.name) / "out")])
        self.assertEqual(code, 10)
        self.assertIn("MSG:9109", out)

    def test_firmware_tool_without_arguments(self):
        code, out, _ = self.run_cli(["-r", "f"])
        self.assertEqual(code, 2)
        self.assertIn("MSG:9111", out)

    def test_invalid_registration_key(self):
        code, out, _ = self.run_cli(["-r", "reg", "BOGUSTESTKEY123"])
        self.assertEqual(code, 1)
        self.assertIn("MSG:9105", out)


if __name__ == "__main__":
    unittest.main()
