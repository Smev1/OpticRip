import ctypes
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opticrip import aacs, bddecrypt, bdplus
from opticrip.aacs import AacsError, AacsHandle, stage_library as stage_aacs
from opticrip.bddecrypt import BlurayDecryptError, BlurayDecryptor
from opticrip.bdplus import BdplusError, stage_library as stage_bdplus
from opticrip.discfs import FolderFS
from opticrip.engine import Engine, OpenResult
from opticrip.ffmpeg import available, ffprobe_json, tool
from opticrip.model import Disc, Segment, Stream, Title
from opticrip.profiles import builtin_default
from opticrip.robot import RobotWriter
from opticrip.settings import JsonBackend, Settings

try:
    from test_bdmv import make_mpls
except ImportError:  # direct module execution
    from tests.test_bdmv import make_mpls

UNIT = aacs.UNIT_SIZE


class Symbol:
    """Callable stand-in for a ctypes function, with bindable signatures."""

    def __init__(self, function) -> None:
        self.function = function
        self.calls: list[tuple] = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.function(*args)


class FakeAacsModule:
    def __init__(self) -> None:
        self._vid = (ctypes.c_ubyte * 16)(*range(16))
        self._mk = (ctypes.c_ubyte * 16)(*range(16, 32))
        self.init = Symbol(lambda: 11)
        self.open_device = Symbol(lambda handle, path, key: 0)
        self.close = Symbol(lambda handle: None)
        self.decrypt = Symbol(self._decrypt)
        self.select = Symbol(lambda handle, title: None)
        self.get_vid = Symbol(lambda handle: ctypes.addressof(self._vid))
        self.get_mk = Symbol(lambda handle: ctypes.addressof(self._mk))
        self.mkb = Symbol(lambda handle: 68)

    @staticmethod
    def _decrypt(handle, buffer):
        for index in range(len(buffer)):
            buffer[index] ^= 0xAA
        return 1

    def install(self, library) -> None:
        library.aacs_init = self.init
        library.aacs_open_device = self.open_device
        library.aacs_close = self.close
        library.aacs_decrypt_unit = self.decrypt
        library.aacs_select_title = self.select
        library.aacs_get_vid = self.get_vid
        library.aacs_get_mk = self.get_mk
        library.aacs_get_mkb_version = self.mkb


class FakeBdplusLib:
    def __init__(self, convtab_result: int = 1, cached: int = 0) -> None:
        self.init = Symbol(lambda path, config, vid: 21)
        self.free = Symbol(lambda handle: None)
        self.set_mk = Symbol(lambda handle, key: None)
        self.mmap = Symbol(lambda handle, region, memory: None)
        self.psr = Symbol(lambda handle, regs, read, write: None)
        self.event = Symbol(lambda handle, event, p1, p2:
                            1 if event != bdplus.RUN_CONVTAB else convtab_result)
        self.is_cached = Symbol(lambda handle: cached)
        self.m2ts = Symbol(lambda handle, clip: 31)
        self.m2ts_close = Symbol(lambda stream: None)
        self.seek = Symbol(lambda stream, offset: 0)
        self.fixup = Symbol(self._fixup)
        self.code_date = Symbol(lambda handle: 20140101)

    @staticmethod
    def _fixup(stream, length, buffer):
        buffer[0] ^= 0x01
        return 1

    def install(self, library) -> None:
        library.bdplus_init = self.init
        library.bdplus_free = self.free
        library.bdplus_set_mk = self.set_mk
        library.bdplus_mmap = self.mmap
        library.bdplus_psr = self.psr
        library.bdplus_event = self.event
        library.bdplus_is_cached = self.is_cached
        library.bdplus_m2ts = self.m2ts
        library.bdplus_m2ts_close = self.m2ts_close
        library.bdplus_seek = self.seek
        library.bdplus_fixup = self.fixup
        library.bdplus_get_code_date = self.code_date


class CtypesModule:
    def __init__(self) -> None:
        self.attributes = {}

    def __setattr__(self, name, value):
        self.__dict__[name] = value


class AacsWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = FakeAacsModule()
        library = CtypesModule()
        self.fake.install(library)
        with mock.patch.object(aacs, "_load_shared", return_value=library):
            self.library = aacs.AacsLibrary.load(self.root / "libaacs.dll")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_modern_open_uses_path_and_keyfile(self):
        handle = self.library.open(str(self.root), self.root / "KEYDB.cfg")
        self.assertIsInstance(handle, AacsHandle)
        _handle, path, keyfile = self.fake.open_device.calls[-1]
        self.assertEqual(path, str(self.root).encode())
        self.assertEqual(keyfile, str(self.root / "KEYDB.cfg").encode())
        self.assertEqual(handle.volume_id, bytes(range(16)))
        self.assertEqual(handle.media_key, bytes(range(16, 32)))
        self.assertEqual(handle.mkb_version, 68)
        handle.close()
        handle.close()
        self.assertEqual(len(self.fake.close.calls), 1)

    def test_decrypt_unit_is_in_place(self):
        handle = self.library.open(str(self.root))
        unit = bytearray(range(256)) * (UNIT // 256)
        handle.decrypt_unit(unit)
        self.assertEqual(unit, bytes(value ^ 0xAA for value in range(256)) * (UNIT // 256))
        with self.assertRaises(ValueError):
            handle.decrypt_unit(bytearray(16))

    def test_decrypt_failure_raises(self):
        handle = self.library.open(str(self.root))
        self.fake.decrypt.function = lambda _handle, _buffer: 0
        with self.assertRaises(AacsError):
            handle.decrypt_unit(bytearray(UNIT))

    def test_old_open2_api_is_used_when_needed(self):
        library = CtypesModule()
        self.fake.install(library)
        del library.aacs_init
        del library.aacs_open_device
        opened = Symbol(lambda path, key, error: 7)
        library.aacs_open2 = opened
        with mock.patch.object(aacs, "_load_shared", return_value=library):
            loaded = aacs.AacsLibrary.load(self.root / "old.dll")
        handle = loaded.open(str(self.root))
        self.assertEqual(opened.calls[-1][0], str(self.root).encode())
        handle.close()

    def test_old_open2_error_code_is_reported(self):
        library = CtypesModule()
        self.fake.install(library)
        del library.aacs_init
        del library.aacs_open_device
        closed = Symbol(lambda handle: None)
        library.aacs_close = closed

        def fail(path, key, error):
            error._obj.value = -3
            return 7

        library.aacs_open2 = Symbol(fail)
        with mock.patch.object(aacs, "_load_shared", return_value=library):
            loaded = aacs.AacsLibrary.load(self.root / "old.dll")
        with self.assertRaises(AacsError) as caught:
            loaded.open(str(self.root))
        self.assertIn("matching processing key", str(caught.exception))
        self.assertEqual(len(closed.calls), 1)

    def test_library_without_api_is_rejected(self):
        with mock.patch.object(aacs, "_load_shared", return_value=CtypesModule()):
            with self.assertRaises(AacsError):
                aacs.AacsLibrary.load(self.root / "bad.dll")

    def test_stage_copies_and_rejects(self):
        source = self.root / "download" / "libaacs.dll"
        source.parent.mkdir()
        source.write_bytes(b"library")
        target = stage_aacs(source, self.root / "data", loader=lambda _path: object())
        self.assertEqual(target.read_bytes(), b"library")
        self.assertTrue(target.parent.name == "libaacs")

        bad = self.root / "bad.dll"
        bad.write_bytes(b"junk")

        def reject(_path):
            raise AacsError("missing aacs_init")

        with self.assertRaises(AacsError):
            stage_aacs(bad, self.root / "data", loader=reject)
        self.assertFalse((self.root / "data" / "libaacs" / "bad.dll").exists())

    def test_explicit_missing_library_is_an_error(self):
        with self.assertRaises(AacsError):
            aacs.resolve(self.root / "missing.dll", self.root)

    def test_resolve_uses_staged_copy(self):
        name = aacs._shared_library_names()[0]
        staged = self.root / "data" / "libaacs" / name
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"staged")
        sentinel = object()

        def fake_load(path):
            if Path(str(path)).name == name:
                return sentinel
            raise AacsError("not this one")

        with mock.patch.object(aacs.AacsLibrary, "load", side_effect=fake_load):
            library, used = aacs.resolve(None, self.root / "data")
        self.assertIs(library, sentinel)
        self.assertEqual(Path(str(used)), staged)


class BdplusWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = FakeBdplusLib()
        library = CtypesModule()
        self.fake.install(library)
        with mock.patch.object(bdplus, "_load_shared", return_value=library):
            self.library = bdplus.BdplusLibrary.load(self.root / "libbdplus.dll")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_open_starts_vm_and_builds_conversion_table(self):
        handle = self.library.open(self.root, b"V" * 16, b"M" * 16)
        _path, _config, vid = self.fake.init.calls[-1]
        self.assertEqual(bytes(vid), b"V" * 16)
        self.assertEqual(bytes(self.fake.set_mk.calls[-1][1]), b"M" * 16)
        events = [(call[1], call[2]) for call in self.fake.event.calls]
        self.assertIn((bdplus.EVENT_START, 0), events)
        self.assertIn((bdplus.RUN_CONVTAB, 1), events)
        self.assertEqual(len(self.fake.mmap.calls), 2)
        self.assertIsNotNone(self.fake.psr.calls[-1][2])
        registers = bytes(self.fake.psr.calls[-1][1])
        self.assertEqual(int.from_bytes(registers[15 * 4:16 * 4], "little"), 0xAAAA)
        self.assertEqual(int.from_bytes(registers[16 * 4:17 * 4], "little"), 0xFFFFFF)
        self.assertEqual(int.from_bytes(registers[31 * 4:32 * 4], "little"), 0x030200)
        handle.close()
        self.assertEqual(len(self.fake.free.calls), 1)

    def test_clip_seek_and_fixup(self):
        handle = self.library.open(self.root, b"V" * 16, b"M" * 16)
        stream = handle.clip(3)
        self.assertIsNotNone(stream)
        self.assertEqual(self.fake.m2ts.calls[-1][1], 3)
        self.assertEqual(self.fake.seek.calls[-1][1], 0)
        unit = bytearray([0x10]) + bytearray(UNIT - 1)
        self.assertEqual(stream.fixup(unit), 1)
        self.assertEqual(unit[0], 0x11)
        stream.close()
        handle.close()
        self.assertEqual(len(self.fake.m2ts_close.calls), 1)

    def test_cached_conversion_table_counts_as_success(self):
        library = CtypesModule()
        fake = FakeBdplusLib(convtab_result=0, cached=1)
        fake.install(library)
        with mock.patch.object(bdplus, "_load_shared", return_value=library):
            loaded = bdplus.BdplusLibrary.load(self.root / "bdplus.dll")
        handle = loaded.open(self.root, b"V" * 16, b"M" * 16)
        handle.close()

    def test_missing_conversion_table_is_an_error(self):
        library = CtypesModule()
        fake = FakeBdplusLib(convtab_result=0, cached=0)
        fake.install(library)
        with mock.patch.object(bdplus, "_load_shared", return_value=library):
            loaded = bdplus.BdplusLibrary.load(self.root / "bdplus.dll")
        with self.assertRaises(BdplusError):
            loaded.open(self.root, b"V" * 16, b"M" * 16)

    def test_old_library_without_events_is_refused_for_bdplus(self):
        library = CtypesModule()
        fake = FakeBdplusLib()
        fake.install(library)
        del library.bdplus_event
        del library.bdplus_is_cached
        library.bdplus_start = Symbol(lambda handle: 0)
        with mock.patch.object(bdplus, "_load_shared", return_value=library):
            loaded = bdplus.BdplusLibrary.load(self.root / "bdplus.dll")
        with self.assertRaises(BdplusError):
            loaded.open(self.root, b"V" * 16)

    def test_missing_config_is_reported(self):
        library = CtypesModule()
        fake = FakeBdplusLib()
        fake.init.function = lambda _path, _config, _vid: None
        fake.install(library)
        with mock.patch.object(bdplus, "_load_shared", return_value=library):
            loaded = bdplus.BdplusLibrary.load(self.root / "bdplus.dll")
        with self.assertRaises(BdplusError):
            loaded.open(self.root, b"V" * 16)

    def test_stage_copies_and_rejects(self):
        source = self.root / "download" / "libbdplus.dll"
        source.parent.mkdir()
        source.write_bytes(b"library")
        target = stage_bdplus(source, self.root / "data",
                              loader=lambda _path: object())
        self.assertEqual(target.read_bytes(), b"library")
        self.assertTrue(target.parent.name == "libbdplus")

        def reject(_path):
            raise BdplusError("missing bdplus_init")

        with self.assertRaises(BdplusError):
            stage_bdplus(source, self.root / "data", loader=reject)

    def test_resolve_uses_staged_copy(self):
        name = bdplus._shared_library_names()[0]
        staged = self.root / "data" / "libbdplus" / name
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"staged")
        sentinel = object()

        def fake_load(path):
            if Path(str(path)).name == name:
                return sentinel
            raise BdplusError("not this one")

        with mock.patch.object(bdplus.BdplusLibrary, "load", side_effect=fake_load):
            library, used = bdplus.resolve(None, self.root / "data")
        self.assertIs(library, sentinel)
        self.assertEqual(Path(str(used)), staged)


class FakeAacsHandle:
    def __init__(self) -> None:
        self.vid = b"V" * 16
        self.mk = b"M" * 16
        self.resets = 0
        self.closed = False
        self.units = 0

    @property
    def volume_id(self) -> bytes:
        return self.vid

    @property
    def media_key(self) -> bytes:
        return self.mk

    def reset(self) -> None:
        self.resets += 1

    def decrypt_unit(self, unit: bytearray) -> None:
        self.units += 1
        for index in range(len(unit)):
            unit[index] ^= 0xAA

    def close(self) -> None:
        self.closed = True


class FakeBdplusStream:
    def __init__(self) -> None:
        self.closed = False

    def fixup(self, unit: bytearray) -> int:
        unit[0] ^= 0x01
        return 1

    def close(self) -> None:
        self.closed = True


class FakeBdplusHandle:
    def __init__(self) -> None:
        self.clips: list[int] = []
        self.closed = False
        self.streams: list[FakeBdplusStream] = []

    def clip(self, number: int):
        self.clips.append(number)
        stream = FakeBdplusStream()
        self.streams.append(stream)
        return stream

    def close(self) -> None:
        self.closed = True


class FakeAacsOpenLibrary:
    def __init__(self) -> None:
        self.handle = FakeAacsHandle()
        self.target = ""
        self.keyfile = None

    def open(self, target, keyfile=None):
        self.target = str(target)
        self.keyfile = keyfile
        return self.handle


class FakeBdplusOpenLibrary:
    def __init__(self) -> None:
        self.handle = FakeBdplusHandle()
        self.target = ""
        self.vid = b""
        self.mk = b""

    def open(self, target, vid, media_key=None, num_titles=1,
             run_conversion_table=True):
        self.target = str(target)
        self.vid = vid
        self.mk = media_key
        return self.handle


def make_root(directory: Path, bdplus: bool = True) -> Path:
    root = directory / "disc"
    (root / "AACS").mkdir(parents=True)
    (root / "AACS" / "Unit_Key_RO.inf").write_bytes(b"unit keys")
    if bdplus:
        (root / "BDSVM").mkdir()
        (root / "BDSVM" / "00000.svm").write_bytes(b"svm")
    stream = root / "BDMV" / "STREAM"
    stream.mkdir(parents=True)
    (stream / "00001.m2ts").write_bytes(bytes(range(256)) * 48)
    return root


class BlurayDecryptorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self.tmp.name))
        self.aacs_library = FakeAacsOpenLibrary()
        self.bd_library = FakeBdplusOpenLibrary()
        self.messages: list[tuple] = []
        self.fs = FolderFS(self.root)
        self.settings = Settings(JsonBackend(Path(self.tmp.name) / "settings.json"))

    def tearDown(self) -> None:
        self.fs.close()
        self.tmp.cleanup()

    def make_decryptor(self, bdplus_available: bool = True) -> BlurayDecryptor:
        decryptor = BlurayDecryptor(self.fs, self.settings,
                                    lambda ident, *params: self.messages.append((ident, params)))
        patches = [
            mock.patch.object(bddecrypt.aacslib, "resolve",
                              return_value=(self.aacs_library, "fake libaacs")),
        ]
        if bdplus_available:
            patches.append(mock.patch.object(
                bddecrypt.bdpluslib, "resolve",
                return_value=(self.bd_library, "fake libbdplus")))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return decryptor

    def test_prepare_opens_aacs_and_bdplus(self):
        decryptor = self.make_decryptor()
        decryptor.prepare(num_titles=5)
        self.assertTrue(decryptor.active)
        self.assertEqual(self.aacs_library.target, str(self.root))
        self.assertEqual(self.bd_library.target, str(self.root))
        self.assertEqual(self.bd_library.vid, self.aacs_library.handle.vid)
        self.assertEqual(self.bd_library.mk, self.aacs_library.handle.mk)
        self.assertEqual(self.bd_library.handle.clips, [])
        self.assertTrue(any(ident == 9131 for ident, _ in self.messages))
        self.assertTrue(any(ident == 9135 for ident, _ in self.messages))
        decryptor.close()
        self.assertTrue(self.aacs_library.handle.closed)
        self.assertTrue(self.bd_library.handle.closed)

    def test_prepare_after_close_is_refused(self):
        decryptor = self.make_decryptor()
        decryptor.prepare()
        decryptor.close()
        with self.assertRaises(BlurayDecryptError):
            decryptor.prepare()

    def test_stage_decrypts_and_patches_units(self):
        decryptor = self.make_decryptor()
        reports: list[int] = []
        paths, temporary = decryptor.stage(
            [Segment("BDMV/STREAM/00001.m2ts")], Path(self.tmp.name),
            lambda done, _total: reports.append(done),
        )
        try:
            self.assertEqual(reports[0], 0)
            self.assertEqual(reports[-1], 2 * UNIT)
            self.assertEqual(reports, sorted(reports))
            data = Path(paths[0]).read_bytes()
            source = (self.root / "BDMV" / "STREAM" / "00001.m2ts").read_bytes()
            expected = bytearray()
            for offset in range(0, len(source), UNIT):
                unit = bytearray(source[offset:offset + UNIT])
                for index in range(len(unit)):
                    unit[index] ^= 0xAA
                unit[0] ^= 0x01
                expected.extend(unit)
            self.assertEqual(data, bytes(expected))
            self.assertEqual(self.aacs_library.handle.resets, 1)
            self.assertEqual(self.aacs_library.handle.units, 2)
            self.assertEqual(self.bd_library.handle.clips, [1])
            self.assertTrue(all(stream.closed for stream in self.bd_library.handle.streams))
        finally:
            temporary.cleanup()
            decryptor.close()

    def test_aacs_only_disc_does_not_need_libbdplus(self):
        plain_root = make_root(Path(self.tmp.name) / "plain", bdplus=False)
        self.fs.close()
        self.fs = FolderFS(plain_root)
        decryptor = BlurayDecryptor(self.fs, self.settings)
        self.addCleanup(decryptor.close)
        with mock.patch.object(bddecrypt.aacslib, "resolve",
                               return_value=(self.aacs_library, "fake libaacs")), \
                mock.patch.object(bddecrypt.bdpluslib, "resolve",
                                  side_effect=AssertionError("BD+ should not load")):
            decryptor.prepare()
            paths, temporary = decryptor.stage([Segment("BDMV/STREAM/00001.m2ts")])
            try:
                source = (plain_root / "BDMV" / "STREAM" / "00001.m2ts").read_bytes()
                self.assertEqual(Path(paths[0]).read_bytes(),
                                 bytes(value ^ 0xAA for value in source))
            finally:
                temporary.cleanup()

    def test_metadata_is_materialised_when_source_has_no_local_path(self):
        class RemoteFS(FolderFS):
            def __init__(self, root: Path) -> None:
                super().__init__(root)
                self.root = str(root / "image.iso")

            def local(self, path: str):
                return None

        self.fs.close()
        self.fs = RemoteFS(self.root)
        decryptor = self.make_decryptor()
        decryptor.prepare()
        self.assertNotEqual(self.aacs_library.target, str(self.root))
        materialised = Path(self.aacs_library.target)
        self.assertTrue((materialised / "AACS" / "Unit_Key_RO.inf").is_file())
        self.assertTrue((materialised / "BDSVM" / "00000.svm").is_file())
        paths, temporary = decryptor.stage([Segment("BDMV/STREAM/00001.m2ts")])
        try:
            self.assertEqual(Path(paths[0]).stat().st_size, 2 * UNIT)
        finally:
            temporary.cleanup()
            decryptor.close()
        self.assertFalse(materialised.exists())

    def test_nested_bdsvm_is_normalised_for_libbdplus(self):
        nested = Path(self.tmp.name) / "nested"
        (nested / "AACS").mkdir(parents=True)
        (nested / "AACS" / "Unit_Key_RO.inf").write_bytes(b"unit keys")
        (nested / "BDMV" / "BDSVM").mkdir(parents=True)
        (nested / "BDMV" / "BDSVM" / "00000.svm").write_bytes(b"svm")
        stream = nested / "BDMV" / "STREAM"
        stream.mkdir(parents=True)
        (stream / "00001.m2ts").write_bytes(bytes(range(256)) * 48)
        self.fs.close()
        self.fs = FolderFS(nested)
        decryptor = self.make_decryptor()
        self.addCleanup(decryptor.close)
        decryptor.prepare()
        # libbdplus only loads BDSVM/00000.svm from the root it is given.
        self.assertNotEqual(self.bd_library.target, str(nested))
        materialised = Path(self.bd_library.target)
        self.assertTrue((materialised / "AACS" / "Unit_Key_RO.inf").is_file())
        self.assertTrue((materialised / "BDSVM" / "00000.svm").is_file())

    def test_decrypt_failure_is_reported_and_cleans_staged_files(self):
        decryptor = self.make_decryptor()
        calls = {"count": 0}

        def fail_on_second_unit(unit):
            calls["count"] += 1
            if calls["count"] == 2:
                raise AacsError("bad unit key")
            for index in range(len(unit)):
                unit[index] ^= 0xAA

        self.aacs_library.handle.decrypt_unit = fail_on_second_unit
        with self.assertRaises(BlurayDecryptError) as caught:
            decryptor.stage([Segment("BDMV/STREAM/00001.m2ts")],
                            Path(self.tmp.name))
        self.assertIn("bad unit key", str(caught.exception))
        self.assertFalse(list(Path(self.tmp.name).glob("opticrip-bd-*")))
        decryptor.close()

    def test_cancel_cleans_staged_files(self):
        decryptor = self.make_decryptor()
        reports: list[int] = []

        def progress(done, _total):
            reports.append(done)

        with self.assertRaises(KeyboardInterrupt):
            decryptor.stage(
                [Segment("BDMV/STREAM/00001.m2ts")], Path(self.tmp.name),
                progress, lambda: any(value > 0 for value in reports),
            )
        self.assertFalse(list(Path(self.tmp.name).glob("opticrip-bd-*")))
        decryptor.close()


class EngineBlurayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.stream = io.StringIO()
        self.settings = Settings(JsonBackend(self.dir / "settings.json"))
        self.engine = Engine(RobotWriter(self.stream), self.settings,
                             builtin_default())
        root = make_root(self.dir)
        self.engine.fs = FolderFS(root)
        self.title = Title(index=0, duration=2.0, source="bluray",
                           streams=[Stream(kind="video", index=0, codec="H.264")],
                           segments=[Segment("BDMV/STREAM/00001.m2ts")])
        self.engine.disc = Disc(kind="Blu-ray", encrypted=True,
                                titles=[self.title], title_count=1)

    def tearDown(self) -> None:
        self.engine.close_source()
        self.tmp.cleanup()

    def test_missing_libaacs_refuses_save_with_reference_messages(self):
        with mock.patch.object(bddecrypt.aacslib, "resolve",
                               side_effect=AacsError("no libaacs")):
            path = self.engine.save_title(self.title, self.dir / "out")
        self.assertIsNone(path)
        output = self.stream.getvalue()
        self.assertIn("MSG:9130", output)
        self.assertIn("MSG:9100", output)

    def test_resolve_inputs_delegates_to_decryptor(self):
        class FakeDecryptor:
            active = True

            def stage(self, segments, workdir=None, on_progress=None, cancelled=None):
                return ["/tmp/clear.m2ts"], None

        paths, temporary = self.engine._resolve_inputs(
            self.title, decryptor=FakeDecryptor())
        self.assertEqual(paths, ["/tmp/clear.m2ts"])
        self.assertIsNone(temporary)

    def test_encrypted_bluray_without_decryptor_is_not_staged(self):
        paths, temporary = self.engine._resolve_inputs(self.title)
        try:
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].endswith("00001.m2ts"))
        finally:
            if temporary is not None:
                temporary.cleanup()


