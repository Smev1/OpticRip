import struct
import unittest

from opticrip import dvd


def make_pgc():
    pgc = bytearray(0x200)
    pgc[2] = 2          # programs
    pgc[3] = 2          # cells
    pgc[4:8] = bytes([0x00, 0x03, 0x43, 0xE8])   # 0:03:43 + 28 frames
    struct.pack_into(">H", pgc, 0xE6, 0x0100)    # program map
    struct.pack_into(">H", pgc, 0xE8, 0x0110)    # cell playback
    pgc[0x100:0x102] = bytes([1, 2])             # program 1 -> cell 1, program 2 -> cell 2
    cells = [
        (bytes([0x00, 0x03, 0x00, 0x00]), 100, 200),   # 3:00
        (bytes([0x00, 0x00, 0x43, 0xE8]), 201, 250),   # 0:43 + 28f
    ]
    for i, (time, first, last) in enumerate(cells):
        base = 0x110 + i * 24
        pgc[base + 4:base + 8] = time
        struct.pack_into(">I", pgc, base + 8, first)
        struct.pack_into(">I", pgc, base + 20, last)
    return bytes(pgc)


class DvdTests(unittest.TestCase):
    def test_decode_playtime(self):
        self.assertAlmostEqual(dvd.decode_playtime(bytes([0, 3, 0x43, 0xE8]), 0),
                               223 + 28 / 30, places=3)
        self.assertAlmostEqual(dvd.decode_playtime(bytes([0, 0, 0, 0xC0]), 0), 0.0)

    def test_decode_language_ascii_and_five_bit(self):
        self.assertEqual(dvd.decode_language(b"en", 0), "eng")
        self.assertEqual(dvd.decode_language(b"fr", 0), "fre")
        self.assertEqual(dvd.decode_language(bytes([0x15, 0xC7]), 0), "eng")
        self.assertEqual(dvd.decode_language(b"\x00\x00", 0), "und")

    def test_parse_pgc(self):
        pgc = dvd.parse_pgc(make_pgc(), 0)
        self.assertEqual(pgc["programs"], 2)
        self.assertEqual(pgc["cells"], 2)
        self.assertAlmostEqual(pgc["duration"], 223 + 28 / 30, places=2)
        self.assertEqual(pgc["chapters"], [0.0, 180.0])
        self.assertEqual(pgc["byte_size"], (101 + 50) * 2048)

    def test_cell_map_matches_reference(self):
        self.assertEqual(dvd._cell_map(1), "1")
        self.assertEqual(dvd._cell_map(2), "1,2")
        self.assertEqual(dvd._cell_map(17), "1-17")

    def test_cell_segments_map_across_vob_boundaries(self):
        class FakeFS:
            sizes = {"VIDEO_TS/VTS_01_1.VOB": 4096,
                     "VIDEO_TS/VTS_01_2.VOB": 2048}

            def size(self, name):
                return self.sizes[name]

        segments = dvd._cell_segments(
            FakeFS(),
            ["VIDEO_TS/VTS_01_1.VOB", "VIDEO_TS/VTS_01_2.VOB"],
            [1], [4096], [2.0],
        )
        self.assertEqual([(s.path, s.byte_offset, s.byte_size) for s in segments], [
            ("VIDEO_TS/VTS_01_1.VOB", 2048, 2048),
            ("VIDEO_TS/VTS_01_2.VOB", 0, 2048),
        ])

    def test_cell_segments_coalesce_contiguous_cells(self):
        class FakeFS:
            def size(self, name):
                return 8192

        segments = dvd._cell_segments(
            FakeFS(), ["VIDEO_TS/VTS_01_1.VOB"], [0, 1], [2048, 2048],
            [1.0, 1.0],
        )
        self.assertEqual(len(segments), 1)
        self.assertEqual((segments[0].byte_offset, segments[0].byte_size),
                         (0, 4096))

    def test_cell_segments_clamp_to_vob_size(self):
        class FakeFS:
            def size(self, name):
                return 4096

        segments = dvd._cell_segments(
            FakeFS(), ["VIDEO_TS/VTS_01_1.VOB"], [1], [999999], [2.0],
        )
        self.assertEqual([(s.byte_offset, s.byte_size) for s in segments],
                         [(2048, 2048)])

    def test_video_attributes(self):
        stream = dvd._video_stream(0x4E80)
        self.assertEqual(stream.codec, "MPEG-2")
        self.assertEqual((stream.width, stream.height), (720, 480))
        self.assertEqual(stream.aspect, "16:9")

    def test_audio_attributes(self):
        stream = dvd._audio_stream(0, bytes([0x04, 0xC1, 0x65, 0x6E, 0, 0, 0, 0]))
        self.assertEqual(stream.codec, "AC-3")
        self.assertEqual(stream.channels, 2)
        self.assertEqual(stream.language, "eng")
        self.assertEqual(stream.sample_rate, 48000)
        self.assertEqual(stream.channel_layout, "Stereo")

    def test_multichannel_layout_matches_reference(self):
        stream = dvd._audio_stream(0, bytes([0x04, 0xC5, 0x65, 0x6E, 0, 0, 0, 0]), 448000)
        self.assertEqual(stream.channels, 6)
        self.assertEqual(stream.channel_layout, "Surround 5.1")
        self.assertEqual(stream.bitrate, 448000)

    def test_video_reports_dvd_peak_bitrate(self):
        stream = dvd._video_stream(0x4E80)
        self.assertEqual(stream.bitrate, 9_800_000)

    def test_ac3_bitrates_from_clear_pes_headers(self):
        head = bytearray(3 * 2048)
        tracks = {0x80: 30, 0x81: 20}   # 448 kb/s and 192 kb/s
        for sector, (substream, code) in enumerate(tracks.items(), start=1):
            base = sector * 2048
            # MPEG-2 PES header: stream id, length, flags, header length 8,
            # five PTS bytes and three extension bytes, then the private
            # stream payload.
            head[base:base + 9] = b"\x00\x00\x01\xbd\x07\xec\x91\x81\x08"
            head[base + 17] = substream
            head[base + 18:base + 21] = b"\x02\x00\x01"
            head[base + 21:base + 23] = b"\x0b\x77"
            head[base + 25] = code
        rates = dvd._ac3_bitrates_from_head(bytes(head))
        self.assertEqual(rates, {0: 448000, 1: 192000})

    def test_scrambling_control_bits_detect_css(self):
        clear = bytearray(3 * 2048)
        clear[0:4] = b"\x00\x00\x01\xba"
        clear[0x0E:0x12] = b"\x00\x00\x01\xe0"
        clear[0x14] = 0x80          # not scrambled
        for sector in (1, 2):
            base = sector * 2048
            clear[base:base + 4] = b"\x00\x00\x01\xba"
            clear[base + 0x0E:base + 0x12] = b"\x00\x00\x01\xe0"
            clear[base + 0x14] = 0x91   # scrambled
        self.assertTrue(dvd.has_scrambled_sectors(bytes(clear)))
        clear[2048 + 0x14] = 0x80
        self.assertFalse(dvd.has_scrambled_sectors(bytes(clear[2048:])))

    def test_system_header_bytes_do_not_look_scrambled(self):
        sector = bytearray(2048)
        sector[0:4] = b"\x00\x00\x01\xba"
        sector[0x0E:0x12] = b"\x00\x00\x01\xbb"
        sector[0x12:0x14] = (12).to_bytes(2, "big")
        # A language byte in the system header happens to have bits 0x30
        # set; the PES flags that follow are clear.
        sector[0x14] = 0xA1
        pes = 0x0E + 6 + 12
        sector[pes:pes + 4] = b"\x00\x00\x01\xe0"
        sector[pes + 6] = 0x80
        self.assertFalse(dvd.has_scrambled_sectors(bytes(sector)))

    def test_looks_encrypted_uses_sector_flags(self):
        class FakeFS:
            def __init__(self, blob):
                self.blob = blob

            def listdir(self, _path):
                return ["VTS_01_1.VOB", "VTS_01_0.VOB"]

            def read(self, _path, _offset=0, _size=None):
                return self.blob

        blob = bytearray(3 * 2048)
        blob[0:4] = b"\x00\x00\x01\xba"
        blob[0x14] = 0x80
        for sector in (1, 2):
            base = sector * 2048
            blob[base:base + 4] = b"\x00\x00\x01\xba"
            blob[base + 0x14] = 0x91
        self.assertTrue(dvd.looks_encrypted(FakeFS(bytes(blob)), [1]))
        for sector in (1, 2):
            blob[sector * 2048 + 0x14] = 0x80
        blob[36:40] = b"\x00\x00\x01\xb3"
        self.assertFalse(dvd.looks_encrypted(FakeFS(bytes(blob)), [1]))

    def test_subtitle_attributes(self):
        stream = dvd._subtitle_stream(0, bytes([0x01, 0x00, 0x65, 0x6E, 0x00, 0x01]))
        self.assertEqual(stream.kind, "subtitle")
        self.assertEqual(stream.language, "eng")

    def test_commentary_code_extensions(self):
        audio = dvd._audio_stream(0, bytes([0x04, 0xC1, 0x65, 0x6E, 0x00, 0x03, 0x00, 0x00]))
        self.assertEqual(audio.flags, 1)
        subtitle = dvd._subtitle_stream(0, bytes([0x01, 0x00, 0x65, 0x6E, 0x00, 0x0D]))
        self.assertEqual(subtitle.flags, 1)

    def test_stream_order_sets_input_indices(self):
        vts = bytearray(0x300)
        vts[0x200:0x202] = struct.pack(">H", 0x4E80)
        vts[0x202:0x204] = struct.pack(">H", 1)
        vts[0x204:0x20C] = bytes([0x04, 0xC1, 0x65, 0x6E, 0, 0, 0, 0])
        vts[0x254:0x256] = struct.pack(">H", 1)
        vts[0x256:0x25C] = bytes([0x01, 0x00, 0x66, 0x72, 0x00, 0x01])
        streams = dvd.read_streams(bytes(vts))
        self.assertEqual([s.kind for s in streams], ["video", "audio", "subtitle"])
        self.assertEqual([s.source_index for s in streams], [0, 1, 2])

    def test_media_without_sequence_header_looks_encrypted(self):
        class FakeFS:
            def read(self, path, offset=0, size=None):
                return b"\x00\x00\x01\xba" + b"\x00" * 4096
            def listdir(self, path):
                return ["VTS_01_1.VOB"]
        self.assertTrue(dvd.looks_encrypted(FakeFS(), [1]))

    def test_clear_media_is_not_encrypted(self):
        class FakeFS:
            def read(self, path, offset=0, size=None):
                return b"\x00\x00\x01\xba" + b"\x00" * 8 + b"\x00\x00\x01\xb3" + b"\x00" * 4096
            def listdir(self, path):
                return ["VTS_01_1.VOB"]
        self.assertFalse(dvd.looks_encrypted(FakeFS(), [1]))


if __name__ == "__main__":
    unittest.main()
