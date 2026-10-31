"""Optional AACS decryption through a user-supplied libaacs.

OpticRip neither ships nor links libaacs.  The user can point the application
at an existing copy, either by dropping it into ``<data dir>/libaacs`` or by
using *File -> Load libaacs library...* in the GUI.  Keys come from the usual
``KEYDB.cfg`` (libaacs' own search path, or an explicit path chosen in the GUI
or set through ``OPTICRIP_AACS_KEYDB``).

Only the stable C API is used: ``aacs_init``/``aacs_open_device`` when the
library is new enough, with ``aacs_open2``/``aacs_open`` as fallbacks for old
builds.  ``aacs_decrypt_unit`` decrypts exactly one 6144-byte unit in place,
so callers must hand it aligned units (see :data:`UNIT_SIZE`).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import filecmp
import os
import shutil
import sys
from pathlib import Path

#: An AACS unit starts with a 4-byte TP header and contains 32 192-byte TS
#: packets.  libaacs decrypts one unit per call.
UNIT_SIZE = 6144

#: Error codes from libaacs' aacs.h, used only when the library has no
#: ``aacs_error_str`` symbol.
_ERROR_TEXT = {
    0: "success",
    -1: "corrupt disc",
    -2: "missing AACS configuration file",
    -3: "no matching processing key",
    -4: "no valid certificate",
    -5: "certificate has been revoked",
    -6: "MMC open failed (no MMC drive?)",
    -7: "MMC failure",
    -8: "no matching device key",
    -9: "unknown failure",
    -10: "unsupported AACS version",
}

#: MKB type values from the AACS specification (also used by libaacs' mkb.h).
#: The 2.x/Category C types are the ones used by Ultra HD Blu-ray.
MKB_TYPE_3 = 0x00031003
MKB_TYPE_4 = 0x00041003
MKB_TYPE_10_CLASS_II = 0x000A1003
MKB_20_CATEGORY_C = 0x48141003
MKB_21_CATEGORY_C = 0x48151003

_AACS2_TYPES = {MKB_20_CATEGORY_C: "2.0", MKB_21_CATEGORY_C: "2.1"}

#: Keep os.add_dll_directory handles alive for the process lifetime; releasing
#: one makes DLLs that were loaded from that directory unloadable again.
_DLL_DIRECTORIES: list[object] = []


class AacsError(RuntimeError):
    """The library is missing, incompatible or refused the disc."""


def parse_mkb(blob: bytes) -> tuple[int, int]:
    """Return ``(MKB type, MKB version)`` from an ``MKBROM.AACS`` file.

    An MKB is a chain of records.  Each record starts with a one-byte type
    and a three-byte big-endian length (which includes the header).  Record
    ``0x10`` holds the MKB type and version as two big-endian 32-bit values:
    the same layout libaacs uses in ``mkb.c``.  ``(0, 0)`` means the file
    was truncated or did not have that record.
    """
    position = 0
    size = len(blob)
    while position + 4 <= size:
        kind = blob[position]
        length = int.from_bytes(blob[position + 1:position + 4], "big")
        if length < 4 or position + length > size:
            break
        if kind == 0x10 and length >= 12:
            mkb_type = int.from_bytes(blob[position + 4:position + 8], "big")
            version = int.from_bytes(blob[position + 8:position + 12], "big")
            return mkb_type, version
        position += length
    return 0, 0


def mkb_generation(mkb_type: int) -> str:
    """``"2.0"``/``"2.1"`` for an AACS 2.x MKB, otherwise an empty string."""
    return _AACS2_TYPES.get(int(mkb_type), "")

def _shared_library_names() -> list[str]:
    if sys.platform == "win32":
        return ["libaacs.dll"]
    if sys.platform == "darwin":
        return ["libaacs.0.dylib", "libaacs.dylib"]
    return ["libaacs.so.0", "libaacs.so"]


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
    """Validate the exported API and declare C signatures."""

    _declare(library, "aacs_decrypt_unit",
             [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int)
    _declare(library, "aacs_close", [ctypes.c_void_p], None)

    have_modern = hasattr(library, "aacs_init") and hasattr(library, "aacs_open_device")
    if have_modern:
        _declare(library, "aacs_init", [], ctypes.c_void_p)
        _declare(library, "aacs_open_device",
                 [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p], ctypes.c_int)
    elif hasattr(library, "aacs_open2"):
        _declare(library, "aacs_open2", [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
        ], ctypes.c_void_p)
    elif hasattr(library, "aacs_open"):
        _declare(library, "aacs_open",
                 [ctypes.c_char_p, ctypes.c_char_p], ctypes.c_void_p)
    else:
        raise AttributeError(
            "no aacs_open_device(), aacs_open2() or aacs_open() entry point"
        )

    _declare(library, "aacs_select_title",
             [ctypes.c_void_p, ctypes.c_uint32], None, required=False)
    _declare(library, "aacs_get_vid", [ctypes.c_void_p], ctypes.c_void_p,
             required=False)
    _declare(library, "aacs_get_mk", [ctypes.c_void_p], ctypes.c_void_p,
             required=False)
    _declare(library, "aacs_get_mkb_version", [ctypes.c_void_p], ctypes.c_int,
             required=False)
    _declare(library, "aacs_error_str", [ctypes.c_int], ctypes.c_char_p,
             required=False)


class AacsLibrary:
    """A validated libaacs shared library."""

    def __init__(self, path: str, library) -> None:
        self.path = str(path)
        self._lib = library

    @classmethod
    def load(cls, path: str | Path) -> "AacsLibrary":
        source = str(path)
        try:
            library = _load_shared(source)
        except OSError as exc:
            raise AacsError(f"cannot load {source}: {exc}") from exc
        try:
            _bind(library)
        except AttributeError as exc:
            raise AacsError(
                f"{source} does not export the libaacs API ({exc})"
            ) from exc
        return cls(source, library)

    def open(self, target: str, keyfile: str | Path | None = None) -> "AacsHandle":
        root = str(target)
        key = str(keyfile) if keyfile else None
        key_bytes = key.encode("utf-8") if key else None

        if hasattr(self._lib, "aacs_open_device"):
            handle = self._lib.aacs_init()
            if not handle:
                raise AacsError(
                    "aacs_init() failed; libaacs dependencies "
                    "(for example libgcrypt) must be loadable"
                )
            error = int(self._lib.aacs_open_device(
                handle, root.encode("utf-8"), key_bytes))
            if error:
                self._lib.aacs_close(handle)
                raise AacsError(
                    f"libaacs could not open {root}: {self._error_text(error)}"
                )
            return AacsHandle(self._lib, handle, root, error)

        if hasattr(self._lib, "aacs_open2"):
            error = ctypes.c_int(0)
            handle = self._lib.aacs_open2(
                root.encode("utf-8"), key_bytes, ctypes.byref(error))
            if not handle or error.value:
                if handle:
                    self._lib.aacs_close(handle)
                raise AacsError(
                    f"libaacs could not open {root}: "
                    f"{self._error_text(int(error.value))}"
                )
            return AacsHandle(self._lib, handle, root, int(error.value))

        handle = self._lib.aacs_open(root.encode("utf-8"), key_bytes)
        if not handle:
            raise AacsError(f"libaacs could not open {root}")
        return AacsHandle(self._lib, handle, root)

    def _error_text(self, code: int) -> str:
        function = getattr(self._lib, "aacs_error_str", None)
        if function is not None:
            try:
                raw = function(int(code))
            except (ValueError, ctypes.ArgumentError):
                raw = None
            if raw:
                return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        return _ERROR_TEXT.get(int(code), f"AACS error {code}")


class AacsHandle:
    """One open AACS disc."""

    def __init__(self, library, handle, target: str, error_code: int = 0) -> None:
        self._lib = library
        self._handle = handle
        self.target = target
        self.error_code = error_code
        self._closed = False

    @property
    def error(self) -> str:
        if self.error_code:
            text = getattr(self._lib, "aacs_error_str", None)
            if text is not None:
                raw = text(int(self.error_code))
                if raw:
                    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            return _ERROR_TEXT.get(int(self.error_code),
                                   f"AACS error {self.error_code}")
        return ""

    def _property(self, name: str, size: int = 16) -> bytes | None:
        if self._closed:
            return None
        function = getattr(self._lib, name, None)
        if function is None:
            return None
        pointer = function(self._handle)
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer, size)
        except (ValueError, OSError):
            return None

    @property
    def volume_id(self) -> bytes | None:
        """The disc VID (16 bytes), needed by libbdplus."""
        return self._property("aacs_get_vid")

    @property
    def media_key(self) -> bytes | None:
        """The media key (16 bytes), needed by libbdplus."""
        return self._property("aacs_get_mk")

    @property
    def mkb_version(self) -> int:
        if self._closed:
            return 0
        function = getattr(self._lib, "aacs_get_mkb_version", None)
        return int(function(self._handle)) if function is not None else 0

    def select_title(self, title: int) -> None:
        """Select a CPS unit by on-disc title number (0xffff resets it)."""
        if self._closed:
            return
        function = getattr(self._lib, "aacs_select_title", None)
        if function is not None:
            function(self._handle, int(title) & 0xFFFFFFFF)

    def reset(self) -> None:
        """Forget the selected CPS unit so the next unit is auto-detected."""
        self.select_title(0xFFFF)

    def decrypt_unit(self, unit: bytearray) -> None:
        """Decrypt exactly one 6144-byte unit in place.

        libaacs returns 1 on success and 0/-1 on failure; -1 is truthy, so the
        result must be compared rather than used as a boolean.
        """
        if self._closed:
            raise OSError("libaacs device is closed")
        if len(unit) != UNIT_SIZE:
            raise ValueError(
                f"libaacs decrypts {UNIT_SIZE}-byte units, got {len(unit)}"
            )
        buffer = (ctypes.c_ubyte * UNIT_SIZE).from_buffer(unit)
        result = int(self._lib.aacs_decrypt_unit(self._handle, buffer))
        if result != 1:
            detail = self.error or "the unit key is unknown"
            raise AacsError(f"AACS decryption failed: {detail}")

    def close(self) -> None:
        if not self._closed:
            self._lib.aacs_close(self._handle)
            self._closed = True


def library_candidates(data_dir: str | Path | None = None) -> list[Path]:
    """Local copies that are worth trying, in priority order."""

    roots: list[Path] = []
    if data_dir is not None:
        roots.append(Path(data_dir) / "libaacs")
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
            data_dir: str | Path | None = None) -> tuple[AacsLibrary, Path | str]:
    """Load the configured copy, or discover one on the system."""

    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise AacsError(f"libaacs not found at {path}")
        return AacsLibrary.load(path), path

    attempts: list[str] = []
    for candidate in library_candidates(data_dir):
        try:
            return AacsLibrary.load(candidate), candidate
        except AacsError as exc:
            attempts.append(str(exc))

    soname = ctypes.util.find_library("aacs")
    if soname:
        try:
            return AacsLibrary.load(soname), soname
        except AacsError as exc:
            attempts.append(str(exc))

    detail = f" ({attempts[-1]})" if attempts else ""
    raise AacsError(
        "No libaacs library was found. Use File > Load libaacs library... "
        f"or set OPTICRIP_LIBAACS to your own copy{detail}"
    )


def stage_library(source: str | Path, data_dir: str | Path,
                  loader=None) -> Path:
    """Validate and copy a user-supplied libaacs into the data directory."""

    source_path = Path(source)
    if not source_path.is_file():
        raise AacsError(f"libaacs not found at {source_path}")
    validate = loader or AacsLibrary.load
    try:
        validate(source_path)
    except AacsError:
        raise
    except OSError as exc:
        raise AacsError(f"cannot load {source_path}: {exc}") from exc

    destination = Path(data_dir) / "libaacs"
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AacsError(f"cannot create {destination}: {exc}") from exc
    target = destination / source_path.name
    try:
        if source_path.resolve() != target.resolve():
            temporary = target.with_name(target.name + ".part")
            shutil.copy2(source_path, temporary)
            os.replace(temporary, target)
        # Do not load the staged copy here: Windows keeps a loaded DLL locked
        # for the process lifetime, which would prevent replacing it later.
        if not filecmp.cmp(source_path, target, shallow=False):
            raise AacsError(f"the copied library at {target} is incomplete")
    except AacsError:
        raise
    except OSError as exc:
        raise AacsError(f"cannot install libaacs at {target}: {exc}") from exc
    return target

