import struct
import unittest

from opticrip import bdmv


def make_mpls():
    video = bytes([6, 1]) + struct.pack(">H", 0x1011) + bytes([2, 0x0F])
    audio = bytes([9, 2]) + struct.pack(">H", 0x1100) + bytes([2, 0x30]) + b"eng"
    subtitle = bytes([7, 3]) + struct.pack(">H", 0x1200) + b"eng"
    entries = video + audio + subtitle
    stn = struct.pack(">HH", 12 + len(entries), 0) + bytes([1, 1, 1, 0, 0, 0, 0, 0]) + entries
    playitem = bytearray()
    playitem += struct.pack(">H", 26 + len(stn))
    playitem += struct.pack(">H", 1)              # clip id
    playitem += bytes([1, 1])                     # connection / stc
    playitem += struct.pack(">I", 0)              # in
    playitem += struct.pack(">I", 45000 * 60)     # out = 60 s
    playitem += b"\x00" * 8                       # uo mask
    playitem += bytes([0x80, 0x00])               # random access, still mode
    playitem += struct.pack(">H", 0)
    playitem += stn
    table = struct.pack(">IHHH", 10 + len(playitem), 0, 1, 0) + bytes(playitem)
    header = bytearray(0x40)
    header[0:8] = b"MPLS0200"
    struct.pack_into(">I", header, 8, 0x40)
    mark_start = 0x40 + len(table)
    struct.pack_into(">I", header, 12, mark_start)
    struct.pack_into(">H", header, 0x28, 1)
    mark = struct.pack(">H", 1) + b"\x00\x00"
    mark += bytes([1, 0]) + struct.pack(">H", 0)
    mark += struct.pack(">I", 45000 * 30) + struct.pack(">H", 0) + struct.pack(">I", 0)
    return bytes(header) + table + mark


class BdmvTests(unittest.TestCase):
    def test_parse_playlist(self):
        parsed = bdmv.parse_mpls(make_mpls())
        self.assertEqual(len(parsed["playitems"]), 1)
        item = parsed["playitems"][0]
        self.assertEqual(item["clip"], 1)
        self.assertAlmostEqual(item["duration"], 60.0)
        self.assertEqual([s.kind for s in item["streams"]],
                         ["video", "audio", "subtitle"])
        audio = item["streams"][1]
        self.assertEqual(audio.codec, "AC-3")
        self.assertEqual(audio.language, "eng")
        self.assertEqual(audio.pid, 0x1100)
        self.assertEqual(parsed["chapters"], [30.0])

    def test_wrong_identifier(self):
        with self.assertRaises(bdmv.BdmvError):
            bdmv.parse_mpls(b"XXXX0200" + b"\x00" * 64)


if __name__ == "__main__":
    unittest.main()
