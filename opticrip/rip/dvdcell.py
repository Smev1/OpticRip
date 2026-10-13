"""DVD cell-range extractor - OpticRip's own extraction path.

Reads the exact 2048-byte sector ranges a title's cells occupy (parsed from
the IFO by opticrip.disc.dvd) straight from the VOB file(s) into a complete
MPEG-PS file, then remuxes `-c copy`. Benefits over driving libdvdnav:

* no menu VM / first-play PGC required - malformed discs still work;
* deterministic byte ranges, cancellable and resumable at sector chunks;
* read retries with clear attribution (sector/cell/VTS) per failure.

Sector ranges are relative to the VTS title-set's VOB file set
(``VTS_xx_1.VOB``, plus ``VTS_xx_2..5.VOB`` when a VOB exceeds 1 GiB).
This module maps *sector offsets within the VTS set* to per-file offsets
exactly the way libdvdread's ``DVDReadBlocksPath`` does.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Iterator

from ..core.errors import CancelledError, DiscReadError, ExtractionError
from ..core.interfaces import (
    CancelCheck,
    ExtractionResult,
    ProgressCallback,
    StreamExtractor,
    never_cancelled,
    null_progress,
)
from ..core.logging_bus import LogBus
from ..core.models import (
    Drive,
    DriveKind,
    MediaType,
    ProgressUpdate,
    RipPhase,
    StreamInfo,
    Title,
    TitleSelection,
    selection_to_streams,
)
from ..disc.fs import Iso9660Disc, open_disc_filesystem
from .tools import Toolchain

SECTOR = 2048
VOB_FILES_PER_VTS = 9          # VTS_xx_1.VOB .. VTS_xx_9.VOB per spec
MAX_READ_ATTEMPTS = 3
RETRY_DELAY = 0.25


ANGLE_BLOCK_TYPE = 0x01  # per libdvdread BLOCK_TYPE_ANGLE_BLOCK


def _select_angle_cells(cells: list[dict], title: Title,
                        angle: int) -> list[dict]:
    """Reduce a title's cell list to the cells of one angle.

    DVDs store multi-angle titles as repeated "blocks": N cells per angle,
    ordered angle by angle, so the block containing (mode first..last,
    type angle) has ``nb_angles`` members and the wanted one is index
    ``angle-1`` of each block. When a title has no angle blocks or asks for
    angle 1 of 1, the cell list is returned unchanged.
    """
    if angle <= 1 and title.angles <= 1:
        # also defensively drop any stray angle blocks: use only non-block
        # cells plus the first cell of each block (angle 1's members).
        return _first_angle_of_blocks(cells)
    if title.angles <= 1:
        return cells
    if angle > title.angles:
        raise ExtractionError(
            f"title has {title.angles} angle(s); angle {angle} does not exist")

    picked: list[dict] = []
    block: list[dict] = []
    for cell in cells:
        block.append(cell)
        if cell.get("block_mode") in (0x01, 0x02, 0x03) and \
                cell.get("block_type") == ANGLE_BLOCK_TYPE and \
                cell.get("block_mode") != 0x03:
            continue  # block continues
        # block (or non-block cell) terminated
        if len(block) >= title.angles:
            idx = min(angle, len(block)) - 1
            picked.append(block[idx])
        else:
            # malformed short block: keep it rather than lose content
            picked.extend(block)
        block = []
    if block:
        picked.extend(block)
    return picked


def _first_angle_of_blocks(cells: list[dict]) -> list[dict]:
    picked: list[dict] = []
    skip_rest_of_block = False
    for cell in cells:
        in_block = cell.get("block_type") == ANGLE_BLOCK_TYPE and \
            cell.get("block_mode") in (0x01, 0x02, 0x03)
        if in_block:
            mode = cell.get("block_mode")
            if mode == 0x01:          # first cell of a block
                picked.append(cell)   # keep angle 1
                skip_rest_of_block = True
            elif skip_rest_of_block:
                pass                  # skip angle 2..n cells
            if mode == 0x03:          # last cell of the block
                skip_rest_of_block = False
        else:
            skip_rest_of_block = False
            picked.append(cell)
    return picked


def _vob_files(vts_dir: str, vts_number: int) -> list[str]:
    """Absolute paths of the VOB parts for one title set, in order."""
    parts = []
    for i in range(1, VOB_FILES_PER_VTS + 1):
        path = os.path.join(vts_dir, f"VTS_{vts_number:02d}_{i}.VOB")
        if os.path.isfile(path):
            parts.append(path)
    return parts


def _iso_vob_ranges(fs: "Iso9660Disc", vts_number: int):
    """VOB parts for a title set inside an ISO image, as resolvable extents.

    Returns ``[(extent_path, start_sector, n_sectors, lba), ...]`` where
    ``lba`` is the absolute sector of the extent inside the image and
    ``start_sector`` is the offset within the virtual VOB chain (what the IFO
    cell addresses refer to).
    """
    ranges = []
    total = 0
    for i in range(1, VOB_FILES_PER_VTS + 1):
        for base in (f"VIDEO_TS/VTS_{vts_number:02d}_{i}.VOB",
                     f"VTS_{vts_number:02d}_{i}.VOB"):
            resolved = None
            try:
                resolved = fs.find_case_insensitive(base)
            except Exception:
                resolved = None
            if resolved:
                entry = fs._find(fs._normalise(resolved))  # noqa: SLF001
                if entry is not None and not entry.is_dir:
                    n = (entry.size + SECTOR - 1) // SECTOR
                    ranges.append((resolved, total, n, entry.lba))
                    total += n
                break
    return ranges, total


def _lba_for(ranges, path: str) -> int:
    """Absolute LBA of a region when reading through an image."""
    for name, _start, _n, lba in ranges:
        if name == path and lba is not None:
            return int(lba)
    return 0


class _IsoExtentReader:
    """Reads decrypted/plain image sectors by absolute LBA."""

    def __init__(self, fs: "Iso9660Disc") -> None:
        self._device = fs.device

    def read(self, lba: int, sectors: int) -> bytes:
        return self._device.read_sectors(lba, sectors)


def _sector_map(files: list[str]) -> tuple[list[tuple[str, int, int]], int]:
    """File, start_sector, n_sectors for each VOB part + total."""
    ranges = []
    total = 0
    for path in files:
        size_sectors = os.path.getsize(path) // SECTOR
        ranges.append((path, total, size_sectors))
        total += size_sectors
    return ranges, total


def _iter_sectors(ranges, start: int, end: int, chunk: int = 64) -> Iterator[tuple[str, int, int]]:
    """Yield ``(file, file_sector_offset, count)`` covering [start, end]."""
    pos = start
    # walk: find the file containing 'pos', then emit chunks until moving on.
    idx = 0
    while pos <= end and idx < len(ranges):
        entry = ranges[idx]
        path, f_start, f_len = entry[0], entry[1], entry[2]
        if pos < f_start or pos >= f_start + f_len:
            idx += 1
            continue
        take = min(chunk, end - pos + 1, f_start + f_len - pos)
        yield path, pos - f_start, take
        pos += take
        if pos >= f_start + f_len:
            idx += 1


class DvdCellExtractor(StreamExtractor):
    """Extracts the exact cell sectors of one DVD title to a local PS file."""

    name = "dvd-cell"

    def __init__(self, toolchain: Toolchain | None = None,
                 log: LogBus | None = None) -> None:
        self.tc = toolchain or Toolchain.detect()
        self._log = log or LogBus()

    # ---------------------------------------------------------------- #
    def can_handle(self, drive: Drive, title: Title) -> bool:
        if drive.media_type is not MediaType.DVD_VIDEO:
            return False
        # Folders: VOB files on disk.  Images: the same VOB files addressed
        # through our own ISO9660 reader.  Raw devices still need libdvdnav
        # (ffmpeg dvdvideo), so this extractor declines them rather than
        # claiming the job and failing later.
        if drive.kind not in (DriveKind.FOLDER, DriveKind.IMAGE):
            return False
        ref = title.source_ref or {}
        return ref.get("kind") == "dvd-pgc" and bool(ref.get("cells"))

    # ---------------------------------------------------------------- #
    def extract(self, drive: Drive, title: Title, selection: TitleSelection,
                work_dir: str, *, progress: ProgressCallback = null_progress,
                cancelled: CancelCheck = never_cancelled) -> ExtractionResult:
        log = self._log.bind(f"cell-extract:{title.id}")
        ref = title.source_ref
        vts = int(ref["vts"])
        all_cells = ref["cells"]
        if not all_cells:
            raise ExtractionError(f"title {title.id} has no cell ranges")

        cells = _select_angle_cells(all_cells, title, angle=int(
            title.source_ref.get("angle") or 1))
        iso = None

        # locate VOB parts: on-disk files (folder) or ISO9660 extents (image)
        if drive.kind is DriveKind.FOLDER:
            vts_dir = os.path.join(drive.path, "VIDEO_TS")
            if not os.path.isdir(vts_dir):
                vts_dir = drive.path
            files = _vob_files(vts_dir, vts)
            if not files:
                raise ExtractionError(
                    f"no VOB files for VTS {vts:02d} under {vts_dir}",
                    hint="Check that the folder is a complete VIDEO_TS tree.")
            ranges, total_sectors = _sector_map(files)
            ranges = [(path, start, n, None) for path, start, n in ranges]
            log.info(f"VOB parts: {files}")
        else:  # IMAGE
            fs = open_disc_filesystem(drive.path)
            if not isinstance(fs, Iso9660Disc):
                raise ExtractionError(
                    f"{drive.path} is not an ISO9660 image",
                    hint="UDF-only images need an ffmpeg build with the "
                         "dvdvideo demuxer; re-author to ISO9660 or extract "
                         "the tree to a folder.")
            ranges, total_sectors = _iso_vob_ranges(fs, vts)
            iso = _IsoExtentReader(fs)
            if not ranges:
                raise ExtractionError(
                    f"no VOB extents for VTS {vts:02d} inside {drive.path}",
                    hint="The image may be UDF-only or incomplete.")

        first = min(int(c["first_sector"]) for c in cells)
        last = max(int(c["last_sector"]) for c in cells)
        if last >= total_sectors:
            raise DiscReadError(
                f"title wants sectors up to {last} but the VOB set only has "
                f"{total_sectors}",
                hint="The disc may be damaged or the fixture malformed.")
        chunk_sectors = cells and max(
            int(c["last_sector"]) - int(c["first_sector"]) + 1 for c in cells)
        log.info(f"copying sectors [{first}, {last}] of VTS_{vts:02d} "
                 f"({len(ranges)} part(s), {total_sectors} sectors total)")

        os.makedirs(work_dir, exist_ok=True)
        temp_path = os.path.join(work_dir, f"extract-{title.id}.ps")
        log2 = log
        total_bytes = 0
        started = time.time()
        with open(temp_path, "wb") as out:
            for path, file_sector, count in _iter_sectors(ranges, first, last):
                if cancelled():
                    log2.warning("cancelled by user")
                    raise CancelledError("extraction cancelled")
                attempt = 0
                while True:
                    try:
                        if iso is not None:
                            base_lba = _lba_for(ranges, path)
                            data = iso.read(base_lba + file_sector, count)
                        else:
                            with open(path, "rb") as src:
                                src.seek(file_sector * SECTOR)
                                data = src.read(count * SECTOR)
                        break
                    except OSError as exc:
                        attempt += 1
                        if attempt >= MAX_READ_ATTEMPTS:
                            raise DiscReadError(
                                f"read failed at {path} sector "
                                f"{file_sector} after {attempt} tries",
                                detail=str(exc))
                        log2.warning(f"retry {attempt}: {exc}")
                        time.sleep(RETRY_DELAY * attempt)
                out.write(data)
                total_bytes += len(data)
                if title.duration_seconds:
                    done = (total_bytes / SECTOR) / max(1, (last - first + 1))
                    progress(ProgressUpdate(
                        job_id="", phase=RipPhase.EXTRACTING,
                        fraction=min(1.0, done),
                        message=f"{title.display_name} cells",
                    ))
        elapsed = time.time() - started
        if total_bytes == 0:
            raise ExtractionError("no sectors were copied - nothing to mux")
        log.info(f"cell copy: {total_bytes} bytes in {elapsed:.2f}s")
        return ExtractionResult(
            paths=[temp_path],
            streams=selection_to_streams(title, selection),
            cleanup=True,
            protocol="file",
            size_bytes=total_bytes,
            duration_hint=title.duration_seconds,
        )
