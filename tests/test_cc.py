"""DVD closed-caption extraction: user data, CEA-608 and the save path."""

import io
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from opticrip import cc
from opticrip.engine import Engine
from opticrip.ffmpeg import available, ffprobe_json, tool
from opticrip.profiles import builtin_default
from opticrip.robot import RobotWriter
from opticrip.settings import JsonBackend, Settings

try:
    from test_dvd_rip import make_paths
except ImportError:  # direct module execution
    from tests.test_dvd_rip import make_paths


def caption_payload(first: int, second: int, pattern: int = 1,
                    truncate: int = 0) -> bytes:
    """One field-1 caption pair in the DVD ``CC 01 F8`` user-data format."""
    control = (pattern << 7) | (1 << 1) | truncate
    blocks = bytes([0xFF, first, second, 0xFE, 0x80, 0x80])
    return b"CC\x01\xf8" + bytes([control]) + blocks


def caption_span(first: int, second: int) -> bytes:
    return b"\x00\x00\x01\xb2" + caption_payload(first, second)


POP_ON = [
    (0x14, 0x20),  # RCL
    (0x14, 0x20),
    (0x14, 0x40),  # PAC row 14
    (0x14, 0x40),
    (ord("H"), ord("E")),
    (ord("L"), ord("L")),
    (ord("O"), 0x20),
    (0x14, 0x2F),  # EOC
    (0x14, 0x2F),
    (0x80, 0x80),
    (0x14, 0x2E),  # EDM
    (0x14, 0x2E),
]


def elementary_stream(pairs: list[tuple[int, int]]) -> bytes:
    """MPEG-2 ES with one caption pair on each picture."""
    sequence = b"\x00\x00\x01\xb3" + bytes([0, 0, 0, 0x34]) + b"\x00" * 4
    gop = b"\x00\x00\x01\xb8" + b"\x00\x00\x00\x00"
    frames = []
    for index, (first, second) in enumerate(pairs):
        temporal = struct.pack(">HH", index << 6, 0)
        frames.append(b"\x00\x00\x01\x00" + temporal
                      + caption_span(first, second)
                      + b"\x00\x00\x01\x01" + b"\x00" * 10)
    return sequence + gop + b"".join(frames)


def inject_dvd_captions(data: bytes, payloads: list[bytes]) -> bytes:
    """Insert user data into video PES packets of a DVD VOB.

    ffmpeg's DVD muxer rejects caption user data in its input, so the test
    builds a normal VOB and patches the elementary stream afterwards; the
    PES lengths are updated so the result stays structurally valid.
    """
    out = bytearray()
    position = 0
    frame = 0
    while position < len(data):
        if data[position:position + 4] != b"\x00\x00\x01\xba":
            out.append(data[position])
            position += 1
            continue
        pack_length = 14 + (data[position + 13] & 0x07)
        out += data[position:position + pack_length]
        position += pack_length
        if data[position:position + 4] == b"\x00\x00\x01\xbb":
            length = int.from_bytes(data[position + 4:position + 6], "big")
            out += data[position:position + 6 + length]
            position += 6 + length
        if data[position:position + 3] != b"\x00\x00\x01":
            continue
        stream = data[position + 3]
        length = int.from_bytes(data[position + 4:position + 6], "big")
        if stream == 0xE0 and length:
            end = position + 6 + length
            header_length = data[position + 8]
            start = position + 9 + header_length
            body = data[start:end]
            picture = body.find(b"\x00\x00\x01\x00")
            insert_at = None
            if picture >= 0:
                slice_at = body.find(b"\x00\x00\x01\x01", picture + 4)
                if slice_at >= 0 and frame < len(payloads):
                    insert_at = slice_at
            if insert_at is not None:
                new_body = (body[:insert_at] + b"\x00\x00\x01\xb2"
                            + payloads[frame] + body[insert_at:])
                frame += 1
                new_length = len(new_body) + header_length + 3
                out += (data[position:position + 4]
                        + new_length.to_bytes(2, "big")
                        + data[position + 6:start] + new_body)
                position = end
                continue
        out += data[position:position + 6 + length]
        position += 6 + length
    if frame != len(payloads):
        raise AssertionError(f"only injected {frame} of {len(payloads)} captions")
    return bytes(out)


