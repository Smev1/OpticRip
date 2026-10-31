import io
import struct
import tempfile
import unittest
from pathlib import Path

from opticrip.discfs import IsoFS
from opticrip.engine import Engine
from opticrip.iso9660 import sniff
from opticrip.profiles import builtin_default
from opticrip.robot import RobotWriter
from opticrip.settings import JsonBackend, Settings
from opticrip.udf import UDFImage

try:
    from test_bdmv import make_mpls
except ImportError:  # direct module execution
    from tests.test_bdmv import make_mpls

SECTOR = 2048
TAG_PVD, TAG_ANCHOR, TAG_PARTITION, TAG_LVD = 1, 2, 5, 6
TAG_TERM, TAG_FSD, TAG_FID = 8, 256, 257
TAG_FE, TAG_EFE = 261, 266


def tag(tag_id, location, crc_length):
    out = bytearray(16)
    struct.pack_into("<HHBBHHHI", out, 0, tag_id, 3, 0, 0, 1, 0,
                     crc_length, location)
    out[4] = sum(out) & 0xFF
    return bytes(out)


def place(image, sector, payload):
    offset = sector * SECTOR
    end = offset + len(payload)
    if end > len(image):
        raise AssertionError(f"payload at {sector} overruns image")
    image[offset:end] = payload


def pvd(location):
    out = bytearray(SECTOR)
    out[0:16] = tag(TAG_PVD, location, 56)
    struct.pack_into("<II", out, 16, 1, 1)
    out[24] = 8
    out[25:28] = b"SYN"
    out[55] = 4
    return bytes(out)


def pd(location, start=300, length=300):
    out = bytearray(SECTOR)
    out[0:16] = tag(TAG_PARTITION, location, 196)
    struct.pack_into("<I", out, 16, 1)
    struct.pack_into("<H", out, 22, 0)
    struct.pack_into("<II", out, 188, start, length)
    return bytes(out)


def long_ad(block, partition, length=SECTOR):
    return struct.pack("<IIH", length, block, partition) + b"\x00" * 6


def lvd(location, fsd_block=0, fsd_partition=0, maps=b"", map_count=0):
    out = bytearray(SECTOR)
    out[0:16] = tag(TAG_LVD, location, 440 + len(maps))
    struct.pack_into("<I", out, 16, 1)
    out[84] = 8
    out[85:88] = b"LVI"
    out[211] = 4
    struct.pack_into("<I", out, 212, SECTOR)
    out[248:264] = long_ad(fsd_block, fsd_partition)
    struct.pack_into("<II", out, 264, len(maps), map_count)
    out[440:440 + len(maps)] = maps
    return bytes(out)


def type1_map(partition=0):
    return bytes([1, 6]) + struct.pack("<HH", 1, partition)


def metadata_map(metadata_lba, partition=0):
    body = bytearray([2, 48, 0, 0])
    identifier = b"*UDF Metadata Partition"
    body += identifier.ljust(32, b"\x00")
    body += b"\x00\x00"
    body += struct.pack("<HII", partition, metadata_lba, 0)
    return bytes(body)


def fsd(location, root_block, root_partition):
    out = bytearray(SECTOR)
    out[0:16] = tag(TAG_FSD, location, 416)
    out[28:32] = struct.pack("<HH", 3, 3)
    struct.pack_into("<II", out, 32, 1, 1)
    out[400:416] = long_ad(root_block, root_partition)
    return bytes(out)


def file_entry(location, file_type, info_length, ad_type, ad_area,
               extended=False):
    out = bytearray(SECTOR)
    out[0:16] = tag(TAG_EFE if extended else TAG_FE, location, 200)
    struct.pack_into("<H", out, 20, 4)
    struct.pack_into("<H", out, 24, 1)
    out[27] = file_type
    struct.pack_into("<H", out, 34, ad_type)
    struct.pack_into("<H", out, 48, 1)
    struct.pack_into("<Q", out, 56, info_length)
    if extended:
        struct.pack_into("<Q", out, 64, info_length)
        struct.pack_into("<I", out, 212, len(ad_area))
        start = 216
    else:
        struct.pack_into("<I", out, 172, len(ad_area))
        start = 176
    out[start:start + len(ad_area)] = ad_area
    return bytes(out)


def short_ad(block, length):
    return struct.pack("<II", length, block)


def fid(name, block, partition, is_dir):
    body = bytearray()
    body += struct.pack("<H", 1)
    body += bytes([0x02 if is_dir else 0x00])
    payload = bytes([8]) + name.encode("ascii")
    body.append(len(payload))
    body += long_ad(block, partition)
    body += b"\x00\x00"
    body += payload
    while (len(body) + 16) % 4:
        body.append(0)
    return tag(TAG_FID, 0, len(body)) + bytes(body)


def parent_fid(block, partition):
    body = bytearray(struct.pack("<H", 1) + bytes([0x0A, 0x00]))
    body += long_ad(block, partition) + b"\x00\x00"
    while (len(body) + 16) % 4:
        body.append(0)
    return tag(TAG_FID, 0, len(body)) + body


