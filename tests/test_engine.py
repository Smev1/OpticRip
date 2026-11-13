import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from opticrip import drives, registration
from opticrip.bdmv import BdmvError
from opticrip.discfs import CssFS, FolderFS
from opticrip.engine import Engine
from opticrip.ffmpeg import _progress_seconds, available, ffprobe_json, tool
from opticrip.model import Disc, Segment, Stream, Title
from opticrip.profiles import builtin_default
from opticrip.robot import RobotWriter
from opticrip.settings import JsonBackend, Settings


def make_engine(directory: Path):
    stream = io.StringIO()
    settings = Settings(JsonBackend(directory / "settings.json"))
    engine = Engine(RobotWriter(stream), settings, builtin_default())
    return engine, stream, settings


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.engine, self.stream, self.settings = make_engine(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def output(self) -> str:
        return self.stream.getvalue()

    def test_missing_iso_reports_and_exits_zero(self):
        result = self.engine.open_source(f"iso:{self.dir / 'nope.iso'}")
        self.assertEqual(result.code, 0)
        self.assertIsNone(result.disc)
        self.assertIn("MSG:5006", self.output())
        self.assertIn("TCOUNT:0", self.output())

    def test_zero_filled_iso_reports_read_error(self):
        junk = self.dir / "junk.iso"
        junk.write_bytes(b"\x00" * (1 << 20))
        result = self.engine.open_source(f"iso:{junk}")
        self.assertEqual(result.code, 0)
        self.assertIn("MSG:2003", self.output())
        self.assertIn("MSG:5010", self.output())

    def test_folder_without_disc_exits_ten_without_message(self):
        empty = self.dir / "empty"
        empty.mkdir()
        result = self.engine.open_source(f"file:{empty}")
        self.assertEqual(result.code, 10)
        self.assertEqual(self.output(), "")

    def test_looks_scrambled_uses_sector_flags(self):
        root = self.dir / "disc"
        video = root / "VIDEO_TS"
        video.mkdir(parents=True)
        blob = bytearray(3 * 2048)
        blob[0:4] = b"\x00\x00\x01\xba"
        blob[0x0E:0x12] = b"\x00\x00\x01\xe0"
        blob[0x14] = 0x80
        blob[36:40] = b"\x00\x00\x01\xb3"
        for sector in (1, 2):
            base = sector * 2048
            blob[base:base + 4] = b"\x00\x00\x01\xba"
            blob[base + 0x0E:base + 0x12] = b"\x00\x00\x01\xe0"
            blob[base + 0x14] = 0x91
        (video / "VTS_01_1.VOB").write_bytes(bytes(blob))
        self.assertTrue(self.engine._looks_scrambled(FolderFS(root)))
        for sector in (1, 2):
            blob[sector * 2048 + 0x14] = 0x80
        (video / "VTS_01_1.VOB").write_bytes(bytes(blob))
        self.assertFalse(self.engine._looks_scrambled(FolderFS(root)))

    def test_missing_libdvdcss_reports_9120(self):
        self.settings.set("app_DvdcssPath", str(self.dir / "missing.dll"))
        self.assertIsNone(self.engine._try_css("D:"))
        self.assertIn("MSG:9120", self.output())

    def test_materialise_dvd_copies_only_cell_ranges(self):
        class FakeFS:
            blob = b"0123456789"

            def local(self, path):
                return None

            def read(self, path, offset=0, size=None):
                return self.blob[offset:offset + size]

            def size(self, path):
                return len(self.blob)

        self.engine.fs = FakeFS()
        segments = [
            Segment("VIDEO_TS/VTS_01_1.VOB", byte_offset=2, byte_size=3),
            Segment("VIDEO_TS/VTS_01_1.VOB", byte_offset=7, byte_size=2),
        ]
        reports = []
        paths, temporary = self.engine._materialise_dvd(
            segments, self.dir, lambda done, total: reports.append((done, total)),
        )
        try:
            self.assertEqual(Path(paths[0]).read_bytes(), b"23478")
            self.assertEqual(reports, [(0, 5), (3, 5), (5, 5)])
        finally:
            if temporary is not None:
                temporary.cleanup()

    def test_materialise_dvd_honours_cancel(self):
        class SlowFS:
            blob = b"0123456789"

            def local(self, path):
                return None

            def read(self, path, offset=0, size=None):
                return self.blob[offset:offset + min(size or 2, 2)]

            def size(self, path):
                return len(self.blob)

        self.engine.fs = SlowFS()
        reports = []
        with self.assertRaises(KeyboardInterrupt):
            self.engine._materialise_dvd(
                [Segment("VIDEO_TS/VTS_01_1.VOB", byte_size=10)],
                self.dir,
                lambda done, total: reports.append(done),
                lambda: len(reports) >= 2,
            )
        self.assertEqual(reports, [0, 2])
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_apply_selection_filters_titles_and_streams(self):
        def streams():
            return [Stream(kind="video", index=0), Stream(kind="audio", index=1)]
        disc = Disc(kind="DVD", titles=[
            Title(index=0, duration=200, streams=streams()),
            Title(index=1, duration=300, streams=streams()),
        ], title_count=2)
        self.engine.disc = disc
        self.engine.apply_selection([1], {"1": [0]})
        self.assertFalse(disc.titles[0].selected)
        self.assertTrue(disc.titles[1].selected)
        self.assertTrue(disc.titles[1].streams[0].selected)
        self.assertFalse(disc.titles[1].streams[1].selected)

    def test_backup_requires_disc_source(self):
        ok, code = self.engine.backup(f"iso:{self.dir / 'x.iso'}", self.dir / "out")
        self.assertFalse(ok)
        self.assertEqual(code, 10)
        self.assertIn("MSG:9109", self.output())

    def test_backup_missing_drive(self):
        ok, code = self.engine.backup("disc:15", self.dir / "out")
        self.assertFalse(ok)
        self.assertEqual(code, 10)

    def test_firmware_without_drive(self):
        self.assertEqual(self.engine.firmware([]), 2)
        self.assertIn("MSG:9111", self.output())

    def test_register_invalid_then_valid(self):
        _ok, code = self.engine.register("BOGUS123")
        self.assertEqual(code, 1)
        self.assertIn("MSG:9105", self.output())
        key = registration.make_key("test")
        ok, code = self.engine.register(key)
        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertEqual(Settings(JsonBackend(self.dir / "settings.json")).get_str("app_Key"), key)

    def test_ffmpeg_progress_records_are_microseconds(self):
        # ffmpeg's out_time_ms is misnamed: both records carry microseconds.
        self.assertAlmostEqual(_progress_seconds("out_time_ms=500000"), 0.5)
        self.assertAlmostEqual(_progress_seconds("out_time_us=1250000"), 1.25)
        self.assertIsNone(_progress_seconds("progress=end"))

    @unittest.skipUnless(available(), "ffmpeg is required")
    def test_mkv_end_to_end(self):
        source = self.dir / "sample.mkv"
        subprocess.run([
            tool("ffmpeg"), "-nostdin", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            "-shortest", str(source),
        ], check=True)
        result = self.engine.open_source(str(source), allow_mkv=True)
        self.assertIsNotNone(result.disc)
        self.assertEqual(len(result.disc.titles), 1)
        title = result.disc.titles[0]
        self.assertEqual(len(title.video_streams()), 1)
        self.assertEqual(len(title.audio_streams()), 1)
        progress = []
        self.engine.on_progress = lambda current, total: progress.append(current)
        out = self.dir / "out"
        saved, code = self.engine.save_selected(out)
        self.assertEqual((saved, code), (1, 0))
        path = next(out.glob("*.mkv"))
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 1000)
        info = ffprobe_json(str(path))
        self.assertAlmostEqual(float(info["format"]["duration"]), 2.0, delta=0.3)
        self.assertEqual(len(info["streams"]), 2)
        self.assertTrue(progress)
        self.assertEqual(progress, sorted(progress))
        # Verified behaviour: console saves emit no PRG* records.
        self.assertNotIn("PRGV:", self.output())
        self.assertIn("MSG:5005", self.output())

    @unittest.skipUnless(drives.enumerate_drives(), "no optical drive available")
    def test_real_drive_metadata(self):
        engine, stream, _ = make_engine(self.dir)
        result = engine.open_source("disc:0", emit_info=True)
        if result.disc is None:
            self.assertIn("MSG:5010", stream.getvalue())
            return
        self.assertGreater(result.disc.title_count, 0)
        self.assertIn("CINFO:1,6206,", stream.getvalue())
        self.assertIn("TINFO:0,9,0,", stream.getvalue())
        self.assertIn("TCOUNT:", stream.getvalue())
        # With libdvdcss active an encrypted disc is intentionally saveable;
        # this check covers the no-decryption refusal path only.
        if result.disc.encrypted and not isinstance(engine.fs, CssFS):
            path = engine.save_title(result.disc.titles[0], self.dir / "out")
            self.assertIsNone(path)
            self.assertIn("MSG:9100", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
