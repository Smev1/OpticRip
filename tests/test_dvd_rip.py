"""End-to-end DVD path: folder -> IFO parse -> concat VOBs -> MKV.

The fixture is a real MPEG-PS DVD stream produced by ffmpeg plus hand-built
IFOs, so the parser and the remux path are both exercised without any
CSS-protected media.
"""

import io
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from opticrip.engine import Engine, serialize_disc
from opticrip.ffmpeg import available, ffprobe_json, tool
from opticrip.profiles import builtin_default
from opticrip.robot import RobotWriter
from opticrip.settings import JsonBackend, Settings


def bcd(h: int, m: int, s: int, frames: int = 0, rate: int = 3) -> bytes:
    def enc(value: int) -> int:
        return ((value // 10) << 4) | (value % 10)
    return bytes([enc(h), enc(m), enc(s), (rate << 6) | enc(frames)])


def make_paths(root: Path) -> None:
    video_ts = root / "VIDEO_TS"
    video_ts.mkdir(parents=True)
    vmgi = bytearray(2 * 2048)
    vmgi[0:12] = b"DVDVIDEO-VMG"
    struct.pack_into(">I", vmgi, 0xC4, 1)
    table = bytearray(2048)
    struct.pack_into(">H", table, 0, 1)
    entry = bytearray(12)
    entry[0] = 0x34
    entry[1] = 1
    struct.pack_into(">H", entry, 2, 1)
    entry[6] = 1     # title set number
    entry[7] = 1     # title number
    table[8:20] = entry
    vmgi[2048:4096] = table
    (video_ts / "VIDEO_TS.IFO").write_bytes(bytes(vmgi))

    vtsi = bytearray(4 * 2048)
    vtsi[0:12] = b"DVDVIDEO-VTS"
    struct.pack_into(">I", vtsi, 0xCC, 2)
    struct.pack_into(">H", vtsi, 0x200, 0x4E80)
    struct.pack_into(">H", vtsi, 0x202, 1)
    vtsi[0x204:0x20C] = bytes([0x04, 0xC1, 0x65, 0x6E, 0, 0, 0, 0])
    struct.pack_into(">H", vtsi, 0x254, 0)
    pgcit = bytearray(2048)
    struct.pack_into(">H", pgcit, 0, 1)
    struct.pack_into(">I", pgcit, 4, 0x180)
    struct.pack_into(">I", pgcit, 8, 0x81000000)
    struct.pack_into(">I", pgcit, 12, 16)
    pgc = bytearray(0x200)
    pgc[2] = 1
    pgc[3] = 1
    pgc[4:8] = bcd(0, 0, 3)
    struct.pack_into(">H", pgc, 0xE6, 0x0100)
    struct.pack_into(">H", pgc, 0xE8, 0x0110)
    pgc[0x100] = 1
    pgc[0x114:0x118] = bcd(0, 0, 3)
    struct.pack_into(">I", pgc, 0x118, 0)
    struct.pack_into(">I", pgc, 0x124, 1000)
    pgcit[16:16 + len(pgc)] = pgc
    vtsi[4096:6144] = pgcit
    (video_ts / "VTS_01_0.IFO").write_bytes(bytes(vtsi))


def make_two_title_paths(root: Path, first: int, second: int) -> None:
    """Two PGCs sharing one VOB; the second starts at the first's end."""
    video_ts = root / "VIDEO_TS"
    video_ts.mkdir(parents=True)
    vmgi = bytearray(2 * 2048)
    vmgi[0:12] = b"DVDVIDEO-VMG"
    struct.pack_into(">I", vmgi, 0xC4, 1)
    table = bytearray(2048)
    struct.pack_into(">H", table, 0, 2)
    for index in range(2):
        entry = bytearray(12)
        entry[0] = 0x34
        entry[1] = 1
        struct.pack_into(">H", entry, 2, 1)
        entry[6] = 1
        entry[7] = index + 1
        table[8 + index * 12:20 + index * 12] = entry
    vmgi[2048:4096] = table
    (video_ts / "VIDEO_TS.IFO").write_bytes(bytes(vmgi))

    vtsi = bytearray(4 * 2048)
    vtsi[0:12] = b"DVDVIDEO-VTS"
    struct.pack_into(">I", vtsi, 0xCC, 2)
    struct.pack_into(">H", vtsi, 0x200, 0x4E80)
    struct.pack_into(">H", vtsi, 0x202, 1)
    vtsi[0x204:0x20C] = bytes([0x04, 0xC1, 0x65, 0x6E, 0, 0, 0, 0])
    pgcit = bytearray(4096)
    struct.pack_into(">H", pgcit, 0, 2)
    first_sectors = (first + 2047) // 2048
    second_sectors = (second + 2047) // 2048
    for index, (seconds, start, count) in enumerate((
            (2, 0, first_sectors),
            (3, first_sectors, second_sectors))):
        offset = 16 + index * 0x200
        struct.pack_into(">I", pgcit, 12 + index * 8, offset)
        pgc = bytearray(0x200)
        pgc[2] = 1
        pgc[3] = 1
        pgc[4:8] = bcd(0, 0, seconds)
        struct.pack_into(">H", pgc, 0xE6, 0x0100)
        struct.pack_into(">H", pgc, 0xE8, 0x0110)
        pgc[0x100] = 1
        pgc[0x114:0x118] = bcd(0, 0, seconds)
        struct.pack_into(">I", pgc, 0x118, start)
        struct.pack_into(">I", pgc, 0x124, start + count - 1)
        pgcit[offset:offset + 0x200] = pgc
    vtsi[4096:8192] = pgcit
    (video_ts / "VTS_01_0.IFO").write_bytes(bytes(vtsi))


@unittest.skipUnless(available(), "ffmpeg is required")
class DvdRipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "disc"
        make_paths(root)
        vob = Path(cls.tmp.name) / "raw.vob"
        subprocess.run([
            tool("ffmpeg"), "-nostdin", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=720x480:rate=30000/1001",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "mpeg2video", "-b:v", "2M", "-c:a", "ac3",
            "-target", "ntsc-dvd", "-f", "dvd", str(vob),
        ], check=True)
        target = root / "VIDEO_TS" / "VTS_01_1.VOB"
        target.write_bytes(vob.read_bytes())
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.settings = Settings(JsonBackend(Path(self.work.name) / "settings.json"))
        self.settings.set("app_MinTitleLength", 0)
        self.settings.set("app_DefaultSelectionString", "-sel:all,+sel:(single|favlang|nolang)")
        self.stream = io.StringIO()
        self.engine = Engine(RobotWriter(self.stream), self.settings, builtin_default())

    def tearDown(self):
        self.work.cleanup()

    def test_open_rip_and_probe(self):
        result = self.engine.open_source(f"file:{self.root}", emit_info=True)
        self.assertIsNotNone(result.disc, self.stream.getvalue())
        disc = result.disc
        self.assertEqual(disc.kind, "DVD")
        self.assertFalse(disc.encrypted)
        self.assertEqual(len(disc.titles), 1)
        title = disc.titles[0]
        self.assertAlmostEqual(title.duration, 3.0, delta=0.2)
        self.assertEqual(title.chapter_count, 1)
        self.assertTrue(title.video_streams())
        self.assertTrue(title.audio_streams())
        self.assertIn("TINFO:0,9,0,\"0:00:03\"", self.stream.getvalue())

        out = Path(self.work.name) / "out"
        saved, code = self.engine.save_selected(out)
        self.assertEqual((saved, code), (1, 0), self.stream.getvalue())
        files = list(out.glob("*.mkv"))
        self.assertEqual(len(files), 1)
        info = ffprobe_json(str(files[0]))
        kinds = sorted(stream["codec_type"] for stream in info["streams"])
        self.assertEqual(kinds, ["audio", "video"])
        self.assertAlmostEqual(float(info["format"]["duration"]), 3.0, delta=0.4)
        self.assertGreaterEqual(len(info.get("chapters", [])), 1)
        payload = serialize_disc(disc)
        self.assertEqual(payload["kind"], "DVD")
        self.assertEqual(payload["titles"][0]["streams"][0]["kind_label"], "Video")

    def test_shared_vob_title_is_trimmed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "disc"
        clips = []
        for index, seconds in enumerate((2, 3)):
            path = Path(tmp.name) / f"clip{index}.vob"
            subprocess.run([
                tool("ffmpeg"), "-nostdin", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i",
                f"testsrc=duration={seconds}:size=720x480:rate=30000/1001",
                "-f", "lavfi", "-i", f"sine=frequency={440 + index * 110}:duration={seconds}",
                "-c:v", "mpeg2video", "-b:v", "2M", "-c:a", "ac3",
                "-target", "ntsc-dvd", "-f", "dvd", str(path),
            ], check=True)
            blob = path.read_bytes()
            pad = (-len(blob)) % 2048
            clips.append(blob + b"\x00" * pad)
        make_two_title_paths(root, len(clips[0]), len(clips[1]))
        (root / "VIDEO_TS" / "VTS_01_1.VOB").write_bytes(b"".join(clips))

        settings = Settings(JsonBackend(Path(tmp.name) / "settings.json"))
        settings.set("app_MinTitleLength", 0)
        stream = io.StringIO()
        engine = Engine(RobotWriter(stream), settings, builtin_default())
        result = engine.open_source(f"file:{root}")
        self.assertIsNotNone(result.disc, stream.getvalue())
        self.assertEqual([round(t.duration) for t in result.disc.titles], [2, 3])
        second = result.disc.titles[1]
        self.assertEqual([(s.byte_offset, s.byte_size) for s in second.segments],
                         [(len(clips[0]), len(clips[1]))])

        out = Path(tmp.name) / "out"
        path = engine.save_title(second, out)
        self.assertIsNotNone(path, stream.getvalue())
        info = ffprobe_json(str(path))
        self.assertAlmostEqual(float(info["format"]["duration"]), 3.0, delta=0.4)

    def test_stream_selection_reaches_the_remux(self):
        result = self.engine.open_source(f"file:{self.root}")
        self.assertIsNotNone(result.disc, self.stream.getvalue())
        title = result.disc.titles[0]
        video = title.video_streams()[0]
        self.engine.apply_selection([title.index],
                                    {str(title.index): [video.index]})
        out = Path(self.work.name) / "video-only"
        saved, code = self.engine.save_selected(out)
        self.assertEqual((saved, code), (1, 0), self.stream.getvalue())
        path = next(out.glob("*.mkv"))
        info = ffprobe_json(str(path))
        self.assertEqual([s["codec_type"] for s in info["streams"]], ["video"])


if __name__ == "__main__":
    unittest.main()