class CaptionUserDataTests(unittest.TestCase):
    def test_has_captions(self):
        self.assertTrue(cc.has_captions(elementary_stream([(0x14, 0x20)])))
        self.assertFalse(cc.has_captions(b"\x00\x00\x01\xb3" + b"\x00" * 32))
        padding = caption_span(0x80, 0x80)
        self.assertFalse(cc.has_captions(padding))

    def test_payload_blocks_can_be_requested_as_field_two(self):
        payload = b"CC\x01\xf8" + bytes([0x00 | (1 << 1)]) + (
            b"\xFE\x80\x80" + b"\xFF\x14\x20")
        blocks = list(cc._caption_blocks(payload))
        self.assertEqual(blocks, [(2, 0x80, 0x80), (1, 0x14, 0x20)])

    def test_truncated_payload_stops_cleanly(self):
        payload = b"CC\x01\xf8" + bytes([(1 << 7) | (1 << 1)]) + b"\xFF\x14"
        self.assertEqual(list(cc._caption_blocks(payload)), [])

    def test_trailing_filler_is_not_decoded_as_music_notes(self):
        payload = (b"CC\x01\xf8" + bytes([(1 << 7) | (1 << 1)])
                   + b"\xFF\x14\x20\xFE\x80\x80" + b"\xFF" * 8)
        blocks = list(cc._caption_blocks(payload))
        self.assertEqual(blocks, [(1, 0x14, 0x20), (2, 0x80, 0x80)])
        decoder = cc._CaptionDecoder()
        for field, first, second in blocks:
            if field == 1:
                decoder.feed(first, second, 0.0)
        self.assertNotIn("\u266a", "".join(cue.text for cue in decoder.cues))


class CaptionDecoderTests(unittest.TestCase):
    def test_pop_on_caption_becomes_a_cue(self):
        decoder = cc._CaptionDecoder()
        for index, (first, second) in enumerate(POP_ON):
            decoder.feed(first, second, index / 30.0)
        cues = decoder.finish(10.0)
        self.assertEqual([cue.text for cue in cues], ["HELLO"])
        self.assertAlmostEqual(cues[0].start, 7 / 30.0, places=2)
        self.assertAlmostEqual(cues[0].end, 10 / 30.0, places=2)

    def test_duplicate_control_codes_are_ignored(self):
        decoder = cc._CaptionDecoder()
        decoder.feed(0x14, 0x20, 0.0)
        decoder.feed(0x14, 0x20, 0.033)
        decoder.feed(0x14, 0x20, 0.066)
        decoder.feed(0x14, 0x20, 0.7)   # beyond the duplicate window
        self.assertEqual(decoder.mode, "popon")

    def test_special_characters(self):
        decoder = cc._CaptionDecoder()
        decoder.feed(0x14, 0x20, 0.0)
        decoder.feed(0x14, 0x40, 0.033)
        decoder.feed(0x10, 0x27, 0.066)   # music note
        decoder.feed(0x10, 0x28, 0.099)   # a-grave
        decoder.feed(0x14, 0x2F, 0.132)
        cues = decoder.finish(5.0)
        self.assertEqual(cues[0].text, "\u266a\u00e0")

    def test_roll_up_scrolls_and_emits_new_cue(self):
        decoder = cc._CaptionDecoder()
        decoder.feed(0x14, 0x25, 0.0)     # RU2
        decoder.feed(0x14, 0x25, 0.033)
        decoder.feed(0x14, 0x40, 0.066)   # PAC row 14
        decoder.feed(0x14, 0x40, 0.099)
        decoder.feed(ord("O"), ord("N"), 0.132)
        decoder.feed(ord("E"), 0x20, 0.165)
        decoder.feed(0x14, 0x2D, 0.198)   # CR
        decoder.feed(0x14, 0x2D, 0.231)
        decoder.feed(ord("T"), ord("W"), 0.264)
        decoder.feed(ord("O"), 0x20, 0.297)
        cues = decoder.finish(2.0)
        self.assertEqual([cue.text for cue in cues], ["ONE", "ONE\nTWO"])
        self.assertAlmostEqual(cues[1].start, 0.198, places=2)


class ExtractSrtTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "title.m2v"
        self.srt = Path(self.tmp.name) / "captions.srt"

    def tearDown(self):
        self.tmp.cleanup()

    def test_extracts_subrip(self):
        self.path.write_bytes(elementary_stream(POP_ON))
        result = cc.extract_srt([str(self.path)], self.srt, 10.0)
        self.assertIsNotNone(result)
        text = self.srt.read_text(encoding="utf-8")
        self.assertIn("-->", text)
        self.assertIn("HELLO", text)
        self.assertTrue(text.startswith("1\n00:00:00"))

    def test_no_captions_returns_none_and_no_file(self):
        self.path.write_bytes(elementary_stream([(0x80, 0x80)] * 4))
        self.assertIsNone(cc.extract_srt([str(self.path)], self.srt, 10.0))
        self.assertFalse(self.srt.exists())

    def test_cancellation(self):
        self.path.write_bytes(elementary_stream(POP_ON))
        with self.assertRaises(KeyboardInterrupt):
            cc.extract_srt([str(self.path)], self.srt, 10.0,
                           cancelled=lambda: True)


@unittest.skipUnless(available(), "ffmpeg is required")
class DvdCaptionSaveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "disc"
        make_paths(cls.root)
        vob = Path(cls.tmp.name) / "raw.vob"
        subprocess.run([
            tool("ffmpeg"), "-nostdin", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i",
            "testsrc=duration=4:size=720x480:rate=30000/1001",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-c:v", "mpeg2video", "-b:v", "4M", "-c:a", "ac3",
            "-target", "ntsc-dvd", "-f", "dvd", str(vob),
        ], check=True)
        cls.blob = vob.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.settings = Settings(JsonBackend(Path(self.work.name) / "settings.json"))
        self.settings.set("app_MinTitleLength", 0)
        # Deliberately does not select subtitles: the auto-added CC rule must
        # still tick the caption track, like the reference profile.
        self.settings.set("app_DefaultSelectionString", "-sel:all,+sel:nolang")
        self.stream = io.StringIO()
        self.engine = Engine(RobotWriter(self.stream), self.settings,
                             builtin_default())

    def tearDown(self):
        self.engine.close_source()
        self.work.cleanup()

    def _install(self, blob: bytes) -> None:
        (self.root / "VIDEO_TS" / "VTS_01_1.VOB").write_bytes(blob)

    def test_caption_track_is_listed_and_saved(self):
        self._install(inject_dvd_captions(
            self.blob, [caption_payload(*pair) for pair in POP_ON]))
        result = self.engine.open_source(f"file:{self.root}", emit_info=True)
        self.assertIsNotNone(result.disc, self.stream.getvalue())
        assert result.disc is not None
        self.assertFalse(result.disc.encrypted)
        title = result.disc.titles[0]
        captions = [s for s in title.streams if s.codec_id == "S_CC608/DVD"]
        self.assertEqual(len(captions), 1)
        self.assertTrue(captions[0].selected)
        self.assertEqual(captions[0].index, len(title.streams) - 1)
        output = self.stream.getvalue()
        self.assertIn('SINFO:0,2,5,0,"S_CC608/DVD"', output)
        self.assertIn('SINFO:0,2,6,0,"CC"', output)
        self.assertIn('SINFO:0,2,7,0,"Closed Captions"', output)
        self.assertIn('SINFO:0,2,13,0,"9.8 Mb/s"', output)
        self.assertIn('SINFO:0,2,30,0,"CC\u2192Text English ( Lossy conversion )"',
                      output)
        self.assertIn('SINFO:0,2,34,0,"Text subtitles ( Lossy conversion )"', output)
        self.assertIn('SINFO:0,2,41,0,"Text"', output)
        self.assertIn('SINFO:0,2,42,5087,"( Lossy conversion )"', output)

        destination = Path(self.work.name) / "out"
        saved, code = self.engine.save_selected(destination)
        self.assertEqual((saved, code), (1, 0), self.stream.getvalue())
        files = list(destination.glob("*.mkv"))
        self.assertEqual(len(files), 1)
        info = ffprobe_json(str(files[0]))
        subtitles = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
        self.assertEqual(len(subtitles), 1)
        self.assertEqual(subtitles[0]["codec_name"], "subrip")
        self.assertAlmostEqual(float(info["format"]["duration"]), 4.0, delta=0.6)

    def test_uncaptioned_title_has_no_caption_track(self):
        self._install(self.blob)
        result = self.engine.open_source(f"file:{self.root}", emit_info=True)
        self.assertIsNotNone(result.disc, self.stream.getvalue())
        assert result.disc is not None
        title = result.disc.titles[0]
        self.assertFalse(any(s.codec_id == "S_CC608/DVD" for s in title.streams))
        self.assertNotIn("S_CC608/DVD", self.stream.getvalue())


if __name__ == "__main__":
    unittest.main()
