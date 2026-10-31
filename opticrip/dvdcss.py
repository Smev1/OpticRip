"""Optional CSS decryption through a user-supplied libdvdcss.

OpticRip does not ship or link libdvdcss.  The user can point the application
at an existing copy, either by dropping it into ``<data dir>/libdvdcss`` or by
using *File -> Load libdvdcss library...* in the GUI.  Nothing in this module
runs until a scrambled DVD is actually opened, so the application keeps
working on machines without the library.

The wrapper deliberately exposes only the small, stable part of the C API:
``dvdcss_open``, ``dvdcss_close``, ``dvdcss_seek``, ``dvdcss_read``,
``dvdcss_title`` and ``dvdcss_error``.  ``dvdcss_read`` is called with
``DVDCSS_READ_DECRYPT`` only for VOB payload; ISO9660 metadata is read without
the flag because libdvdcss decrypts every sector of a CSS disc when asked, and
decrypting the file system would corrupt it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import filecmp
import os
import re
import shutil
import sys
from pathlib import Path

BLOCK_SIZE = 2048

DVDCSS_NOFLAGS = 0x0
DVDCSS_READ_DECRYPT = 0x1
DVDCSS_SEEK_MPEG = 0x1
DVDCSS_SEEK_KEY = 0x2

#: Keep the handles returned by os.add_dll_directory alive for the process
#: lifetime.  Releasing one makes dependent DLLs unloadable again.
_DLL_DIRECTORIES: list[object] = []

_DRIVE = re.compile(r"^(?:\\\\\.\\)?([A-Za-z]):")


class DvdcssError(RuntimeError):
    """The library is missing, incompatible or refused the device."""


def _shared_library_names() -> list[str]:
    if sys.platform == "win32":
        return ["libdvdcss-2.dll", "libdvdcss.dll"]
    if sys.platform == "darwin":
        return ["libdvdcss.2.dylib", "libdvdcss.dylib"]
    return ["libdvdcss.so.2", "libdvdcss.so"]


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


class DvdcssLibrary:
    """A validated libdvdcss shared library."""

    def __init__(self, path: str, library) -> None:
        self.path = str(path)
        self._lib = library

    @classmethod
    def load(cls, path: str | Path) -> "DvdcssLibrary":
        source = str(path)
        try:
            library = _load_shared(source)
        except OSError as exc:
            raise DvdcssError(f"cannot load {source}: {exc}") from exc
        try:
            _bind(library)
        except AttributeError as exc:
            raise DvdcssError(
                f"{source} does not export the libdvdcss API ({exc})"
            ) from exc
        return cls(source, library)

    def open(self, target: str) -> "DvdcssHandle":
        device = _normalise_target(target)
        handle = self._lib.dvdcss_open(device.encode("utf-8"))
        if not handle:
            raise DvdcssError(f"libdvdcss could not open {device}")
        return DvdcssHandle(self._lib, handle, device)


class DvdcssHandle:
    """One open disc/device."""

    def __init__(self, library, handle, target: str) -> None:
        self._lib = library
        self._handle = handle
        self.target = target
        self._closed = False

    @property
    def error(self) -> str:
        if self._closed:
            return "device is closed"
        raw = self._lib.dvdcss_error(self._handle)
        if not raw:
            return "unknown libdvdcss error"
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(raw)

    def seek(self, block: int, mpeg: bool = False) -> int:
        if self._closed:
            raise OSError("libdvdcss device is closed")
        # SEEK_KEY also works when libdvdcss is configured for the slower
        # "title" cracking method, where SEEK_MPEG alone would not reload the
        # per-title key after the first read.
        flags = (DVDCSS_SEEK_MPEG | DVDCSS_SEEK_KEY) if mpeg else DVDCSS_NOFLAGS
        result = self._lib.dvdcss_seek(self._handle, int(block), flags)
        if result < 0:
            raise OSError(
                f"libdvdcss seek to block {block} failed: {self.error}"
            )
        return int(result)

    def read(self, blocks: int, decrypt: bool = False) -> bytes:
        if self._closed:
            raise OSError("libdvdcss device is closed")
        remaining = int(blocks)
        if remaining <= 0:
            return b""
        flags = DVDCSS_READ_DECRYPT if decrypt else DVDCSS_NOFLAGS
        out = bytearray()
        while remaining > 0:
            take = min(remaining, 512)
            buffer = ctypes.create_string_buffer(take * BLOCK_SIZE)
            got = self._lib.dvdcss_read(self._handle, buffer, take, flags)
            if got < 0:
                raise OSError(
                    f"libdvdcss read of {take} block(s) failed: {self.error}"
                )
            if got == 0:
                break
            out.extend(buffer.raw[: got * BLOCK_SIZE])
            remaining -= got
            if got < take:
                break
        return bytes(out)

    def is_scrambled(self) -> bool:
        if self._closed:
            return False
        return bool(self._lib.dvdcss_is_scrambled(self._handle))

    def close(self) -> None:
        if not self._closed:
            self._lib.dvdcss_close(self._handle)
            self._closed = True


def _bind(library) -> None:
    """Declare the C signatures before any call."""

    library.dvdcss_open.argtypes = [ctypes.c_char_p]
    library.dvdcss_open.restype = ctypes.c_void_p
    library.dvdcss_close.argtypes = [ctypes.c_void_p]
    library.dvdcss_close.restype = ctypes.c_int
    library.dvdcss_seek.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    library.dvdcss_seek.restype = ctypes.c_int
    library.dvdcss_read.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
    ]
    library.dvdcss_read.restype = ctypes.c_int
    library.dvdcss_title.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.dvdcss_title.restype = ctypes.c_int
    library.dvdcss_error.argtypes = [ctypes.c_void_p]
    library.dvdcss_error.restype = ctypes.c_char_p
    library.dvdcss_is_scrambled.argtypes = [ctypes.c_void_p]
    library.dvdcss_is_scrambled.restype = ctypes.c_int


def _normalise_target(target: str) -> str:
    """libdvdcss on Windows takes ``D:``, not a path or ``\\\\.\\D:``.

    On POSIX systems the path is passed through unchanged so that libdvdcss
    can open either a block device or a disc image.
    """

    if sys.platform != "win32":
        return target
    match = _DRIVE.match(target)
    if match is None:
        raise DvdcssError(
            "libdvdcss on Windows can only open an optical drive letter "
            f"(got {target!r})"
        )
    return match.group(1).upper() + ":"


class DvdcssFile:
    """Seekable file-like view over an open libdvdcss device.

    ``ISOImage`` reads whole sectors.  ``set_mpeg`` changes whether subsequent
    reads ask libdvdcss to decrypt; it is enabled only for VOB payload.
    """

    def __init__(self, handle: DvdcssHandle, name: str = "") -> None:
        self.handle = handle
        self.name = name
        self._pos = 0
        self._mpeg = False
        self.closed = False

    def set_mpeg(self, mpeg: bool) -> None:
        mpeg = bool(mpeg)
        if mpeg == self._mpeg:
            return
        self._mpeg = mpeg
        if self._pos:
            self.seek(self._pos)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if self.closed:
            raise OSError("file is closed")
        if whence == os.SEEK_CUR:
            offset = self._pos + offset
        elif whence == os.SEEK_END:
            raise OSError("cannot seek from the end of an optical device")
        if offset < 0:
            raise OSError("negative seek position")
        block, intra = divmod(int(offset), BLOCK_SIZE)
        self.handle.seek(block, self._mpeg)
        self._pos = block * BLOCK_SIZE + intra
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise OSError("file is closed")
        if size is None or size < 0:
            raise OSError("unbounded reads are not supported on a disc device")
        if size == 0:
            return b""
        block, intra = divmod(self._pos, BLOCK_SIZE)
        blocks = (intra + size + BLOCK_SIZE - 1) // BLOCK_SIZE
        # Seeking again with MPEG set makes libdvdcss check the title key at
        # the start of every read, which is what makes multi-title discs work.
        self.handle.seek(block, self._mpeg)
        data = self.handle.read(blocks, self._mpeg)
        chunk = data[intra:intra + size]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        if not self.closed:
            self.handle.close()
            self.closed = True


def library_candidates(data_dir: str | Path | None = None) -> list[Path]:
    """Local copies that are worth trying, in priority order."""

    roots: list[Path] = []
    if data_dir is not None:
        roots.append(Path(data_dir) / "libdvdcss")
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
            data_dir: str | Path | None = None) -> tuple[DvdcssLibrary, Path | str]:
    """Load the configured copy, or discover one on the system.

    Raises :class:`DvdcssError` with a user-facing reason when nothing usable
    is found.  The returned path is what should be shown in the log.
    """

    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise DvdcssError(f"libdvdcss not found at {path}")
        return DvdcssLibrary.load(path), path

    attempts: list[str] = []
    for candidate in library_candidates(data_dir):
        try:
            return DvdcssLibrary.load(candidate), candidate
        except DvdcssError as exc:
            attempts.append(str(exc))

    soname = ctypes.util.find_library("dvdcss")
    if soname:
        try:
            return DvdcssLibrary.load(soname), soname
        except DvdcssError as exc:
            attempts.append(str(exc))

    detail = f" ({attempts[-1]})" if attempts else ""
    raise DvdcssError(
        "No libdvdcss library was found. Use File > Load libdvdcss library... "
        f"or set OPTICRIP_DVDCSS to your own copy{detail}"
    )


def stage_library(source: str | Path, data_dir: str | Path,
                  loader=None) -> Path:
    """Validate and copy a user-supplied library into the data directory.

    The copy is what gets recorded in ``app_DvdcssPath`` so moving or deleting
    the original download does not break future rips.  ``loader`` is injectable
    for tests.
    """

    source_path = Path(source)
    if not source_path.is_file():
        raise DvdcssError(f"libdvdcss not found at {source_path}")
    validate = loader or DvdcssLibrary.load
    try:
        validate(source_path)
    except DvdcssError:
        raise
    except OSError as exc:
        raise DvdcssError(f"cannot load {source_path}: {exc}") from exc

    destination = Path(data_dir) / "libdvdcss"
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DvdcssError(f"cannot create {destination}: {exc}") from exc
    target = destination / source_path.name
    try:
        if source_path.resolve() != target.resolve():
            temporary = target.with_name(target.name + ".part")
            shutil.copy2(source_path, temporary)
            os.replace(temporary, target)
        # Do not load the staged copy here: Windows keeps a loaded DLL locked
        # for the process lifetime, which would prevent replacing it later.
        if not filecmp.cmp(source_path, target, shallow=False):
            raise DvdcssError(f"the copied library at {target} is incomplete")
    except DvdcssError:
        raise
    except OSError as exc:
        raise DvdcssError(f"cannot install libdvdcss at {target}: {exc}") from exc
    return target