@unittest.skipUnless(available(), "ffmpeg is required")
class BluraySaveEndToEndTests(unittest.TestCase):
    """Staging + remux through the real engine, with fake AACS/BD+."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        raw = base / "raw.ts"
        subprocess.run([
            tool("ffmpeg"), "-nostdin", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", "-f", "mpegts", str(raw),
        ], check=True)
        blob = raw.read_bytes()
        blob += b"\x00" * ((-len(blob)) % UNIT)
        cls.root = base / "disc"
        (cls.root / "AACS").mkdir(parents=True)
        (cls.root / "AACS" / "Unit_Key_RO.inf").write_bytes(b"keys")
        (cls.root / "BDSVM").mkdir()
        (cls.root / "BDSVM" / "00000.svm").write_bytes(b"svm")
        stream_dir = cls.root / "BDMV" / "STREAM"
        stream_dir.mkdir(parents=True)
        (stream_dir / "00001.m2ts").write_bytes(
            bytes(value ^ 0xAA for value in blob))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_save_decrypts_stages_and_remuxes(self):
        base = Path(self.tmp.name)
        log = io.StringIO()
        engine = Engine(RobotWriter(log),
                        Settings(JsonBackend(base / "settings.json")),
                        builtin_default())
        self.addCleanup(engine.close_source)
        engine.fs = FolderFS(self.root)
        title = Title(
            index=0, duration=2.0, source="bluray",
            streams=[Stream(kind="video", index=0, codec="H.264",
                            codec_id="V_MPEG4/ISO/AVC", source_index=0),
                     Stream(kind="audio", index=1, codec="AAC",
                            codec_id="A_AAC", source_index=1)],
            segments=[Segment("BDMV/STREAM/00001.m2ts")],
        )
        engine.disc = Disc(kind="Blu-ray", label="TEST", encrypted=True,
                           titles=[title], title_count=1)
        progress: list[int] = []
        engine.on_progress = lambda current, _total: progress.append(current)
        with mock.patch.object(
                bddecrypt.aacslib, "resolve",
                return_value=(FakeAacsOpenLibrary(), "fake libaacs")), \
                mock.patch.object(
                    bddecrypt.bdpluslib, "resolve",
                    return_value=(FakeBdplusOpenLibrary(), "fake libbdplus")):
            path = engine.save_title(title, base / "out")
        self.assertIsNotNone(path, log.getvalue())
        assert path is not None
        info = ffprobe_json(str(path))
        self.assertEqual([stream["codec_type"] for stream in info["streams"]],
                         ["video", "audio"])
        self.assertAlmostEqual(float(info["format"]["duration"]), 2.0, delta=0.4)
        # Staging owns the first half of the bar, remuxing the second half.
        self.assertTrue(any(value < 32768 for value in progress))
        self.assertTrue(any(value >= 32768 for value in progress))
        self.assertEqual(progress, sorted(progress))
        self.assertFalse(list((base / "out").glob("opticrip-bd-*")))
        self.assertIn("MSG:9131", log.getvalue())
        self.assertIn("MSG:9135", log.getvalue())


def make_mkb(mkb_type: int, version: int = 76) -> bytes:
    """One MKB record 0x10 with a type/version, as AACS/MKBROM.AACS."""
    return (bytes([0x10]) + (12).to_bytes(3, "big")
            + int(mkb_type).to_bytes(4, "big")
            + int(version).to_bytes(4, "big"))


def make_uhd_tree(root: Path) -> None:
    (root / "BDMV" / "PLAYLIST").mkdir(parents=True)
    (root / "BDMV" / "STREAM").mkdir()
    (root / "BDMV" / "index.bdmv").write_bytes(b"INDX0200" + b"\x00" * 64)
    (root / "BDMV" / "PLAYLIST" / "00001.mpls").write_bytes(make_mpls())
    (root / "BDMV" / "STREAM" / "00001.m2ts").write_bytes(b"clip" * 100)
    aacs_dir = root / "AACS"
    aacs_dir.mkdir()
    (aacs_dir / "MKBROM.AACS").write_bytes(
        make_mkb(aacs.MKB_20_CATEGORY_C))


class MkbTests(unittest.TestCase):
    def test_parse_type_and_version(self):
        self.assertEqual(aacs.parse_mkb(make_mkb(aacs.MKB_TYPE_4, 68)),
                         (aacs.MKB_TYPE_4, 68))
        self.assertEqual(aacs.parse_mkb(make_mkb(aacs.MKB_20_CATEGORY_C, 61)),
                         (aacs.MKB_20_CATEGORY_C, 61))

    def test_record_walk_skips_other_records(self):
        other = b"\x02" + (8).to_bytes(3, "big") + b"sig!"
        self.assertEqual(aacs.parse_mkb(other + make_mkb(aacs.MKB_21_CATEGORY_C, 72)),
                         (aacs.MKB_21_CATEGORY_C, 72))

    def test_truncated_file_is_zero(self):
        self.assertEqual(aacs.parse_mkb(b"\x10\x00\x00"), (0, 0))
        self.assertEqual(aacs.parse_mkb(b""), (0, 0))

    def test_generation_labels(self):
        self.assertEqual(aacs.mkb_generation(aacs.MKB_20_CATEGORY_C), "2.0")
        self.assertEqual(aacs.mkb_generation(aacs.MKB_21_CATEGORY_C), "2.1")
        self.assertEqual(aacs.mkb_generation(aacs.MKB_TYPE_4), "")


class UhdDiscTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "uhd"
        make_uhd_tree(self.root)
        self.settings = Settings(JsonBackend(Path(self.tmp.name) / "settings.json"))
        self.settings.set("app_MinTitleLength", 0)
        self.log = io.StringIO()
        self.engine = Engine(RobotWriter(self.log), self.settings,
                             builtin_default())

    def tearDown(self):
        self.engine.close_source()
        self.tmp.cleanup()

    def test_uhd_disc_is_identified(self):
        result = self.engine.open_source(f"file:{self.root}")
        self.assertIsNotNone(result.disc, self.log.getvalue())
        assert result.disc is not None
        self.assertTrue(result.disc.uhd)
        self.assertEqual(result.disc.aacs_generation, "2.0")
        self.assertEqual(result.disc.protection, "AACS 2.0 (UHD)")
        self.assertTrue(result.disc.encrypted)

    def test_uhd_disc_is_refused_for_save_and_backup(self):
        result = self.engine.open_source(f"file:{self.root}")
        self.assertIsNotNone(result.disc, self.log.getvalue())
        saved, code = self.engine.save_selected(Path(self.tmp.name) / "out")
        self.assertEqual((saved, code), (0, 11))
        self.assertIn("MSG:9143", self.log.getvalue())
        self.assertIn("AACS 2.0", self.log.getvalue())
        self.log.seek(0)
        self.log.truncate()

        def fake_open(_rest, _min_length, _emit_info):
            fs = FolderFS(self.root)
            self.engine.fs = fs
            return OpenResult(result.disc, 0, fs, "disc:0")

        with mock.patch.object(self.engine, "_open_disc", side_effect=fake_open):
            ok, backup_code = self.engine.backup("disc:0",
                                                 Path(self.tmp.name) / "copy")
        self.assertFalse(ok)
        self.assertEqual(backup_code, 11)
        self.assertIn("MSG:9143", self.log.getvalue())


if __name__ == "__main__":
    unittest.main()
