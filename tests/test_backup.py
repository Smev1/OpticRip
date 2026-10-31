import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opticrip import backup as backuplib
from opticrip.backup import BackupError, BackupWriter, is_bluray_clip
from opticrip.discfs import FolderFS
from opticrip.engine import Engine, OpenResult
from opticrip.model import Disc
from opticrip.profiles import builtin_default
from opticrip.robot import RobotWriter
from opticrip.settings import JsonBackend, Settings


class FakeDecryptor:
    active = True

    def __init__(self, fs):
        self.fs = fs
        self.calls = []

    def decrypt_clip(self, path, target, on_progress=None, cancelled=None,
                     reset_unit=True):
        self.calls.append((path, reset_unit))
        if cancelled is not None and cancelled():
            raise KeyboardInterrupt
        data = self.fs.read(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(value ^ 0xAA for value in data))
        if on_progress is not None:
            on_progress(len(data), len(data))
        return len(data)


def make_bluray(root: Path, nested_metadata: bool = False) -> FolderFS:
    aacs = root / ("BDMV/AACS" if nested_metadata else "AACS")
    aacs.mkdir(parents=True)
    (aacs / "Unit_Key_RO.inf").write_bytes(b"unit-keys")
    svm = root / ("BDMV/BDSVM" if nested_metadata else "BDSVM")
    svm.mkdir(parents=True)
    (svm / "00000.svm").write_bytes(b"vm")
    stream = root / "BDMV" / "STREAM"
    stream.mkdir(parents=True)
    (stream / "00001.m2ts").write_bytes(bytes(range(128)) * 3)
    (stream / "00002.m2ts").write_bytes(b"second clip")
    playlist = root / "BDMV" / "PLAYLIST"
    playlist.mkdir()
    (playlist / "00001.mpls").write_bytes(b"playlist")
    return FolderFS(root)


class BackupWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "disc"
        self.fs = make_bluray(self.root)
        self.out = Path(self.tmp.name) / "backup"

    def tearDown(self):
        self.fs.close()
        self.tmp.cleanup()

    def test_is_bluray_clip(self):
        self.assertTrue(is_bluray_clip("BDMV/STREAM/00001.m2ts"))
        self.assertTrue(is_bluray_clip("BDAV\\STREAM\\00001.M2TS"))
        self.assertFalse(is_bluray_clip("BDMV/PLAYLIST/00001.mpls"))
        self.assertFalse(is_bluray_clip("OTHER/STREAM/00001.m2ts"))

    def test_raw_backup_is_a_one_to_one_copy(self):
        writer = BackupWriter(self.fs, self.out, kind="Blu-ray", decrypt=False)
        progress = []
        copied = writer.run(lambda done, total: progress.append((done, total)))
        self.assertEqual(copied, 5)
        expected = {str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()}
        actual = {str(path.relative_to(self.out)) for path in self.out.rglob("*") if path.is_file()}
        self.assertEqual(actual, expected)
        self.assertTrue((self.out / "AACS" / "Unit_Key_RO.inf").is_file())
        self.assertFalse((self.out / "MAKEMKV").exists())
        self.assertEqual(progress[-1][0], progress[-1][1])

    def test_decrypted_bluray_backup_moves_metadata_and_decrypts_clips(self):
        decryptor = FakeDecryptor(self.fs)
        writer = BackupWriter(self.fs, self.out, kind="Blu-ray", decrypt=True,
                              decryptor=decryptor)
        copied = writer.run()
        self.assertEqual(copied, 5)
        for name in ("00001.m2ts", "00002.m2ts"):
            source = (self.root / "BDMV" / "STREAM" / name).read_bytes()
            target = (self.out / "BDMV" / "STREAM" / name).read_bytes()
            self.assertEqual(target, bytes(value ^ 0xAA for value in source))
        self.assertTrue((self.out / "MAKEMKV" / "AACS" / "Unit_Key_RO.inf").is_file())
        self.assertTrue((self.out / "MAKEMKV" / "BDSVM" / "00000.svm").is_file())
        self.assertFalse((self.out / "AACS").exists())
        self.assertFalse((self.out / "BDSVM").exists())
        self.assertEqual((self.out / "BDMV" / "PLAYLIST" / "00001.mpls").read_bytes(),
                         b"playlist")
        self.assertEqual([call[0] for call in decryptor.calls],
                         ["BDMV/STREAM/00001.m2ts", "BDMV/STREAM/00002.m2ts"])
        self.assertTrue(all(call[1] for call in decryptor.calls))

    def test_nested_aacs_and_bdsvm_are_flattened_into_makemkv(self):
        self.fs.close()
        nested = Path(self.tmp.name) / "nested"
        self.fs = make_bluray(nested, nested_metadata=True)
        writer = BackupWriter(self.fs, self.out, kind="Blu-ray", decrypt=True,
                              decryptor=FakeDecryptor(self.fs))
        writer.run()
        self.assertTrue((self.out / "MAKEMKV" / "AACS" / "Unit_Key_RO.inf").is_file())
        self.assertTrue((self.out / "MAKEMKV" / "BDSVM" / "00000.svm").is_file())
        self.assertFalse((self.out / "BDMV" / "AACS").exists())
        self.assertFalse((self.out / "BDMV" / "BDSVM").exists())

    def test_decrypted_backup_requires_a_decryptor(self):
        writer = BackupWriter(self.fs, self.out, kind="Blu-ray", decrypt=True)
        with self.assertRaises(BackupError) as caught:
            writer.run()
        self.assertIn("not prepared", str(caught.exception))

    def test_cancel_before_first_file_raises(self):
        writer = BackupWriter(self.fs, self.out, kind="Blu-ray", decrypt=False)
        with self.assertRaises(KeyboardInterrupt):
            writer.run(cancelled=lambda: True)
        self.assertFalse(self.out.exists())

    def test_progress_is_monotonic(self):
        writer = BackupWriter(self.fs, self.out, kind="Blu-ray", decrypt=False)
        progress = []
        writer.run(lambda done, total: progress.append((done, total)))
        values = [done for done, _total in progress]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], progress[-1][1])


