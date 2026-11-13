import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opticrip import ffmpeg


class ToolDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        ffmpeg._CACHE.clear()
        self.addCleanup(ffmpeg._CACHE.clear)

    def _fake(self, name: str) -> Path:
        path = self.folder / name
        path.write_bytes(b"stub")
        return path

    def _env(self, **values):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("OPTICRIP_FFMPEG")}
        env.update(values)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_directory_override_wins_over_path(self):
        expected = self._fake("ffmpeg")
        with self._env(OPTICRIP_FFMPEG_DIR=str(self.folder)):
            with mock.patch.object(ffmpeg.shutil, "which",
                                   return_value="/usr/bin/ffmpeg"):
                self.assertEqual(ffmpeg.tool("ffmpeg"), str(expected))

    def test_binary_override_and_sibling_tool(self):
        binary = self._fake("ffmpeg")
        probe = self._fake("ffprobe")
        with self._env(OPTICRIP_FFMPEG=str(binary)):
            with mock.patch.object(ffmpeg.shutil, "which", return_value=None):
                self.assertEqual(ffmpeg.tool("ffmpeg"), str(binary))
                ffmpeg._CACHE.clear()
                self.assertEqual(ffmpeg.tool("ffprobe"), str(probe))

    def test_missing_tool_names_the_override_variable(self):
        with self._env():
            with mock.patch.object(ffmpeg.shutil, "which", return_value=None):
                with self.assertRaises(ffmpeg.FFmpegError) as caught:
                    ffmpeg.tool("ffmpeg")
        self.assertIn("OPTICRIP_FFMPEG", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
