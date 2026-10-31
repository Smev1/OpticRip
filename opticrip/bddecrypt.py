"""AACS and BD+ decryption for Blu-ray titles.

The engine still remuxes with ffmpeg, which cannot decrypt AACS.  When a
Blu-ray is protected and the user has supplied libaacs/libbdplus, the clips a
title needs are decrypted into a temporary folder first, in 6144-byte units,
and ffmpeg then reads those clear clips.  This mirrors the DVD cell-staging
path and keeps decryption out of the remux code.

Both libraries are optional.  Nothing here runs until an encrypted Blu-ray is
opened for saving, and failures carry a user-facing reason.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable, Iterable

from . import aacs as aacslib
from . import bdplus as bdpluslib
from .aacs import AacsError, AacsHandle, UNIT_SIZE
from .bdplus import BdplusError, BdplusHandle, BdplusStream
from .discfs import DiscFS
from .iso9660 import IsoError
from .model import Segment
from .settings import Settings

#: Read 512 units (3 MiB) at a time.  Small enough to report progress often,
#: large enough that ISO9660 reads and optical-drive seeks stay cheap.
CHUNK_UNITS = 512
READ_SIZE = UNIT_SIZE * CHUNK_UNITS


class BlurayDecryptError(RuntimeError):
    """The disc cannot be decrypted with the configured libraries."""


def _has_bdplus(fs: DiscFS) -> bool:
    return (fs.exists("BDSVM/00000.svm")
            or fs.exists("BDMV/BDSVM/00000.svm")
            or fs.exists("BDAV/BDSVM/00000.svm"))


class BlurayDecryptor:
    """A prepared AACS/BD+ decoder for one open Blu-ray source."""

    def __init__(self, fs: DiscFS, settings: Settings | None = None,
                 message: Callable[..., None] | None = None) -> None:
        self.fs = fs
        self.settings = settings
        self.message = message
        self.aacs: AacsHandle | None = None
        self.bdplus: BdplusHandle | None = None
        self.bdplus_protected = _has_bdplus(fs)
        self._temp: tempfile.TemporaryDirectory | None = None
        self._prepared = False
        self._closed = False

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    def _setting(self, key: str) -> str:
        if self.settings is None:
            return ""
        return self.settings.get_str(key).strip()

    def _say(self, ident: int, *params) -> None:
        if self.message is not None:
            self.message(ident, *params)

    @property
    def prepared(self) -> bool:
        return self._prepared

    @property
    def active(self) -> bool:
        return self._prepared and not self._closed

    def prepare(self, num_titles: int = 1) -> "BlurayDecryptor":
        """Load the libraries and open the disc; safe to call repeatedly."""
        if self._closed:
            raise BlurayDecryptError("decryptor is closed")
        if self._prepared:
            return self
        data_dir = self.settings.data_dir if self.settings is not None else None

        try:
            explicit = self._setting("app_AacsPath") or \
                os.environ.get("OPTICRIP_LIBAACS", "").strip()
            try:
                library, used = aacslib.resolve(explicit or None, data_dir)
            except AacsError as exc:
                raise BlurayDecryptError(str(exc)) from exc
            self._say(9131, str(used))

            root = self._library_root()
            keyfile = self._keyfile()
            try:
                self.aacs = library.open(str(root), keyfile)
            except (AacsError, OSError) as exc:
                raise BlurayDecryptError(str(exc)) from exc

            if self.bdplus_protected:
                explicit = self._setting("app_BdplusPath") or \
                    os.environ.get("OPTICRIP_LIBBDPLUS", "").strip()
                try:
                    bd_library, used = bdpluslib.resolve(explicit or None, data_dir)
                except BdplusError as exc:
                    raise BlurayDecryptError(str(exc)) from exc
                self._say(9135, str(used))
                self._say(9142)
                vid = self.aacs.volume_id
                if not vid:
                    raise BlurayDecryptError(
                        "libaacs did not provide the disc volume ID (VID), "
                        "which BD+ requires"
                    )
                media_key = self.aacs.media_key
                if not media_key:
                    raise BlurayDecryptError(
                        "libaacs did not provide the media key, "
                        "which BD+ requires"
                    )
                try:
                    self.bdplus = bd_library.open(
                        root, vid, media_key, num_titles=num_titles,
                    )
                except (BdplusError, OSError) as exc:
                    raise BlurayDecryptError(str(exc)) from exc
        except Exception:
            self.close()
            raise

        self._prepared = True
        return self

    def _keyfile(self) -> str | None:
        explicit = self._setting("app_AacsKeyFile")
        if not explicit:
            explicit = os.environ.get("OPTICRIP_AACS_KEYDB", "").strip()
        if explicit:
            path = Path(explicit)
            if not path.is_file():
                raise BlurayDecryptError(f"AACS key file not found: {path}")
            return str(path)
        if self.settings is not None:
            candidate = Path(self.settings.data_dir) / "aacs" / "KEYDB.cfg"
            if candidate.is_file():
                return str(candidate)
        return None

    def _aacs_tree(self) -> str | None:
        for path in ("AACS", "BDMV/AACS", "BDAV/AACS"):
            if (self.fs.exists(f"{path}/Unit_Key_RO.inf")
                    or self.fs.exists(f"{path}/DUPLICATE/Unit_Key_RO.inf")):
                return path
        return None

    def _svm_tree(self) -> str | None:
        for path in ("BDSVM", "BDMV/BDSVM", "BDAV/BDSVM"):
            if self.fs.exists(f"{path}/00000.svm"):
                return path
        return None

    def _library_root(self) -> Path:
        """A folder with ``AACS/`` (and ``BDSVM/``) that libaacs can read.

        Mounted discs and folders are used in place when their layout is
        already what libaacs/libbdplus expect.  libbdplus only looks for
        ``BDSVM/00000.svm`` at the root, so a disc that keeps it under ``BDMV``
        (or whose AACS directory is nested) is staged instead.  ISO images are
        never visible to the libraries, so their key metadata is copied to a
        temporary folder as well; clip payload still comes from the source and
        is decrypted by this module.
        """
        aacs = self._aacs_tree()
        svm = self._svm_tree()
        if aacs is not None:
            in_place = (aacs == "AACS"
                        and (not self.bdplus_protected or svm == "BDSVM"))
            if in_place:
                local = self.fs.local("AACS/Unit_Key_RO.inf")
                if local and Path(local).is_file():
                    root = Path(local).parent.parent
                    if root.is_dir():
                        return root
                root_text = str(self.fs.root)
                if Path(root_text).is_dir():
                    return Path(root_text)

        if self._temp is None:
            self._temp = tempfile.TemporaryDirectory(prefix="opticrip-aacs-")
        target = Path(self._temp.name)
        try:
            if aacs is not None:
                self._copy_tree(aacs, target / "AACS")
            else:
                raise BlurayDecryptError(
                    "the disc has AACS metadata but no AACS/Unit_Key_RO.inf"
                )
            if svm is not None:
                self._copy_tree(svm, target / "BDSVM")
        except (OSError, IsoError) as exc:
            raise BlurayDecryptError(
                f"could not read the AACS/BD+ metadata: {exc}"
            ) from exc
        return target

    def _copy_tree(self, source: str, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        for name in self.fs.listdir(source):
            path = f"{source}/{name}"
            if self.fs.isdir(path):
                self._copy_tree(path, target / name)
            else:
                (target / name).write_bytes(self.fs.read(path))

    # ------------------------------------------------------------------
    # staging
    # ------------------------------------------------------------------
    def stage(self, segments: Iterable[Segment],
              workdir: Path | None = None,
              on_progress: Callable[[int, int], None] | None = None,
              cancelled: Callable[[], bool] | None = None
              ) -> tuple[list[str], tempfile.TemporaryDirectory | None]:
        """Decrypt every clip a title uses and return seekable paths."""
        self.prepare()
        assert self.aacs is not None
        ordered = [segment.path for segment in segments]
        unique: list[str] = []
        for path in ordered:
            if path not in unique:
                unique.append(path)
        if not unique:
            raise BlurayDecryptError("title has no source clips")
        total = 0
        for path in unique:
            try:
                total += int(self.fs.size(path))
            except (OSError, IsoError) as exc:
                raise BlurayDecryptError(f"cannot read {path}: {exc}") from exc
        if total <= 0:
            raise BlurayDecryptError("title has no readable video data")
        if workdir is not None:
            workdir = Path(workdir).resolve()

        temporary = tempfile.TemporaryDirectory(
            prefix="opticrip-bd-",
            dir=str(workdir) if workdir is not None else None,
        )
        destination = Path(temporary.name)
        copied = 0
        last_reported = -1
        report_step = max(UNIT_SIZE, total // 200)

        def report(value: int) -> None:
            nonlocal last_reported
            if on_progress is None:
                return
            value = min(max(0, int(value)), total)
            if value == last_reported:
                return
            if value == 0 or value == total or value - last_reported >= report_step:
                last_reported = value
                on_progress(value, total)

        try:
            # CPS units are per title; resetting makes libaacs auto-detect the
            # key from the first unit instead of relying on playlist numbers.
            self.aacs.reset()
            report(0)
            for path in unique:
                target = destination / path.replace("\\", "/")
                target.parent.mkdir(parents=True, exist_ok=True)
                copied += self._decrypt_clip(
                    path, target, lambda done, base=copied: report(base + done),
                    cancelled,
                )
            report(copied)
            if not any((destination / path.replace("\\", "/")).stat().st_size > 0
                       for path in unique):
                raise BlurayDecryptError("decrypted clips are empty")
        except (Exception, KeyboardInterrupt):
            temporary.cleanup()
            raise
        paths = [str(destination / path.replace("\\", "/")) for path in ordered]
        return paths, temporary

    def decrypt_clip(self, path: str, target: Path,
                     on_progress: Callable[[int, int], None] | None = None,
                     cancelled: Callable[[], bool] | None = None,
                     reset_unit: bool = True) -> int:
        """Decrypt one clip to ``target`` (whole-disc backup path).

        Each clip may belong to a different CPS unit, so the unit is reset by
        default and libaacs re-detects it from the clip's first unit.
        """
        self.prepare()
        if self.aacs is None:
            raise BlurayDecryptError("AACS is not prepared")
        if reset_unit:
            self.aacs.reset()
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = int(self.fs.size(path))
        except OSError:
            size = 0
        return self._decrypt_clip(
            path, target,
            (lambda done: on_progress(done, size)) if on_progress else None,
            cancelled,
        )

    def _decrypt_clip(self, path: str, target: Path,
                      on_chunk: Callable[[int], None] | None,
                      cancelled: Callable[[], bool] | None) -> int:
        assert self.aacs is not None
        clip_id = clip_number(path)
        stream: BdplusStream | None = None

        def check_cancel() -> None:
            if cancelled is not None and cancelled():
                raise KeyboardInterrupt

        written = 0
        pending = bytearray()

        def consume(chunk: bytes) -> None:
            nonlocal written
            pending.extend(chunk)
            start = 0
            while len(pending) - start >= UNIT_SIZE:
                unit = bytearray(pending[start:start + UNIT_SIZE])
                start += UNIT_SIZE
                check_cancel()
                self.aacs.decrypt_unit(unit)
                if stream is not None and stream.fixup(unit) < 0:
                    raise BdplusError("BD+ could not patch the stream")
                target_handle.write(unit)
                written += len(unit)
                if on_chunk is not None:
                    on_chunk(written)
            if start:
                # One compaction per chunk instead of one per unit.
                del pending[:start]

        target_handle = None
        try:
            if self.bdplus is not None:
                if clip_id is not None:
                    stream = self.bdplus.clip(clip_id)
                if stream is None:
                    # Not every clip is BD+ protected; AACS alone is still valid.
                    self._say(9141, Path(path).name)
            target_handle = target.open("wb")
            check_cancel()
            local = self.fs.local(path)
            if local:
                with open(local, "rb") as source:
                    while True:
                        chunk = source.read(READ_SIZE)
                        if not chunk:
                            break
                        check_cancel()
                        consume(chunk)
            else:
                offset = 0
                while True:
                    chunk = self.fs.read(path, offset, READ_SIZE)
                    if not chunk:
                        break
                    offset += len(chunk)
                    check_cancel()
                    consume(chunk)
            if pending:
                # A trailing partial unit is not encrypted; copy it verbatim.
                target_handle.write(pending)
                written += len(pending)
                if on_chunk is not None:
                    on_chunk(written)
            target_handle.flush()
        except KeyboardInterrupt:
            raise
        except (OSError, IsoError, AacsError, BdplusError) as exc:
            raise BlurayDecryptError(f"could not decrypt {path}: {exc}") from exc
        finally:
            if target_handle is not None:
                target_handle.close()
            if stream is not None:
                stream.close()
        return written

    # ------------------------------------------------------------------
    # teardown
    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.bdplus is not None:
            self.bdplus.close()
            self.bdplus = None
        if self.aacs is not None:
            self.aacs.close()
            self.aacs = None
        if self._temp is not None:
            self._temp.cleanup()
            self._temp = None

    def __enter__(self) -> "BlurayDecryptor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def clip_number(path: str) -> int | None:
    stem = Path(path.replace("\\", "/")).stem
    return int(stem) if stem.isdigit() else None