class DvdBackupTests(unittest.TestCase):
    class FakeCss:
        def __init__(self, read_value, raw_value):
            self.read_value = read_value
            self.raw_value = raw_value
            self.raw_calls = []
            self.read_calls = []

        def listdir(self, path):
            return ["VIDEO_TS"] if not path else ["VTS_01_1.VOB", "VIDEO_TS.IFO"]

        def isdir(self, path):
            return path == "VIDEO_TS"

        def size(self, path):
            return len(self.read_value if path.endswith(".VOB") else b"ifo")

        def read(self, path, offset=0, size=None):
            self.read_calls.append(path)
            value = self.read_value if path.endswith(".VOB") else b"ifo"
            return value[offset:offset + size]

        def raw_read(self, path, offset=0, size=None):
            self.raw_calls.append(path)
            value = self.raw_value if path.endswith(".VOB") else b"ifo"
            return value[offset:offset + size]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "backup"
        self.css = self.FakeCss(b"clear-vob", b"scrambled")

    def tearDown(self):
        self.tmp.cleanup()

    def test_raw_dvd_backup_uses_undecrypted_sectors(self):
        with mock.patch.object(backuplib, "CssFS", self.FakeCss):
            writer = BackupWriter(self.css, self.out, kind="DVD", decrypt=False)
            writer.run()
        self.assertEqual((self.out / "VIDEO_TS" / "VTS_01_1.VOB").read_bytes(),
                         b"scrambled")
        self.assertIn("VIDEO_TS/VTS_01_1.VOB", self.css.raw_calls)
        self.assertEqual(self.css.read_calls, [])

    def test_decrypted_dvd_backup_reads_through_libdvdcss(self):
        with mock.patch.object(backuplib, "CssFS", self.FakeCss):
            writer = BackupWriter(self.css, self.out, kind="DVD", decrypt=True)
            writer.run()
        self.assertEqual((self.out / "VIDEO_TS" / "VTS_01_1.VOB").read_bytes(),
                         b"clear-vob")
        self.assertEqual(self.css.read_calls, ["VIDEO_TS/VTS_01_1.VOB"])

    def test_encrypted_dvd_without_css_reader_is_refused(self):
        writer = BackupWriter(self.css, self.out, kind="DVD", decrypt=True)
        with mock.patch.object(backuplib, "CssFS", type("Other", (), {})):
            with self.assertRaises(BackupError) as caught:
                writer.run()
        self.assertIn("libdvdcss", str(caught.exception))


class EngineBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.stream = io.StringIO()
        self.settings = Settings(JsonBackend(self.dir / "settings.json"))
        self.engine = Engine(RobotWriter(self.stream), self.settings,
                             builtin_default())
        self.fs = make_bluray(self.dir / "disc")
        self.disc = Disc(kind="Blu-ray", encrypted=False, title_count=1)

    def tearDown(self):
        self.engine.close_source()
        self.fs.close()
        self.tmp.cleanup()

    def install(self, encrypted=False, kind="Blu-ray"):
        self.disc.kind = kind
        self.disc.encrypted = encrypted

        def fake_open(_rest, _min_length, _emit_info):
            self.engine.fs = self.fs
            self.engine.disc = self.disc
            return OpenResult(self.disc, 0, self.fs, "disc:0")

        patcher = mock.patch.object(self.engine, "_open_disc", side_effect=fake_open)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unencrypted_backup_copies_tree(self):
        self.install()
        ok, code = self.engine.backup("disc:0", self.dir / "copy")
        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertTrue((self.dir / "copy" / "BDMV" / "STREAM" / "00001.m2ts").is_file())
        self.assertIn("MSG:9008", self.stream.getvalue())

    def test_encrypted_bluray_backup_uses_decryptor(self):
        self.install(encrypted=True)
        decryptor = FakeDecryptor(self.fs)
        with mock.patch.object(self.engine, "_bluray_decryptor",
                               return_value=decryptor):
            ok, code = self.engine.backup("disc:0", self.dir / "copy")
        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertTrue((self.dir / "copy" / "MAKEMKV" / "AACS" /
                         "Unit_Key_RO.inf").is_file())
        self.assertEqual(len(decryptor.calls), 2)

    def test_encrypted_bluray_without_libraries_is_refused(self):
        self.install(encrypted=True)
        with mock.patch.object(self.engine, "_bluray_decryptor", return_value=None):
            ok, code = self.engine.backup("disc:0", self.dir / "copy")
        self.assertFalse(ok)
        self.assertEqual(code, 11)
        self.assertIn("MSG:9100", self.stream.getvalue())

    def test_encrypted_dvd_without_css_is_refused(self):
        self.install(encrypted=True, kind="DVD")
        ok, code = self.engine.backup("disc:0", self.dir / "copy")
        self.assertFalse(ok)
        self.assertEqual(code, 11)
        self.assertIn("MSG:9100", self.stream.getvalue())


if __name__ == "__main__":
    unittest.main()
