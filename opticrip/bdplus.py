"""Optional BD+ processing through a user-supplied libbdplus.

BD+ wraps AACS: every clip is first AACS-decrypted and then patched by the BD+
virtual machine.  libbdplus needs the disc VID and media key from libaacs, the
disc's ``BDSVM/00000.svm`` code, and its own VM configuration files (usually
installed under ``<config dir>/bdplus/vm0``).

Only the stable C stream API is used: ``bdplus_init``, ``bdplus_set_mk``,
``bdplus_mmap``/``bdplus_psr``, ``bdplus_event``, ``bdplus_m2ts``,
``bdplus_seek`` and ``bdplus_fixup``.  The pre-0.2 API (``bdplus_set_title`` +
handle-level seek/fixup) is also supported.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import filecmp
import os
import shutil
import struct
import sys
from pathlib import Path

#: libaacs units; BD+ patches exactly these units.
UNIT_SIZE = 6144

#: BD+ events from bdplus.h.
EVENT_START = 0x00000000
EVENT_TITLE = 0x00000110
RUN_CONVTAB = 0xFFFFFFFF

_PSR_READ = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int)
_PSR_WRITE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                              ctypes.c_uint32)

#: The initial player status/setting registers (libbluray's ``bd_psr_init``).
#: BD+ code reads these while building the conversion table, so matching a
#: real player's defaults avoids taking code paths meant for limited players.
_PSR_DEFAULTS = {
    0: 1,             # interactive graphics stream
    1: 0xFF,          # primary audio stream
    2: 0x0FFF0FFF,    # PG TextST / PiP stream
    3: 1,             # angle
    4: 0xFFFF,        # title number
    5: 0xFFFF,        # chapter
    10: 0xFFFF,       # selected button
    12: 0xFF,         # user style
    13: 0xFF,         # parental age
    14: 0xFFFF,       # secondary audio/video
    15: 0xAAAA,       # audio capability: LPCM/DD+/DTS-HD/DD/MLP surround
    16: 0xFFFFFF,     # audio language (undetermined)
    17: 0xFFFFFF,     # subtitle language (undetermined)
    18: 0xFFFFFF,     # menu language (undetermined)
    19: 0xFFFF,       # country
    20: 2,            # region B
    21: 0,            # output mode preference: 2D
    29: 0x03,         # secondary HD video, 25/50 Hz
    30: 0x1FFFF,      # text subtitle capability
    31: 0x030200,     # profile 2, version 2.0
    36: 0xFFFF,       # backup PSR4
    37: 0xFFFF,       # backup PSR5
    42: 0xFFFF,       # backup PSR10
    44: 0xFF,         # backup PSR12
    **{index: 0xFFFFFFFF for index in range(48, 62)},  # characteristic text
}

_DLL_DIRECTORIES: list[object] = []


class BdplusError(RuntimeError):
    """The library is missing, incompatible or could not process the disc."""


def _shared_library_names() -> list[str]:
    if sys.platform == "win32":
        return ["libbdplus.dll"]
    if sys.platform == "darwin":
        return ["libbdplus.0.dylib", "libbdplus.dylib"]
    return ["libbdplus.so.0", "libbdplus.so"]


def _load_shared(path: str | Path):
    text = str(path)
    if os.name == "nt":
        parent = Path(text).parent
        try:
            if hasattr(os, "add_dll_directory") and parent.is_dir():
                _DLL_DIRECTORIES.append(os.add_dll_directory(str(parent)))
        except OSError:
            pass
        loader = getattr(ctypes, "WinDLL", ctypes.CDLL)
        return loader(text)
    return ctypes.CDLL(text)


def _declare(library, name: str, argtypes, restype, required: bool = True):
    try:
        function = getattr(library, name)
    except AttributeError:
        if required:
            raise
        return None
    function.argtypes = argtypes
    function.restype = restype
    return function


def _bind(library) -> None:
    _declare(library, "bdplus_init",
             [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p], ctypes.c_void_p)
    _declare(library, "bdplus_free", [ctypes.c_void_p], None)
    _declare(library, "bdplus_seek", [ctypes.c_void_p, ctypes.c_uint64],
             ctypes.c_int)
    _declare(library, "bdplus_fixup",
             [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p], ctypes.c_int)
    _declare(library, "bdplus_set_mk",
             [ctypes.c_void_p, ctypes.c_void_p], None, required=False)
    _declare(library, "bdplus_mmap",
             [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p], None,
             required=False)
    _declare(library, "bdplus_psr",
             [ctypes.c_void_p, ctypes.c_void_p, _PSR_READ, _PSR_WRITE], None,
             required=False)
    _declare(library, "bdplus_start", [ctypes.c_void_p], ctypes.c_int32,
             required=False)
    _declare(library, "bdplus_event",
             [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32],
             ctypes.c_int32, required=False)

    if hasattr(library, "bdplus_m2ts"):
        _declare(library, "bdplus_m2ts",
                 [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_void_p)
        _declare(library, "bdplus_m2ts_close", [ctypes.c_void_p], None)
    else:
        # Old API: select the clip on the handle, then seek/fixup it directly.
        if not hasattr(library, "bdplus_set_title"):
            _declare(library, "bdplus_set_m2ts",
                     [ctypes.c_void_p, ctypes.c_uint32], None)
        else:
            _declare(library, "bdplus_set_title",
                     [ctypes.c_void_p, ctypes.c_uint32], None)

    if not hasattr(library, "bdplus_event") and not hasattr(library, "bdplus_start"):
        raise AttributeError("neither bdplus_event() nor bdplus_start() exists")

    _declare(library, "bdplus_get_code_date", [ctypes.c_void_p], ctypes.c_int32,
             required=False)
    _declare(library, "bdplus_get_code_gen", [ctypes.c_void_p], ctypes.c_int32,
             required=False)
    _declare(library, "bdplus_is_cached", [ctypes.c_void_p], ctypes.c_int32,
             required=False)


class BdplusLibrary:
    """A validated libbdplus shared library."""

    def __init__(self, path: str, library) -> None:
        self.path = str(path)
        self._lib = library

    @classmethod
    def load(cls, path: str | Path) -> "BdplusLibrary":
        source = str(path)
        try:
            library = _load_shared(source)
        except OSError as exc:
            raise BdplusError(f"cannot load {source}: {exc}") from exc
        try:
            _bind(library)
        except AttributeError as exc:
            raise BdplusError(
                f"{source} does not export the libbdplus API ({exc})"
            ) from exc
        return cls(source, library)

    def open(self, root: str | Path, vid: bytes | None,
             media_key: bytes | None = None, num_titles: int = 1,
             run_conversion_table: bool = True) -> "BdplusHandle":
        if vid is None or len(vid) != 16:
            raise BdplusError("libbdplus needs the 16-byte AACS volume ID (VID)")
        if run_conversion_table and not hasattr(self._lib, "bdplus_event"):
            raise BdplusError(
                "this libbdplus is too old: it cannot generate the BD+ "
                "conversion table (libbdplus 0.2 or newer is required)"
            )
        root_bytes = str(root).encode("utf-8")
        vid_buffer = (ctypes.c_ubyte * 16).from_buffer_copy(vid)
        handle = self._lib.bdplus_init(root_bytes, None, vid_buffer)
        if not handle:
            raise BdplusError(
                "bdplus_init() failed; libbdplus VM configuration files "
                "(for example bdplus/vm0) and its dependencies (for example "
                "libgcrypt) must be available"
            )
        try:
            if media_key is not None and len(media_key) == 16:
                set_mk = getattr(self._lib, "bdplus_set_mk", None)
                if set_mk is not None:
                    set_mk(handle, (ctypes.c_ubyte * 16).from_buffer_copy(media_key))
            result = BdplusHandle(self._lib, handle, root=str(root))
            if run_conversion_table:
                result.start(num_titles)
            return result
        except Exception:
            self._lib.bdplus_free(handle)
            raise


class BdplusHandle:
    """One initialized BD+ virtual machine."""

    def __init__(self, library, handle, root: str = "") -> None:
        self._lib = library
        self._handle = handle
        self.root = root
        self._closed = False
        self._started = False
        self._psr = (ctypes.c_ubyte * (128 * 4))()
        self._gpr = (ctypes.c_ubyte * (4096 * 4))()
        self._psr_read_cb = None
        self._psr_write_cb = None
        self._map_registers()

    def _map_registers(self) -> None:
        for index, value in _PSR_DEFAULTS.items():
            struct.pack_into("<I", self._psr, index * 4, int(value))
        mmap = getattr(self._lib, "bdplus_mmap", None)
        psr = getattr(self._lib, "bdplus_psr", None)
        if mmap is not None:
            mmap(self._handle, 0, self._psr)   # MMAP_ID_PSR
            mmap(self._handle, 1, self._gpr)   # MMAP_ID_GPR
        if psr is not None:
            def read(_regs, register):
                if 0 <= int(register) < 128:
                    return struct.unpack_from("<I", self._psr,
                                              int(register) * 4)[0]
                return 0xFFFFFFFF

            def write(_regs, register, value):
                if 0 <= int(register) < 128:
                    struct.pack_into("<I", self._psr, int(register) * 4,
                                     int(value) & 0xFFFFFFFF)
                return 0

            self._psr_read_cb = _PSR_READ(read)
            self._psr_write_cb = _PSR_WRITE(write)
            # The pointer only has to be non-NULL for application-layer traps;
            # the callbacks below use the handle's own register buffers.
            psr(self._handle, self._psr, self._psr_read_cb, self._psr_write_cb)

    @property
    def code_date(self) -> int:
        if self._closed:
            return 0
        function = getattr(self._lib, "bdplus_get_code_date", None)
        return int(function(self._handle)) if function is not None else 0

    @property
    def cached(self) -> bool:
        if self._closed:
            return False
        function = getattr(self._lib, "bdplus_is_cached", None)
        return bool(function(self._handle) > 0) if function is not None else False

    def start(self, num_titles: int = 1) -> int:
        """Start the VM and build the no-menu conversion table."""
        if self._closed or self._started:
            return 0
        event = getattr(self._lib, "bdplus_event", None)
        if event is not None:
            result = int(event(self._handle, EVENT_START, 0, 0))
        else:
            result = int(self._lib.bdplus_start(self._handle))
        if result < 0:
            raise BdplusError("BD+ virtual machine failed to start")
        self._started = True
        if event is not None:
            result = int(event(self._handle, RUN_CONVTAB,
                               max(1, int(num_titles)), 0))
            if result < 0:
                raise BdplusError("BD+ conversion table could not be generated")
            # A cached table makes the event return 0 without regenerating it.
            if result == 0 and not self.cached:
                raise BdplusError(
                    "BD+ did not produce a conversion table "
                    "(the disc may use unsupported BD+ code)"
                )
        return result

    def event(self, event: int, param1: int = 0, param2: int = 0) -> int:
        if self._closed:
            return -1
        function = getattr(self._lib, "bdplus_event", None)
        if function is None:
            return 0
        return int(function(self._handle, int(event),
                            int(param1) & 0xFFFFFFFF,
                            int(param2) & 0xFFFFFFFF))

    def clip(self, clip_id: int) -> "BdplusStream | None":
        """Open one ``xxxxx.m2ts`` clip, seeked to its start."""
        if self._closed:
            return None
        if hasattr(self._lib, "bdplus_m2ts"):
            stream = self._lib.bdplus_m2ts(self._handle, int(clip_id))
            if not stream:
                return None
            if int(self._lib.bdplus_seek(stream, 0)) < 0:
                self._lib.bdplus_m2ts_close(stream)
                return None
            return BdplusStream(self._lib, stream, owner=self, modern=True)

        selector = getattr(self._lib, "bdplus_set_title", None) \
            or getattr(self._lib, "bdplus_set_m2ts")
        selector(self._handle, int(clip_id))
        if int(self._lib.bdplus_seek(self._handle, 0)) < 0:
            return None
        return BdplusStream(self._lib, self._handle, owner=self, modern=False)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._lib.bdplus_free(self._handle)
        self._psr_read_cb = None
        self._psr_write_cb = None


class BdplusStream:
    """A BD+ patched clip stream."""

    def __init__(self, library, handle, owner: BdplusHandle,
                 modern: bool = True) -> None:
        self._lib = library
        self._handle = handle
        self._owner = owner
        self._modern = modern
        self._closed = False

    def seek(self, offset: int) -> int:
        if self._closed:
            return -1
        return int(self._lib.bdplus_seek(self._handle, int(offset)))

    def fixup(self, unit: bytearray) -> int:
        if self._closed:
            raise OSError("BD+ stream is closed")
        if len(unit) != UNIT_SIZE:
            raise ValueError(
                f"BD+ patches {UNIT_SIZE}-byte units, got {len(unit)}"
            )
        buffer = (ctypes.c_ubyte * UNIT_SIZE).from_buffer(unit)
        return int(self._lib.bdplus_fixup(self._handle, UNIT_SIZE, buffer))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._modern:
            self._lib.bdplus_m2ts_close(self._handle)


def library_candidates(data_dir: str | Path | None = None) -> list[Path]:
    roots: list[Path] = []
    if data_dir is not None:
        roots.append(Path(data_dir) / "libbdplus")
    here = Path(__file__).resolve().parent
    roots.extend([here, here.parent])
    if sys.platform == "win32":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(variable)
            if base:
                roots.append(Path(base) / "VideoLAN" / "VLC")
    found: list[Path] = []
    names = _shared_library_names()
    for root in roots:
        for name in names:
            candidate = root / name
            try:
                if candidate.is_file() and candidate not in found:
                    found.append(candidate)
            except OSError:
                continue
    return found


def resolve(explicit: str | Path | None,
            data_dir: str | Path | None = None) -> tuple[BdplusLibrary, Path | str]:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise BdplusError(f"libbdplus not found at {path}")
        return BdplusLibrary.load(path), path

    attempts: list[str] = []
    for candidate in library_candidates(data_dir):
        try:
            return BdplusLibrary.load(candidate), candidate
        except BdplusError as exc:
            attempts.append(str(exc))

    soname = ctypes.util.find_library("bdplus")
    if soname:
        try:
            return BdplusLibrary.load(soname), soname
        except BdplusError as exc:
            attempts.append(str(exc))

    detail = f" ({attempts[-1]})" if attempts else ""
    raise BdplusError(
        "No libbdplus library was found. Use File > Load libbdplus library... "
        f"or set OPTICRIP_LIBBDPLUS to your own copy{detail}"
    )


def stage_library(source: str | Path, data_dir: str | Path,
                  loader=None) -> Path:
    """Validate and copy a user-supplied libbdplus into the data directory."""

    source_path = Path(source)
    if not source_path.is_file():
        raise BdplusError(f"libbdplus not found at {source_path}")
    validate = loader or BdplusLibrary.load
    try:
        validate(source_path)
    except BdplusError:
        raise
    except OSError as exc:
        raise BdplusError(f"cannot load {source_path}: {exc}") from exc

    destination = Path(data_dir) / "libbdplus"
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BdplusError(f"cannot create {destination}: {exc}") from exc
    target = destination / source_path.name
    try:
        if source_path.resolve() != target.resolve():
            temporary = target.with_name(target.name + ".part")
            shutil.copy2(source_path, temporary)
            os.replace(temporary, target)
        if not filecmp.cmp(source_path, target, shallow=False):
            raise BdplusError(f"the copied library at {target} is incomplete")
    except BdplusError:
        raise
    except OSError as exc:
        raise BdplusError(f"cannot install libbdplus at {target}: {exc}") from exc
    return target
