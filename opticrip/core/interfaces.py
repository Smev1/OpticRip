"""Abstract interfaces between the engine's interchangeable components.

The GUI/cLI and the orchestration layer only ever talk to these protocols, so
replacing, say, the DVD parser or the muxer backend does not ripple through
the app.  Every method is synchronous; cancellation is cooperative via the
``CancelCheck`` callable passed into long-running operations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Optional, Protocol, runtime_checkable

from .models import (
    DiscAnalysis,
    Drive,
    ProgressUpdate,
    RipJob,
    StreamInfo,
    Title,
    TitleSelection,
    ValidationReport,
)

#: Called often during long operations; must be cheap and non-blocking.
ProgressCallback = Callable[[ProgressUpdate], None]
#: Returns True when the user asked to cancel.
CancelCheck = Callable[[], bool]


def never_cancelled() -> bool:
    return False


def null_progress(_update: ProgressUpdate) -> None:
    return None


# --------------------------------------------------------------------------- #
# Media access
# --------------------------------------------------------------------------- #


class BlockDevice(ABC):
    """Random-access byte reader over 2048-byte logical sectors.

    Raw optical devices, ISO images and test doubles all implement this, so
    the DVD extractor can copy sector ranges without caring where they live.
    """

    #: logical sector size in bytes (2048 for DVD/Blu-ray data)
    sector_size: int = 2048

    @property
    @abstractmethod
    def size_bytes(self) -> int:
        """Total readable size in bytes."""

    @abstractmethod
    def read(self, offset: int, length: int) -> bytes:
        """Read ``length`` bytes at ``offset`` (short reads are returned as-is)."""

    def read_sectors(self, lba: int, count: int) -> bytes:
        return self.read(lba * self.sector_size, count * self.sector_size)

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "BlockDevice":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def describe(self) -> str:  # pragma: no cover - overridden everywhere useful
        return type(self).__name__


class DiscFile(ABC):
    """A named file inside a disc filesystem (e.g. ``VTS_01_0.IFO``)."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def size(self) -> int: ...

    @abstractmethod
    def read(self, offset: int = 0, length: int | None = None) -> bytes: ...


class DiscFileSystem(ABC):
    """Read-only view of the files on a disc, without mounting it.

    Implementations: a plain directory (mounted disc) and ISO9660 (raw
    device/image).  Bluetooth-free, no ``sudo``, no FUSE.
    """

    #: human description e.g. "ISO9660 on /dev/sr0"
    describe: str = ""

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def listdir(self, path: str) -> list[str]: ...

    @abstractmethod
    def getfile(self, path: str) -> DiscFile: ...

    def find_case_insensitive(self, path: str) -> str | None:
        """ISO9660 names are upper-case on disc, lower-case in directories."""
        if self.exists(path):
            return path
        parts = [p for p in path.split("/") if p]
        current = ""
        for part in parts:
            try:
                entries = self.listdir(current or ".")
            except (KeyError, OSError):
                return None
            match = next((e for e in entries if e.lower() == part.lower()), None)
            if match is None:
                return None
            current = f"{current}/{match}" if current else match
        return current

    def close(self) -> None:
        return None

    def __enter__(self) -> "DiscFileSystem":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Drive discovery
# --------------------------------------------------------------------------- #


class DriveProvider(ABC):
    """Enumerates usable media sources on this machine."""

    name: str = "provider"

    @abstractmethod
    def list_drives(self) -> list[Drive]: ...

    def rescan(self) -> list[Drive]:
        return self.list_drives()


# --------------------------------------------------------------------------- #
# Disc analysis
# --------------------------------------------------------------------------- #


class DiscAnalyzer(ABC):
    """Turns a drive/image/folder into a :class:`DiscAnalysis`."""

    name: str = "analyzer"

    @abstractmethod
    def can_handle(self, drive: Drive, fs: DiscFileSystem | None) -> bool:
        """Cheap check; must not do heavy I/O."""

    @abstractmethod
    def analyze(
        self,
        drive: Drive,
        fs: DiscFileSystem | None,
        *,
        progress: ProgressCallback = null_progress,
        cancelled: CancelCheck = never_cancelled,
    ) -> DiscAnalysis: ...


