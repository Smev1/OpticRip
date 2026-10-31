import struct
import tempfile
import unittest
from pathlib import Path

from opticrip.iso9660 import ISOImage, UnsupportedImage, sniff


def record(name: bytes, lba: int, size: int, is_dir: bool) -> bytes:
    length = 33 + len(name)
    if length % 2:
        length += 1
    blob = bytearray(length)
    blob[0] = length
    struct.pack_into("<I", blob, 2, lba)
    struct.pack_into(">I", blob, 6, lba)
    struct.pack_into("<I", blob, 10, size)
    struct.pack_into(">I", blob, 14, size)
    blob[25] = 0x02 if is_dir else 0
    blob[28:32] = struct.pack("<HH", 1, 1)
    blob[32] = len(name)
    blob[33:33 + len(name)] = name
    return bytes(blob)


def make_iso(path: Path) -> None:
    sectors = [b"\x00" * 2048 for _ in range(24)]

    def put(sector: int, data: bytes) -> None:
        sectors[sector] = data.ljust(2048, b"\x00")[:2048]

    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[8:40] = b"OPTICRIP".ljust(32)
    pvd[40:72] = b"TESTDISC".ljust(32)
    root = record(b"\x00", 18, 2048, True)
    pvd[156:156 + len(root)] = root
    put(16, bytes(pvd))
    term = bytearray(2048)
    term[0] = 255
    term[1:6] = b"CD001"
    term[6] = 1
    put(17, bytes(term))
    directory = record(b"\x00", 18, 2048, True) + record(b"\x01", 18, 2048, True)
    directory += record(b"HELLO.TXT;1", 20, 5, False)
    put(18, directory)
    put(20, b"hello")
    path.write_bytes(b"".join(sectors))


class IsoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.iso"
        make_iso(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sniff(self):
        self.assertEqual(sniff(self.path), "iso9660")

    def test_list_read_and_size(self):
        with ISOImage(self.path) as image:
            self.assertTrue(image.exists("HELLO.TXT"))
            self.assertEqual(image.size("hello.txt"), 5)
            self.assertEqual(image.read("HELLO.TXT"), b"hello")
            self.assertEqual(image.listdir("/"), ["HELLO.TXT"])

    def test_seekable_source_is_supported(self):
        class Source:
            name = "device"

            def __init__(self, data):
                self.data = data
                self.pos = 0
                self.closed = False

            def seek(self, offset, whence=0):
                self.pos = offset
                return self.pos

            def read(self, size=-1):
                chunk = self.data[self.pos:] if size < 0 else self.data[self.pos:self.pos + size]
                self.pos += len(chunk)
                return chunk

            def close(self):
                self.closed = True

        source = Source(self.path.read_bytes())
        with ISOImage(source=source) as image:
            self.assertEqual(image.read("HELLO.TXT"), b"hello")
        self.assertTrue(source.closed)

    def test_missing_file(self):
        with ISOImage(self.path) as image:
            self.assertFalse(image.exists("NOPE.TXT"))
            with self.assertRaises(FileNotFoundError):
                image.size("NOPE.TXT")

    def test_unsupported_image(self):
        junk = Path(self.tmp.name) / "junk.iso"
        junk.write_bytes(b"\x00" * 4096)
        self.assertEqual(sniff(junk), "unknown")
        with self.assertRaises(UnsupportedImage):
            ISOImage(junk)


if __name__ == "__main__":
    unittest.main()
