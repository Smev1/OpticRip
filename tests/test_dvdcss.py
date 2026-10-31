import io
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opticrip import dvdcss
from opticrip.discfs import CssFS
from opticrip.dvdcss import DvdcssError, DvdcssFile, stage_library
from opticrip.engine import Engine
from opticrip.model import Disc, Segment, Stream, Title
from opticrip.profiles import builtin_default
from opticrip.robot import RobotWriter
from opticrip.settings import JsonBackend, Settings


SECTOR = 2048


class FakeHandle:
    """Decrypts by XOR, enough to prove which reads ask for decryption."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.closed = False
        self.mpeg_seeks: list[tuple[int, bool]] = []
        self.decrypt_reads: list[bool] = []

    def seek(self, block: int, mpeg: bool = False) -> int:
        self.pos = block * SECTOR
        self.mpeg_seeks.append((block, mpeg))
        return self.pos

    def read(self, blocks: int, decrypt: bool = False) -> bytes:
        raw = self.data[self.pos:self.pos + blocks * SECTOR]
        self.decrypt_reads.append(decrypt)
        if decrypt:
            raw = bytes(value ^ 0xAA for value in raw)
        self.pos += len(raw)
        return raw

    def close(self) -> None:
        self.closed = True


class FakeLibrary:
    def __init__(self, handle: FakeHandle) -> None:
        self.handle = handle
        self.target = ""

    def open(self, target: str) -> FakeHandle:
        self.target = target
        return self.handle


def directory_record(name: bytes, lba: int, size: int, is_dir: bool) -> bytes:
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


def make_css_iso(path: Path, clear_vob: bytes) -> None:
    sectors = [b"\x00" * SECTOR for _ in range(24)]

    def put(number: int, data: bytes) -> None:
        sectors[number] = data.ljust(SECTOR, b"\x00")[:SECTOR]

    pvd = bytearray(SECTOR)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[8:40] = b"OPTICRIP".ljust(32)
    pvd[40:72] = b"CSSDISC".ljust(32)
    root = directory_record(b"\x00", 18, SECTOR, True)
    pvd[156:156 + len(root)] = root
    put(16, bytes(pvd))

    term = bytearray(SECTOR)
    term[0] = 255
    term[1:6] = b"CD001"
    term[6] = 1
    put(17, bytes(term))

    root_dir = directory_record(b"\x00", 18, SECTOR, True)
    root_dir += directory_record(b"\x01", 18, SECTOR, True)
    root_dir += directory_record(b"VIDEO_TS", 19, SECTOR, True)
    put(18, root_dir)

    video_dir = directory_record(b"\x00", 19, SECTOR, True)
    video_dir += directory_record(b"\x01", 18, SECTOR, True)
    video_dir += directory_record(b"VTS_01_1.VOB", 20, len(clear_vob), False)
    put(19, video_dir)

    # A CSS VOB's clear pack header remains, but the payload is scrambled.
    scrambled = bytes(value ^ 0xAA for value in clear_vob)
    put(20, scrambled)
    path.write_bytes(b"".join(sectors))


class DvdcssFileTests(unittest.TestCase):
    def test_sector_aligned_reads_and_decryption_flag(self):
        data = bytes(range(256)) * 24
        handle = FakeHandle(data)
        stream = DvdcssFile(handle, "D:")
        stream.set_mpeg(True)
        stream.seek(100)
        got = stream.read(2100)
        self.assertEqual(got, bytes(value ^ 0xAA for value in data[100:2200]))
        self.assertIn((0, True), handle.mpeg_seeks)
        self.assertTrue(any(handle.decrypt_reads))

        stream.set_mpeg(False)
        stream.seek(100)
        self.assertEqual(stream.read(2100), data[100:2200])
        self.assertEqual(handle.decrypt_reads[-1], False)

    def test_seek_does_not_move_past_end(self):
        stream = DvdcssFile(FakeHandle(b"x" * (2 * SECTOR)))
        with self.assertRaises(OSError):
            stream.seek(-1)
        with self.assertRaises(OSError):
            stream.seek(0, 2)

    def test_close_is_idempotent(self):
        handle = FakeHandle(b"")
        stream = DvdcssFile(handle)
        stream.close()
        stream.close()
        self.assertTrue(handle.closed)
        with self.assertRaises(OSError):
            stream.read(1)


class CssFSTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clear = b"\x00\x00\x01\xba" + bytes(range(256)) * 8
        self.clear = self.clear[:SECTOR].ljust(SECTOR, b"\x00")
        image = Path(self.tmp.name) / "css.iso"
        make_css_iso(image, self.clear)
        self.handle = FakeHandle(image.read_bytes())
        self.library = FakeLibrary(self.handle)
        self.css = CssFS("D:", self.library)

    def tearDown(self):
        self.css.close()
        self.tmp.cleanup()

    def test_metadata_is_not_decrypted_but_vob_is(self):
        self.assertTrue(self.css.exists("VIDEO_TS/VTS_01_1.VOB"))
        self.assertEqual(self.css.read("VIDEO_TS/VTS_01_1.VOB"), self.clear)
        self.assertIn(False, self.handle.decrypt_reads)
        self.assertIn(True, self.handle.decrypt_reads)
        self.assertIn((20, True), self.handle.mpeg_seeks)

    def test_extract_materialises_clear_vob(self):
        destination = Path(self.tmp.name) / "out"
        self.css.extract(["VIDEO_TS/VTS_01_1.VOB"], destination)
        target = destination / "VIDEO_TS" / "VTS_01_1.VOB"
        self.assertEqual(target.read_bytes(), self.clear)

    def test_close_releases_device(self):
        self.css.close()
        self.assertTrue(self.handle.closed)

    def test_engine_materialises_css_segments_for_ffmpeg(self):
        engine = Engine(RobotWriter(io.StringIO()),
                        Settings(JsonBackend(Path(self.tmp.name) / "settings.json")),
                        builtin_default())
        engine.fs = self.css
        title = Title(index=0, segments=[Segment("VIDEO_TS/VTS_01_1.VOB")],
                      streams=[Stream(kind="video", index=0)])
        paths, temporary = engine._resolve_inputs(title)
        self.assertIsNotNone(temporary)
        assert temporary is not None
        try:
            self.assertEqual(Path(paths[0]).read_bytes(), self.clear)
        finally:
            temporary.cleanup()


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stage_validates_and_copies(self):
        source = self.root / "download" / "libdvdcss-2.dll"
        source.parent.mkdir()
        source.write_bytes(b"shared library")
        seen: list[Path] = []

        def loader(path):
            seen.append(Path(path))
            return object()

        target = stage_library(source, self.root / "data", loader=loader)
        self.assertEqual(target, self.root / "data" / "libdvdcss" / source.name)
        self.assertEqual(target.read_bytes(), b"shared library")
        self.assertEqual(seen, [source])

    def test_stage_rejects_invalid_library(self):
        source = self.root / "bad.dll"
        source.write_bytes(b"not a library")

        def loader(_path):
            raise DvdcssError("missing dvdcss_open")

        with self.assertRaises(DvdcssError):
            stage_library(source, self.root / "data", loader=loader)
        self.assertFalse((self.root / "data" / "libdvdcss" / source.name).exists())

    def test_explicit_missing_library_is_an_error(self):
        with self.assertRaises(DvdcssError):
            dvdcss.resolve(self.root / "missing.dll", self.root)

    def test_resolve_uses_staged_copy(self):
        name = dvdcss._shared_library_names()[0]
        staged = self.root / "data" / "libdvdcss" / name
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"staged")
        sentinel = object()

        def fake_load(path):
            if Path(str(path)).name == name:
                return sentinel
            raise DvdcssError("not this one")

        with mock.patch.object(dvdcss.DvdcssLibrary, "load", side_effect=fake_load):
            library, used = dvdcss.resolve(None, self.root / "data")
        self.assertIs(library, sentinel)
        self.assertEqual(Path(str(used)), staged)


if __name__ == "__main__":
    unittest.main()