# --------------------------------------------------------------------------- #
# Ripping pipeline
# --------------------------------------------------------------------------- #


class StreamExtractor(ABC):
    """Copies the selected streams of a title out of the disc source.

    Implementations may write a temporary MPEG-PS/TS file (DVD cell copy) or
    simply hand the tool a playlist reference (Blu-ray).  The output is
    *always* a local path that the remuxer can open.
    """

    name: str = "extractor"

    @abstractmethod
    def can_handle(self, drive: Drive, title: Title) -> bool: ...

    @abstractmethod
    def extract(
        self,
        drive: Drive,
        title: Title,
        selection: TitleSelection,
        work_dir: str,
        *,
        progress: ProgressCallback = null_progress,
        cancelled: CancelCheck = never_cancelled,
    ) -> "ExtractionResult": ...


class ExtractionResult:
    """What :meth:`StreamExtractor.extract` produced."""

    __slots__ = ("paths", "streams", "chapter_times", "cleanup", "protocol",
                 "size_bytes")

    def __init__(
        self,
        paths: list[str],
        streams: list[StreamInfo],
        chapter_times: list[float] | None = None,
        cleanup: bool = True,
        protocol: str = "file",
        size_bytes: int | None = None,
    ) -> None:
        #: input argument(s) handed to the muxer, in order
        self.paths = paths
        #: the streams the extractor guarantees are present, in mux order
        self.streams = streams
        #: chapter start times (seconds) as recovered from the disc
        self.chapter_times = chapter_times or []
        #: whether ``paths`` are temporary files we own and must delete
        self.cleanup = cleanup
        #: ffmpeg input protocol hint (``file``, ``concat``, ``bluray``...)
        self.protocol = protocol
        self.size_bytes = size_bytes


class Remuxer(ABC):
    """Writes exactly the given streams into an MKV container, no re-encode."""

    name: str = "remuxer"

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """``(usable, reason_if_not)`` - never raises."""

    @abstractmethod
    def remux(
        self,
        extracted: "ExtractionResult",
        output_path: str,
        *,
        discard_original_subtitles: bool = False,
        progress: ProgressCallback = null_progress,
        cancelled: CancelCheck = never_cancelled,
    ) -> str: ...


class OutputValidator(ABC):
    """Re-opens a finished file and verifies it holds what was requested."""

    name: str = "validator"

    @abstractmethod
    def validate(
        self,
        path: str,
        *,
        expected: list[StreamInfo] | None = None,
        expected_chapters: int | None = None,
        expected_duration: float | None = None,
    ) -> ValidationReport: ...


class DiskSpaceChecker(ABC):
    """Free-space accounting so we fail *before* filling a disk."""

    @abstractmethod
    def free_bytes(self, path: str) -> int: ...

    @abstractmethod
    def required_bytes(self, title: Title, selection: TitleSelection) -> int: ...


class JobQueue(ABC):
    """Ordered, cancellable queue of :class:`RipJob`."""

    @abstractmethod
    def add(self, job: RipJob) -> None: ...

    @abstractmethod
    def cancel(self, job_id: str) -> None: ...

    @abstractmethod
    def jobs(self) -> list[RipJob]: ...


# --------------------------------------------------------------------------- #
# Progress reporting
# --------------------------------------------------------------------------- #


@runtime_checkable
class ProgressSink(Protocol):
    """Anything that can receive progress updates (GUI panel, log, CLI)."""

    def __call__(self, update: ProgressUpdate) -> None: ...


def iter_chunks(total: int, chunk: int) -> Iterator[tuple[int, int]]:
    """Yield ``(offset, length)`` pairs covering ``total`` in ``chunk`` steps."""
    if chunk <= 0:
        raise ValueError("chunk must be positive")
    offset = 0
    while offset < total:
        yield offset, min(chunk, total - offset)
        offset += chunk
