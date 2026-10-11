"""Identify what is on a disc without mounting it.

Everything here works on raw 2048-byte logical sectors.  Two independent
"what is this" questions are answered:

* **filesystem / format** - ISO9660 volume descriptor, UDF recognition
  sequence, or an audio CD table of contents;
* **content** - whether the tree contains ``VIDEO_TS`` (DVD-Video) or
  ``BDMV`` (Blu-ray).

Protection detection is an explicitly *heuristic* report, and it never
attempts to defeat anything (see ``docs/PROTECTED_DISCS.md``).
"""

from __future__ import annotations

import errno
import os
import stat as _stat
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..core.models import MediaType

SECTOR = 2048
#: ISO9660 primary volume descriptor lives at logical sector 16
PVD_SECTOR = 16
#: DVD/Blu-ray need at least this many readable sectors to be interesting
_MIN_SECTORS = 32

# protection markers we look for, as plain directory names at the disc root
_AACS_DIRS = {"AACS", "BDSVM"}


@dataclass
class MediaProbeResult:
    """Outcome of probing one device/image for media."""

    has_media: bool = False
    media_type: MediaType = MediaType.EMPTY
    volume_label: str = ""
    capacity_bytes: int | None = None
    filesystem: str = ""
    sector_size: int = SECTOR
    #: human-readable reasons we think the disc is protected (never bypassed)
    protection: list[str] = field(default_factory=list)
    #: set when the device could not be read (permission, no medium, I/O error)
    error: str | None = None
    error_hint: str | None = None
    raw_signatures: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Signature helpers (pure functions - unit tested directly)
# --------------------------------------------------------------------------- #


def is_iso9660_pvd(sector: bytes) -> bool:
    """``CD001`` at offset 1 of a volume descriptor."""
    return len(sector) >= 7 and sector[0] == 0x01 and sector[1:6] == b"CD001"


def iso9660_volume_label(sector: bytes) -> str:
    """Label from a PVD (offset 40, 32 bytes, space padded)."""
    if len(sector) < 72:
        return ""
    return sector[40:72].decode("ascii", "replace").strip()


def udf_versions(vrs: bytes) -> list[str]:
    """UDF identifiers found in the Volume Recognition Sequence."""
    found: list[str] = []
    # Each Volume Structure Descriptor is one logical sector; its standard
    # identifier is 5 bytes ("BEA01", "NSR02", "NSR03", "TEA01").
    for offset in range(0, len(vrs) - 5, SECTOR):
        entry = vrs[offset:offset + 5]
        if entry in (b"BEA01", b"NSR02", b"NSR03", b"TEA01"):
            name = entry.decode("ascii")
            if name not in found:
                found.append(name)
    return found


def classify_root_names(names: list[str]) -> MediaType:
    """Decide DVD-Video vs Blu-ray from the root directory listing."""
    upper = {n.upper() for n in names}
    if "BDMV" in upper:
        return MediaType.BLURAY
    if "VIDEO_TS" in upper:
        return MediaType.DVD_VIDEO
    if "AUDIO_TS" in upper:
        # A DVD with only AUDIO_TS and no VIDEO_TS is a DVD-Audio disc.
        return MediaType.DATA
    return MediaType.DATA


def protection_from_names(names: list[str], *,
                          media_type: MediaType | None = None) -> list[str]:
    """Best-effort protection *suspicion* from root directory names."""
    upper = {n.upper() for n in names}
    found: list[str] = []
    if upper & _AACS_DIRS:
        found.append("AACS (Blu-ray)")
    if media_type is MediaType.DVD_VIDEO and "VIDEO_TS" in upper:
        # no directory tells us about CSS; reported by the device probe below
        pass
    return found


def looks_encrypted_dvd_pack(first_vob_sector: bytes) -> bool:
    """Heuristic: a clean, unencrypted MPEG pack starts ``00 00 01 BA``.

    CSS leaves the pack header readable but scrambles the payload, so a VOB
    whose *first* sector is not a pack start is suspicious *only* when the
    disc is already known to be DVD-Video.  This is a hint for the user, not
    proof, and is never used to attempt decryption.
    """
    if len(first_vob_sector) < 4:
        return False
    return first_vob_sector[:4] != b"\x00\x00\x01\xba"


# --------------------------------------------------------------------------- #
# Probe interface
# --------------------------------------------------------------------------- #


class MediaProbe(ABC):
    """Reads just enough of a device to say what is on it."""

    @abstractmethod
    def probe(self, device_path: str) -> MediaProbeResult: ...


