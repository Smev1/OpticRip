"""Windows backend tests: run on Linux, exercising the win-code paths
through stubs so CI covers them without a Windows box."""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest

from opticrip.core.models import DriveKind, DriveState, MediaType


@pytest.fixture
def winmod():
    """windows_probe module with a stubbed kernel32 so Linux can exercise
    the drive-letter logic paths."""
    import opticrip.disc.windows_probe as wp
    return wp


class TestDriveDiscovery:
    def test_letters_decode_from_bitmap(self, winmod, monkeypatch):
        monkeypatch.setattr(winmod, "IS_WINDOWS", True)
        # bits for C(0), D(1), F(3): 0b00011011 = 27
        # bits 2,3,5 == C:, D:, F:
        monkeypatch.setattr(winmod, "kernel32",
                            _FakeKernel(bits=(1 << 2) | (1 << 3) | (1 << 5)),
                            raising=False)
        assert winmod.list_cdrom_letters() == ["C:", "D:", "F:"]

    def test_drive_type_filter(self, winmod, monkeypatch):
        monkeypatch.setattr(winmod, "IS_WINDOWS", True)
        # bitmap bits 2,3,4 => C:, D:, E: exist; of those D and E are
        # CDROM-typed; C is fixed-disk (2)
        k = _FakeKernel(bits=(1 << 2) | (1 << 3) | (1 << 4),
                        types={"C:\\": 2, "D:\\": 5, "E:\\": 5})
        monkeypatch.setattr(winmod, "kernel32", k, raising=False)
        assert winmod.list_cdrom_letters() == ["D:", "E:"]

    def test_nonwindows_is_safe_noop(self, winmod):
        if sys.platform.startswith("win"):
            pytest.skip("real windows box")
        # on Linux the stub path still returns [] because _IS_WINDOWS False
        assert winmod.list_cdrom_letters() == []

    def test_provider_builds_drive(self, winmod, monkeypatch):
        from opticrip.drives.windows import WindowsDriveProvider, \
            _WindowsMediaProbeAdapter
        from opticrip.disc.identify import MediaProbeResult
        monkeypatch.setattr(winmod, "list_cdrom_letters",
                            lambda: ["D:", "E:"])
        calls = []

        class P(_WindowsMediaProbeAdapter):
            def probe(self, path):
                calls.append(path)
                if path == "D:":
                    return MediaProbeResult(has_media=True,
                                            media_type=MediaType.UNKNOWN,
                                            volume_label="MY DISC")
                return MediaProbeResult()   # empty drive

        provider = WindowsDriveProvider(probe=P())
        drives = provider.list_drives()
        assert [d.path for d in drives] == ["D:", "E:"]
        assert drives[0].volume_label == "MY DISC"
        assert drives[1].state is DriveState.NO_DISC

    def test_probe_folder_uses_mount_path(self, winmod, tmp_path,
                                          monkeypatch):
        # dir branch: has_media, UNKNOWN, label read attempt is stubbed
        monkeypatch.setattr(winmod, "_volume_label", lambda p: "LAB")
        folder = tmp_path / "disc"
        folder.mkdir()
        result = winmod.probe_root(str(folder))
        assert result.has_media and result.media_type is MediaType.UNKNOWN

    def test_probe_image_pvd(self, winmod, tmp_path):
        # build a file with an ISO PVD at sector 16
        data = bytearray(22 * 2048)   # room for the 6-sector VRS read
        data[16 * 2048] = 1
        data[16 * 2048 + 1:16 * 2048 + 6] = b"CD001"
        data[16 * 2048 + 40:16 * 2048 + 72] = b"TESTVOL".ljust(32)
        f = tmp_path / "img.iso"
        f.write_bytes(bytes(data))
        result = winmod.probe_root(str(f))
        assert result.filesystem == "ISO9660"
        assert result.volume_label.startswith("TESTVOL")
        assert result.has_media

    def test_probe_empty_image_is_empty(self, winmod, tmp_path):
        f = tmp_path / "empty.iso"
        f.write_bytes(b"")
        result = winmod.probe_root(str(f))
        assert result.media_type is MediaType.EMPTY


class _FakeKernel:
    def __init__(self, bits: int, types: dict[str, int] | None = None):
        self._bits = bits
        self._types = types or {}

    def GetLogicalDrives(self):
        return self._bits

    def GetDriveTypeW(self, root):
        return self._types.get(root, 5)

    def GetVolumeInformationW(self, root, name, n, *a):
        return 0
