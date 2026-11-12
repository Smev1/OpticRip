import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opticrip import ffmpeg
from opticrip.model import Stream, Title


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


class MuxMappingTests(unittest.TestCase):
    def args_for(self, title, streams):
        return ffmpeg._mux_args(
            ["-i", "input.m2ts"], Path("out.mkv"), streams, title,
            chapters=False, compression=None, temporary=Path("."),
            map_all=False, container="matroska",
        )

    def test_bluray_uses_probed_per_kind_ordinals(self):
        title = Title(index=0, source="bluray", duration=1.0)
        video = Stream(kind="video", index=0, source_index=0)
        first = Stream(kind="audio", index=1, pid=0x1101, source_index=1)
        second = Stream(kind="audio", index=2, pid=0x1100, source_index=0)
        title.streams = [video, first, second]
        args = self.args_for(title, title.streams)
        mapped = [args[i + 1] for i, value in enumerate(args) if value == "-map"]
        self.assertEqual(mapped, ["0:v:0", "0:a:1", "0:a:0"])

    def test_language_and_title_tags_follow_output_order(self):
        title = Title(index=0, source="dvd", duration=1.0)
        stream = Stream(kind="audio", index=0, language="eng",
                        language_name="English", codec="AC-3", channels=6)
        title.streams = [stream]
        args = self.args_for(title, title.streams)
        self.assertIn("-metadata:s:0", args)
        self.assertIn("language=eng", args)
        self.assertIn("title=English Audio (AC-3 5.1)", args)

    def test_subtitle_title_uses_normalised_format(self):
        title = Title(index=0, source="dvd", duration=1.0)
        stream = Stream(kind="subtitle", index=0, language="fra",
                        language_name="French", codec="DVD subtitles")
        title.streams = [stream]
        args = self.args_for(title, title.streams)
        self.assertIn("title=French Subtitles (VobSub)", args)

    def test_unknown_language_still_gets_a_descriptive_title(self):
        title = Title(index=0, source="mkv", duration=1.0)
        stream = Stream(kind="audio", index=0, language="und",
                        codec="AAC", channels=2)
        title.streams = [stream]
        args = self.args_for(title, title.streams)
        self.assertIn("title=Undetermined Audio (AAC Stereo)", args)


if __name__ == "__main__":
    unittest.main()