class LinuxMediaProbe(MediaProbe):
    """Real 2048-byte-sector probing of a block device using ``os.pread``.

    Requires read access to the device node.  OpticRip never needs root: add
    the user to the ``cdrom`` group instead (see ``docs/INSTALL.md``).
    """

    def __init__(self, sector_size: int = SECTOR) -> None:
        self.sector_size = sector_size

    # -- helpers -------------------------------------------------------- #
    def _device_capacity(self, fd: int) -> int | None:
        try:
            from fcntl import ioctl
            # BLKGETSIZE64 = 0x80081272 on Linux
            buf = bytearray(8)
            ioctl(fd, 0x80081272, buf, True)
            size = struct.unpack("<Q", buf)[0]
            if size:
                return size
        except (OSError, ImportError):
            pass
        return None

    def _audio_cd_toc(self, fd: int) -> bool:
        try:
            import fcntl
            # CDROMREADTOCHDR = 0x5305 -> returns first/last track
            buf = bytearray(2)
            fcntl.ioctl(fd, 0x5305, buf, True)
            first, last = buf[0], buf[1]
            return 0 < first <= last
        except (OSError, ImportError):
            return False

    # -- main ----------------------------------------------------------- #
    def probe(self, device_path: str) -> MediaProbeResult:
        result = MediaProbeResult()
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(device_path, flags)
        except OSError as exc:
            result.error = _describe_open_error(exc, device_path)
            result.error_hint = _hint_for_errno(exc.errno, device_path)
            return result

        try:
            st = os.fstat(fd)
            if _stat.S_ISREG(st.st_mode):
                result.capacity_bytes = st.st_size
            else:
                result.capacity_bytes = self._device_capacity(fd)

            if result.capacity_bytes == 0:
                result.media_type = MediaType.EMPTY
                result.error = None
                return result
            if result.capacity_bytes is not None and \
                    result.capacity_bytes < _MIN_SECTORS * self.sector_size:
                # e.g. a USB stick; not a video disc
                result.has_media = True
                result.media_type = MediaType.DATA
                return result

            vrs = os.pread(fd, self.sector_size * 6,
                           PVD_SECTOR * self.sector_size)
            if len(vrs) < self.sector_size:
                result.error = "the disc did not return its volume descriptors"
                result.error_hint = ("The disc may be dirty, scratched or "
                                     "not finalised.")
                return result

            pvd = vrs[:self.sector_size]
            labels = udf_versions(vrs)
            if is_iso9660_pvd(pvd):
                result.filesystem = "ISO9660"
                result.volume_label = iso9660_volume_label(pvd)
            if labels:
                result.filesystem = (result.filesystem + "+UDF") if result.filesystem \
                    else "UDF"
                result.raw_signatures["udf"] = ",".join(sorted(set(labels)))

            result.has_media = True
            if not result.filesystem:
                # No ISO9660/UDF: could be an audio CD or a foreign filesystem.
                if self._audio_cd_toc(fd):
                    result.media_type = MediaType.CD_AUDIO
                    result.filesystem = "CD-DA"
                else:
                    result.media_type = MediaType.DATA
                return result

            # Filesystem recognised: content is decided by the callers that can
            # walk the tree (iso9660 / directory analyzers).
            result.media_type = MediaType.UNKNOWN
            return result
        except OSError as exc:
            result.error = f"read error while probing {device_path}: {exc.strerror}"
            result.error_hint = _hint_for_errno(exc.errno, device_path)
            return result
        finally:
            os.close(fd)


def _describe_open_error(exc: OSError, path: str) -> str:
    if exc.errno == errno.ENOENT:
        return f"{path} does not exist"
    if exc.errno == errno.EACCES:
        return f"permission denied opening {path}"
    if exc.errno in (errno.ENOMEDIUM, getattr(errno, "ENODATA", 61)):
        return f"no medium in {path}"
    return f"cannot open {path}: {exc.strerror}"


def _hint_for_errno(err: int | None, path: str) -> str | None:
    if err == errno.EACCES:
        return ("Add your user to the 'cdrom' group (or check udev rules) "
                "instead of running as root.")
    if err in (errno.ENOMEDIUM, getattr(errno, "ENODATA", 61)):
        return "Insert a disc."
    if err == errno.ENOENT:
        return "Rescan for drives - the device may have been removed."
    if err == errno.EIO:
        return "The drive reported an I/O error; the disc may be damaged."
    return None


class FakeMediaProbe(MediaProbe):
    """Deterministic probe for tests and the demo drive provider."""

    def __init__(self, results: dict[str, MediaProbeResult] | None = None,
                 default: MediaProbeResult | None = None) -> None:
        self._results = dict(results or {})
        self._default = default or MediaProbeResult()

    def set(self, path: str, result: MediaProbeResult) -> None:
        self._results[path] = result

    def probe(self, device_path: str) -> MediaProbeResult:
        return self._results.get(device_path, self._default)
