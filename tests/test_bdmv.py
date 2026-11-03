import struct
import unittest

from opticrip import bdmv


def make_mpls():
    # Each stream is an entry block (length byte, type, PID) followed by an
    # attribute block (length byte, coding type, layout, language).
    video = bytes([3, 1]) + struct.pack(">H", 0x1011)
    video_attr = bytes([5, 0x1B, 0x61, 0x00, 0x00, 0x00])   # H.264 1080p23.976
    audio = bytes([3, 1]) + struct.pack(">H", 0x1100)
    audio_attr = bytes([5, 0x81, 0x61]) + b"eng"           # AC-3 5.1 48 kHz
    subtitle = bytes([3, 1]) + struct.pack(">H", 0x1200)
    subtitle_attr = bytes([4, 0x90]) + b"eng"              # PGS
    entries = video + video_attr + audio + audio_attr + subtitle + subtitle_attr
    counts = bytes([1, 1, 1, 0, 0, 0, 0, 0])
    stn_body = b"\x00\x00" + counts + b"\x00" * 4 + entries
    stn = struct.pack(">H", len(stn_body)) + stn_body

    body = bytearray()
    body += b"00001"                      # clip_information_file_name
    body += b"M2TS"                       # clip_codec_identifier
    body += struct.pack(">H", 0)          # flags: single angle, connection 0
    body += bytes([1])                    # ref_to_STC_id
    body += struct.pack(">I", 0)          # IN_time
    body += struct.pack(">I", 45000 * 60)  # OUT_time = 60 s
    body += b"\x00" * 8                   # UO_mask_table
    body += bytes([0x80, 0x00])           # random access flag, still mode
    body += struct.pack(">H", 0)          # still time
    body += stn
    playitem = struct.pack(">H", len(body)) + bytes(body)
    table = struct.pack(">IHHH", 6 + len(playitem), 0, 1, 0) + playitem
    header = bytearray(0x40)
    header[0:8] = b"MPLS0200"
    struct.pack_into(">I", header, 8, 0x40)
    mark_start = 0x40 + len(table)
    struct.pack_into(">I", header, 12, mark_start)
    struct.pack_into(">H", header, 0x28, 1)
    # PlaylistMark(): length, count, then one 14-byte chapter mark.
    mark = struct.pack(">I", 0) + struct.pack(">H", 1)
    mark += bytes([0, 1]) + struct.pack(">H", 0)      # reserved, type, playitem
    mark += struct.pack(">I", 45000 * 30)             # timestamp
    mark += b"\xff\xff" + struct.pack(">I", 0)        # entry PID, duration
    return bytes(header) + table + mark


class BdmvTests(unittest.TestCase):
    def test_parse_playlist(self):
        parsed = bdmv.parse_mpls(make_mpls())
        self.assertEqual(len(parsed["playitems"]), 1)
        item = parsed["playitems"][0]
        self.assertEqual(item["clip"], "00001")
        self.assertAlmostEqual(item["duration"], 60.0)
        self.assertEqual([s.kind for s in item["streams"]],
                         ["video", "audio", "subtitle"])
        video = item["streams"][0]
        self.assertEqual(video.codec, "H.264")
        self.assertEqual(video.fps, "23.976")
        self.assertEqual(video.height, 1080)
        audio = item["streams"][1]
        self.assertEqual(audio.codec, "AC-3")
        self.assertEqual(audio.language, "eng")
        self.assertEqual(audio.pid, 0x1100)
        self.assertEqual(audio.channels, 6)
        self.assertEqual(audio.sample_rate, 48000)
        subtitle = item["streams"][2]
        self.assertEqual(subtitle.language, "eng")
        self.assertEqual(subtitle.pid, 0x1200)
        self.assertEqual(parsed["chapters"], [30.0])

    def test_wrong_identifier(self):
        with self.assertRaises(bdmv.BdmvError):
            bdmv.parse_mpls(b"XXXX0200" + b"\x00" * 64)


if __name__ == "__main__":
    unittest.main()
