"""Uniform access to a disc tree, whether it lives in a folder or an ISO."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable, Protocol

from .dvdcss import DvdcssFile, DvdcssLibrary
from .iso9660 import ISOImage, IsoError, UnsupportedImage
from .udf import UDFImage


class DiscFS(Protocol):
    root: str

    def exists(self, path: str) -> bool: ...
    def isdir(self, path: str) -> bool: ...
    def size(self, path: str) -> int: ...
    def read(self, path: str, offset: int = 0, size: int | None = None) -> bytes: ...
    def listdir(self, path: str) -> list[str]: ...
    def local(self, path: str) -> str | None: ...
    def close(self) -> None: ...


class FolderFS:
    def __init__(self, root: str | Path) -> None:
        self.root = str(root)
        self._root = Path(root)

    def _abs(self, path: str) -> Path:
        clean = path.replace("\\", "/").strip("/")
        return self._root / clean if clean else self._root

    def exists(self, path: str) -> bool:
        return self._abs(path).exists()

    def isdir(self, path: str) -> bool:
        return self._abs(path).is_dir()

    def size(self, path: str) -> int:
        return self._abs(path).stat().st_size

    def read(self, path: str, offset: int = 0, size: int | None = None) -> bytes:
        with self._abs(path).open("rb") as handle:
            handle.seek(offset)
            return handle.read(size) if size is not None else handle.read()

    def listdir(self, path: str) -> list[str]:
        return sorted(p.name for p in self._abs(path).iterdir())

    def local(self, path: str) -> str | None:
        return str(self._abs(path))

    def close(self) -> None:
        return None


class IsoFS:
    def __init__(self, path: str | Path) -> None:
        self.root = str(path)
        try:
            self.image = ISOImage(path)
        except UnsupportedImage:
            # Blu-ray images are UDF 2.50 without an ISO9660 PVD.
            try:
                self.image = UDFImage(path)
            except (UnsupportedImage, IsoError) as exc:
                raise UnsupportedImage(str(exc)) from exc
        self._tmp: Path | None = None

    def exists(self, path: str) -> bool:
        return self.image.exists(path)

    def isdir(self, path: str) -> bool:
        return self.image.isdir(path)

    def size(self, path: str) -> int:
        return self.image.size(path)

    def read(self, path: str, offset: int = 0, size: int | None = None) -> bytes:
        return self.image.read(path, offset, size)

    def listdir(self, path: str) -> list[str]:
        return self.image.listdir(path)

    def local(self, path: str) -> str | None:
        return None

    def extract(self, paths: Iterable[str], destination: Path) -> Path:
        """Materialise ISO entries so ffmpeg can read them."""
        destination.mkdir(parents=True, exist_ok=True)
        for name in paths:
            target = destination / name.replace("\\", "/")
            target.parent.mkdir(parents=True, exist_ok=True)
            self.image.extract(name, target)
        return destination

    def close(self) -> None:
        self.image.close()
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self) -> "IsoFS":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _is_vob(path: str) -> bool:
    return path.replace("\\", "/").upper().endswith(".VOB")


class CssFS:
    """ISO9660 tree on a CSS-protected drive, decrypted through libdvdcss.

    Metadata sectors are read without decryption; libdvdcss decrypts every
    sector of a CSS disc when asked, which would corrupt the ISO9660 file
    system.  ``DvdcssFile.set_mpeg`` is toggled by :class:`ISOImage` so only
    VOB payload is descrambled.
    """

    def __init__(self, target: str, library: DvdcssLibrary) -> None:
        self.root = target
        self._handle = library.open(target)
        try:
            self.image = ISOImage(source=DvdcssFile(self._handle, target))
        except Exception:
            self._handle.close()
            raise

    def exists(self, path: str) -> bool:
        return self.image.exists(path)

    def isdir(self, path: str) -> bool:
        return self.image.isdir(path)

    def size(self, path: str) -> int:
        return self.image.size(path)

    def read(self, path: str, offset: int = 0, size: int | None = None) -> bytes:
        return self.image.read(path, offset, size, mpeg=_is_vob(path))

    def raw_read(self, path: str, offset: int = 0,
                 size: int | None = None) -> bytes:
        """Read sectors without decryption (used by a raw disc backup)."""
        return self.image.read(path, offset, size, mpeg=False)

    def listdir(self, path: str) -> list[str]:
        return self.image.listdir(path)

    def local(self, path: str) -> str | None:
        return None

    def extract(self, paths: Iterable[str], destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        for name in paths:
            target = destination / name.replace("\\", "/")
            self.image.extract(name, target, mpeg=_is_vob(name))
        return destination

    def close(self) -> None:
        self.image.close()

    def __enter__(self) -> "CssFS":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def detect_kind(fs: DiscFS) -> str:
    """Detection order from CLONE_SPEC §12.2."""
    if fs.exists("BDMV/index.bdmv") or fs.exists("BDMV/MovieObject.bdmv"):
        return "Blu-ray"
    if fs.exists("BDAV/info.bdav"):
        return "Blu-ray"
    if fs.exists("VIDEO_TS/VIDEO_TS.IFO"):
        return "DVD"
    if fs.exists("HVA00001.VTI"):
        return "HD-DVD"
    if fs.exists("MKBROM.AACS") or fs.exists("MKB_RO.inf"):
        return "Data"
    return "Data"


def read_text(fs: DiscFS, path: str, limit: int = 1 << 20) -> str:
    return fs.read(path, 0, limit).decode("utf-8", "replace")
