"""libdvdcss discovery and enablement (Phase 10).

OpticRip ships no decryption code and does not bundle libdvdcss.  If the
*user* has installed libdvdcss on their system (a step many legal systems
explicitly allow for accessing their own discs), ffmpeg's dvdvideo demuxer
can already decrypt CSS through its libdvdread dependency - but only when
libdvdread can find the library at runtime:

* Linux/BSD:  ``libdvdread`` built with runtime loading does
  ``dlopen("libdvdcss.so.2")`` honouring ``LD_LIBRARY_PATH``;
* Windows:    it ``LoadLibrary``s ``libdvdcss-2.dll`` from the DLL search
  path (application dir / ``PATH``).

This module:

#. searches the usual locations + OpticRip settings for the library;
#. provides :func:`dvdcss_env` which returns the process-environment
   overrides to prepend ahead of every ffmpeg/ffprobe subprocess so the
   library is found by libdvdread;
#. offers an optional smoke test (:func:`probe_dvdcss_visible`) that starts
   ffprobe on a source and inspects stderr for libdvdcss confirmation,
   used by the diagnostics panel.

Legal note: libdvdcss itself is an external program like ffmpeg.  Its
key-derivation method is the subject of a French Conseil d'État decision
(16 July 2008) cited by VideoLAN as authorising publication.  OpticRip
neither bundles nor downloads it - see docs/PROTECTED_DISCS.md.
"""
from __future__ import annotations

import os
import ctypes.util
import subprocess
import sys

#: possible library file names per platform
_CANDIDATES = {
    "win32": ("libdvdcss-2.dll", "libdvdcss.dll"),
    "darwin": ("libdvdcss.2.dylib", "libdvdcss.dylib"),
}.get(sys.platform, ("libdvdcss.so.2", "libdvdcss.so"))

#: env var name the dynamic loader honours, per platform
_LOADER_ENV = "PATH" if sys.platform == "win32" else "LD_LIBRARY_PATH"


def find_dvdcss(extra_paths: list[str] | None = None) -> str | None:
    """Locate a libdvdcss library file.  Returns its full path or None."""
    dirs: list[str] = []

    # 1. settings-driven extra search paths (usually the same list as tools)
    if extra_paths:
        dirs.extend(extra_paths)

    # 2. directory of the configured ffmpeg binary (users often install
    #    libdvdcss next to their ffmpeg build)
    try:
        from ..rip.tools import find_executable
        import shutil
        ff = find_executable("ffmpeg") or shutil.which("ffmpeg")
        if ff:
            dirs.append(os.path.dirname(os.path.abspath(ff)))
    except Exception:
        pass

    # 3. frozen app's bundled _bin
    def _frozen_base():
        import sys
        frozen = getattr(sys, "frozen", False)
        if not frozen:
            return None
        return getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    fbase = _frozen_base()
    for sub in ("_bin", "_internal/_bin"):
        if fbase:
            dirs.append(os.path.join(fbase, sub))

    # 4. the OS loader path itself
    if sys.platform == "win32":
        dirs.extend(os.environ.get("PATH", "").split(os.pathsep))
        dirs.append(os.getcwd())
    else:
        dirs.extend(os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep))
        dirs.append("/usr/lib/x86_64-linux-gnu")
        dirs.append("/usr/lib")
        dirs.append("/usr/local/lib")

    names = [os.path.join(d, n) for d in dirs
             for n in _CANDIDATES if os.path.isdir(d)]
    for path in names:
        if os.path.isfile(path):
            return path

    # 5. ctypes.util may know an ABI name even when files elude us
    try:
        found = ctypes.util.find_library("dvdcss")
        if found and os.path.isfile(found):
            return found
    except Exception:
        pass
    return None


def dvdcss_env(dvdcss_path: str) -> dict[str, str]:
    """Environment overrides so libdvdread locates the library."""
    lib_dir = os.path.dirname(os.path.abspath(dvdcss_path))
    if sys.platform == "win32":
        current = os.environ.get("PATH", "")
        if lib_dir not in current:
            return {"PATH": lib_dir + os.pathsep + current}
        return {}
    current = os.environ.get("LD_LIBRARY_PATH", "")
    if lib_dir not in current:
        return {"LD_LIBRARY_PATH": lib_dir + (os.pathsep + current if current else "")}
    return {}
