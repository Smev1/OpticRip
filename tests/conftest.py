"""Shared pytest fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# make `tests.synth` and the project importable when running from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Fixture builders shell out to ffmpeg; expose the static ffmpeg copy to all
# subprocesses collected by shutil.which, before any test imports run.
try:  # pragma: no cover - depends on environmentImage
    import static_ffmpeg  # type: ignore
    static_ffmpeg.add_paths()
except ImportError:  # pragma: no cover
    pass

# PySide6 needs system libs that may be absent in containers; a prepared
# user-local tree at ~/.local/udebs-root carries them (see docs/DEPENDENCIES).
# LD_LIBRARY_PATH alone is unreliable at this point, so we dlopen everything
# with ctypes in dependency order before Qt is imported by any test.
import ctypes as _ctypes
import glob as _glob

_udebs = os.path.expanduser("~/.local/udebs-root/usr/lib/x86_64-linux-gnu")
if os.path.isdir(_udebs):
    _lib_paths = _glob.glob(os.path.join(_udebs, "*.so*"))
    _lib_names = {os.path.basename(p): p for p in _lib_paths}

    _PRIORITY = [
        "libz.so.1", "libbz2.so.1.0", "liblzma.so.5", "libzstd.so.1",
        "libpng16.so.16", "libexpat.so.1", "libfreetype.so.6",
        "libfontconfig.so.1", "libdbus-1.so.3", "libpcre2-16.so.0",
        "libdrm.so.2", "libX11-xcb.so.1", "libxkbcommon.so.0",
        "libxkbcommon-x11-0.so.0", "libGLX.so.0", "libEGL.so.1",
        "libGL.so.1", "libGLESv2.so.2",
    ]

    def _try_load(prefix):
        """RTLD_GLOBAL-load `prefix.so...` from the tree; True on success."""
        for fname, path in sorted(_lib_names.items()):
            if fname == prefix or fname.startswith(prefix + "."):
                try:
                    _ctypes.CDLL(path, mode=_ctypes.RTLD_GLOBAL)
                    return True
                except OSError:
                    continue
        return False

    for _ in range(6):          # multi-pass resolves load-order deps
        _remaining = False
        for _name in _PRIORITY:
            _already = False
            for _f in _lib_names:
                if _f == _name or _f.startswith(_name + "."):
                    break
            else:
                pass
        # load everything not yet loaded, longest-prefix heuristic
        for _prefix in _PRIORITY:
            _try_load(_prefix)
        # loop exits when nothing new loads; bounded by range(6)
        _seen = set()
        try:
            from PySide6 import QtWidgets as _QtW          # noqa: F401
        except ImportError:
            _remaining = True
        if not _remaining:
            break


@pytest.fixture(scope="session")
def have_ffmpeg() -> bool:
    """True when a real ffmpeg/ffprobe is importable/available."""
    try:
        import static_ffmpeg  # type: ignore
        static_ffmpeg.add_paths()
        import shutil
        return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    except ImportError:
        import shutil
        return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.fixture
def dvd_fixture(tmp_path: Path):
    """A synthetic, spec-accurate, unencrypted DVD-Video tree."""
    from tests.synth import DvdFixtureSpec, write_dvd_fixture
    video_ts = write_dvd_fixture(str(tmp_path / "disc"),
                                 DvdFixtureSpec(cell_seconds=2.0, cell_count=3,
                                                audio_streams=1))
    # return the DISC ROOT (parent of VIDEO_TS), matching the documented
    # OpticRip source convention (directory containing VIDEO_TS/BDMV)
    return str(Path(video_ts).parent)
