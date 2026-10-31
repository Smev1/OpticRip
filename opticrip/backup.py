"""Disc backup writer (CLONE_SPEC §14).

The reference can copy a whole disc either byte-for-byte or with the video
payload decrypted.  This module performs the copy through :class:`DiscFS`, so
folders, ISO images and CSS-locked drives all use the same code, and applies
AACS/BD+ or libdvdcss decryption while writing.

A decrypted Blu-ray backup keeps the original AACS/BD+ metadata under
``MAKEMKV/`` and omits it from the playable part of the tree.  That makes the
result open like an ordinary unencrypted Blu-ray (the reference stores the same
auxiliary data in a ``MAKEMKV`` subtree).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator

from .bddecrypt import BlurayDecryptError, BlurayDecryptor
from .discfs import CssFS, DiscFS

#: Large enough that a 50 GB disc is a few thousand Python read calls, small
#: enough that cancellation and progress stay responsive.
READ_SIZE = 8 << 20

_AUX_TREES = {
    "AACS": "MAKEMKV/AACS",
    "BDMV/AACS": "MAKEMKV/AACS",
    "BDAV/AACS": "MAKEMKV/AACS",
    "BDSVM": "MAKEMKV/BDSVM",
    "BDMV/BDSVM": "MAKEMKV/BDSVM",
    "BDAV/BDSVM": "MAKEMKV/BDSVM",
}


class BackupError(RuntimeError):
    """The backup could not be written."""


def _walk(fs: DiscFS, path: str = "") -> Iterator[str]:
    for name in fs.listdir(path):
        child = f"{path}/{name}" if path else name
        if fs.isdir(child):
            yield from _walk(fs, child)
        else:
            yield child


def is_bluray_clip(path: str) -> bool:
    """True for ``BDMV/STREAM/xxxxx.m2ts`` and ``BDAV/STREAM/...``."""
    parts = path.replace("\\", "/").split("/")
    return (len(parts) >= 3 and parts[-3].upper() in ("BDMV", "BDAV")
            and parts[-2].upper() == "STREAM"
            and parts[-1].lower().endswith(".m2ts"))


def _aux_destination(path: str) -> tuple[str, str] | None:
    """Map an AACS/BDSVM file to its ``MAKEMKV`` copy destination."""
    normal = path.replace("\\", "/")
    for source, target in _AUX_TREES.items():
        if normal == source or normal.startswith(source + "/"):
            relative = normal[len(source):].lstrip("/")
            return source, f"{target}/{relative}" if relative else target
    return None


def _inside(path: str, tree: str) -> bool:
    normal = path.replace("\\", "/")
    return normal == tree or normal.startswith(tree + "/")


class BackupWriter:
    """Copy one opened disc into ``destination``.

    ``decrypt`` requires ``decryptor`` for Blu-ray and a :class:`CssFS` for
    DVD.  ``message`` receives the same numeric message ids the engine uses.
    """

    def __init__(self, fs: DiscFS, destination: Path, *,
                 kind: str = "", decrypt: bool = False,
                 decryptor: BlurayDecryptor | None = None,
                 message: Callable[..., None] | None = None) -> None:
        self.fs = fs
        self.destination = Path(destination)
        self.kind = kind
        self.decrypt = bool(decrypt)
        self.decryptor = decryptor
        self.message = message
        self._aux = self._aux_roots()

    def _aux_roots(self) -> list[str]:
        roots: list[str] = []
        for candidate in _AUX_TREES:
            try:
                if self.fs.isdir(candidate):
                    roots.append(candidate)
            except OSError:
                continue
        return roots

    def _needs_decryption(self, path: str) -> bool:
        if not self.decrypt:
            return False
        if self.kind == "Blu-ray":
            return is_bluray_clip(path)
        if self.kind == "DVD":
            return path.upper().endswith(".VOB")
        return False

    def _aux_target(self, path: str) -> str | None:
        if not self.decrypt:
            return None
        normal = path.replace("\\", "/")
        for root in self._aux:
            if _inside(normal, root):
                mapped = _aux_destination(normal)
                if mapped is not None:
                    return mapped[1]
                relative = normal[len(root):].lstrip("/")
                return f"MAKEMKV/{root.split('/')[-1]}/{relative}" if relative else ""
        return None

    def _files(self) -> list[str]:
        try:
            return sorted(_walk(self.fs))
        except OSError as exc:
            raise BackupError(f"cannot read the disc tree: {exc}") from exc

    def run(self, on_progress: Callable[[int, int], None] | None = None,
            cancelled: Callable[[], bool] | None = None) -> int:
        if cancelled is not None and cancelled():
            raise KeyboardInterrupt
        files = self._files()
        total = 0
        for path in files:
            try:
                total += int(self.fs.size(path))
            except OSError:
                pass
        done = 0
        copied = 0
        try:
            self.destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackupError(f"cannot create {self.destination}: {exc}") from exc

        raw_reader = getattr(self.fs, "raw_read", None)
        for path in files:
            if cancelled is not None and cancelled():
                raise KeyboardInterrupt
            target_name = self._aux_target(path)
            if target_name is not None:
                target = self.destination / target_name
                written = self._copy(path, target, raw_reader, cancelled)
            else:
                target = self.destination / path
                if self._needs_decryption(path):
                    written = self._copy_decrypted(path, target, cancelled)
                else:
                    # A CSS filesystem decrypts every VOB by default; metadata
                    # must be read raw.  Other sources have no raw_read().
                    metadata = not path.upper().endswith(".VOB")
                    reader = raw_reader if (not self.decrypt or metadata) else None
                    written = self._copy(path, target, reader, cancelled)
            copied += 1
            done += written
            if on_progress is not None:
                on_progress(min(done, total), max(total, done))
        if on_progress is not None:
            on_progress(max(total, done), max(total, done))
        return copied

    def _copy_decrypted(self, path: str, target: Path,
                        cancelled: Callable[[], bool] | None) -> int:
        if self.kind == "Blu-ray":
            if self.decryptor is None or not self.decryptor.active:
                raise BackupError("AACS/BD+ decryption is not prepared")
            try:
                return int(self.decryptor.decrypt_clip(
                    path, target, cancelled=cancelled, reset_unit=True,
                ))
            except (BlurayDecryptError, OSError) as exc:
                raise BackupError(str(exc)) from exc
        if not isinstance(self.fs, CssFS):
            raise BackupError(
                "CSS decryption is unavailable; load libdvdcss to make a "
                "decrypted DVD backup"
            )
        return self._copy(path, target, self.fs.read, cancelled)

    def _copy(self, path: str, target: Path,
              reader: Callable[..., bytes] | None,
              cancelled: Callable[[], bool] | None) -> int:
        """Stream one file to ``target``; ``reader`` overrides DiscFS.read."""
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            size = int(self.fs.size(path))
        except OSError as exc:
            raise BackupError(f"cannot read {path}: {exc}") from exc
        offset = 0
        written = 0
        try:
            with target.open("wb") as out:
                while offset < size:
                    if cancelled is not None and cancelled():
                        raise KeyboardInterrupt
                    if reader is None:
                        chunk = self.fs.read(path, offset, min(READ_SIZE, size - offset))
                    else:
                        chunk = reader(path, offset, min(READ_SIZE, size - offset))
                    if not chunk:
                        break
                    out.write(chunk)
                    offset += len(chunk)
                    written += len(chunk)
        except OSError as exc:
            raise BackupError(f"cannot write {target}: {exc}") from exc
        if written != size:
            raise BackupError(
                f"short read from {path}: got {written} of {size} bytes"
            )
        return written