def build_udf(metadata=False):
    image = bytearray(700 * SECTOR)
    for index, name in enumerate((b"BEA01", b"NSR03", b"TEA01")):
        block = bytearray(SECTOR)
        block[1:6] = name
        block[6] = 1
        place(image, 16 + index, bytes(block))
    avdp = bytearray(SECTOR)
    avdp[0:16] = tag(TAG_ANCHOR, 256, 32)
    struct.pack_into("<II", avdp, 16, 4 * SECTOR, 257)
    place(image, 256, bytes(avdp))
    place(image, 257, pvd(257))
    place(image, 258, pd(258))
    if metadata:
        maps = type1_map() + metadata_map(50)
        place(image, 259, lvd(259, 0, 1, maps, 2))
    else:
        place(image, 259, lvd(259, 0, 0, type1_map(), 1))
    term = bytearray(SECTOR)
    term[0:16] = tag(TAG_TERM, 260, 16)
    place(image, 260, bytes(term))

    if metadata:
        # Metadata file entry at data partition block 50 (sector 350).
        meta = file_entry(350, 250, 20 * SECTOR, 0,
                          short_ad(100, 20 * SECTOR), extended=True)
        place(image, 350, meta)
        base = 400
        partition = 1
    else:
        base = 300
        partition = 0

    # File Set Descriptor and root directory.
    place(image, base, fsd(base, 1, partition))
    root_fids = parent_fid(1, partition)
    root_fids += fid("BDMV", 2, partition, True)
    root_fids += fid("TEST", 3, partition, False)
    place(image, base + 1, file_entry(base + 1, 4, len(root_fids), 3,
                                      root_fids))

    bdmv_fids = parent_fid(1, partition)
    bdmv_fids += fid("index.bdmv", 6, partition, False)
    bdmv_fids += fid("PLAYLIST", 7, partition, True)
    bdmv_fids += fid("STREAM", 9, partition, True)
    place(image, base + 2, file_entry(base + 2, 4, len(bdmv_fids), 3,
                                      bdmv_fids))

    place(image, base + 3, file_entry(base + 3, 5, 5, 3, b"HELLO"))
    place(image, base + 6, file_entry(base + 6, 5, 4, 3, b"IDX0"))
    playlist = make_mpls()
    place(image, base + 7, file_entry(base + 7, 4, len(
        parent_fid(1, partition) + fid("00001.mpls", 8, partition, False)), 3,
        parent_fid(1, partition) + fid("00001.mpls", 8, partition, False)))
    place(image, base + 8, file_entry(base + 8, 5, len(playlist), 3,
                                      playlist))
    stream_fids = parent_fid(1, partition) + fid("00001.m2ts", 10,
                                                 partition, False)
    place(image, base + 9, file_entry(base + 9, 4, len(stream_fids), 3,
                                      stream_fids))
    data = b"m2ts-payload" * 100
    if metadata:
        # File data lives in the physical partition, addressed by a long_ad.
        place(image, base + 10, file_entry(
            base + 10, 5, len(data), 1, long_ad(120, 0, len(data)), extended=True))
        place(image, 300 + 120, data)
    else:
        place(image, base + 10, file_entry(base + 10, 5, len(data), 0,
                                           short_ad(12, len(data))))
        place(image, 300 + 12, data)
    return bytes(image)


class UdfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bluray.iso"
        self.path.write_bytes(build_udf())

    def tearDown(self):
        self.tmp.cleanup()

    def test_sniff_and_listing(self):
        self.assertEqual(sniff(self.path), "udf")
        with UDFImage(self.path) as image:
            self.assertEqual(image.label, "SYN")
            self.assertTrue(image.exists("BDMV/index.bdmv"))
            self.assertTrue(image.isdir("BDMV/STREAM"))
            self.assertEqual(image.listdir("/"), ["BDMV", "TEST"])
            self.assertEqual(image.read("TEST"), b"HELLO")
            self.assertEqual(image.size("BDMV/STREAM/00001.m2ts"),
                             len(b"m2ts-payload" * 100))

    def test_data_is_read_in_ranges(self):
        with UDFImage(self.path) as image:
            data = b"m2ts-payload" * 100
            self.assertEqual(image.read("BDMV/STREAM/00001.m2ts", 5, 20),
                             data[5:25])
            self.assertEqual(image.read("BDMV/STREAM/00001.m2ts", 9999, 10),
                             b"")

    def test_iso_fs_uses_udf(self):
        with IsoFS(self.path) as fs:
            self.assertTrue(fs.exists("BDMV/PLAYLIST/00001.mpls"))
            self.assertEqual(fs.read("TEST"), b"HELLO")
            extracted = fs.extract(["BDMV/STREAM/00001.m2ts"],
                                   Path(self.tmp.name) / "out")
            self.assertEqual((extracted / "BDMV" / "STREAM" / "00001.m2ts")
                             .read_bytes(), b"m2ts-payload" * 100)

    def test_engine_opens_udf_bluray_image(self):
        settings = Settings(JsonBackend(Path(self.tmp.name) / "settings.json"))
        engine = Engine(RobotWriter(io.StringIO()), settings, builtin_default())
        try:
            result = engine.open_source(f"iso:{self.path}", min_length=0)
            self.assertIsNotNone(result.disc, result.path)
            assert result.disc is not None
            self.assertEqual(result.disc.kind, "Blu-ray")
            self.assertEqual(result.disc.title_count, 1)
            self.assertEqual(result.disc.titles[0].segments[0].path,
                             "BDMV/STREAM/00001.m2ts")
            self.assertEqual(result.disc.titles[0].duration, 60.0)
        finally:
            engine.close_source()

    def test_metadata_partition_is_followed(self):
        path = Path(self.tmp.name) / "bluray-meta.iso"
        path.write_bytes(build_udf(metadata=True))
        self.assertEqual(sniff(path), "udf")
        with UDFImage(path) as image:
            self.assertTrue(image.exists("BDMV/index.bdmv"))
            self.assertTrue(image.isdir("BDMV/STREAM"))
            self.assertEqual(image.read("TEST"), b"HELLO")
            self.assertEqual(image.read("BDMV/STREAM/00001.m2ts"),
                             b"m2ts-payload" * 100)


if __name__ == "__main__":
    unittest.main()
