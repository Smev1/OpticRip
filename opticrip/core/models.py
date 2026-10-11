"""Framework-agnostic domain models.

Nothing in this module touches the filesystem, subprocesses or Qt.  Every
other layer speaks in terms of these types, which is what lets the engine be
replaced piecewise (see ``opticrip.core.interfaces``).
"""

from __future__ import annotations

import dataclasses
import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class MediaType(str, enum.Enum):
    """What kind of disc (if any) is sitting in a drive."""

    EMPTY = "empty"
    DVD_VIDEO = "dvd-video"
    BLURAY = "bluray"
    UHD_BLURAY = "uhd-bluray"
    CD_AUDIO = "cd-audio"
    DATA = "data"
    UNKNOWN = "unknown"

    @property
    def is_video(self) -> bool:
        return self in (MediaType.DVD_VIDEO, MediaType.BLURAY, MediaType.UHD_BLURAY)

    @property
    def label(self) -> str:
        return {
            MediaType.EMPTY: "No disc",
            MediaType.DVD_VIDEO: "DVD-Video",
            MediaType.BLURAY: "Blu-ray",
            MediaType.UHD_BLURAY: "Ultra HD Blu-ray",
            MediaType.CD_AUDIO: "Audio CD",
            MediaType.DATA: "Data disc",
            MediaType.UNKNOWN: "Unknown media",
        }[self]


class StreamKind(str, enum.Enum):
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {"video": "Video", "audio": "Audio", "subtitle": "Subtitle",
                "other": "Other"}[self.value]


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.CANCELLED, JobStatus.DONE, JobStatus.FAILED,
                        JobStatus.SKIPPED)

    @property
    def label(self) -> str:
        return {
            JobStatus.QUEUED: "Queued",
            JobStatus.RUNNING: "Ripping",
            JobStatus.CANCELLING: "Cancelling\u2026",
            JobStatus.CANCELLED: "Cancelled",
            JobStatus.DONE: "Completed",
            JobStatus.FAILED: "Failed",
            JobStatus.SKIPPED: "Skipped",
        }[self.value]


class RipPhase(str, enum.Enum):
    QUEUED = "queued"
    ANALYSING = "analysing"
    EXTRACTING = "extracting"
    REMUXING = "remuxing"
    VALIDATING = "validating"
    FINISHED = "finished"

    @property
    def label(self) -> str:
        return {
            RipPhase.QUEUED: "Queued",
            RipPhase.ANALYSING: "Analysing disc",
            RipPhase.EXTRACTING: "Reading streams",
            RipPhase.REMUXING: "Muxing MKV",
            RipPhase.VALIDATING: "Validating output",
            RipPhase.FINISHED: "Finished",
        }[self.value]


# --------------------------------------------------------------------------- #
# Language handling
# --------------------------------------------------------------------------- #

# ISO 639-1 (2 letter, used by DVD) -> ISO 639-2/B (3 letter, used by Blu-ray)
_ISO1_TO_ISO2B: dict[str, str] = {
    "ar": "ara", "bg": "bul", "ca": "cat", "cs": "cze", "cy": "wel", "da": "dan",
    "de": "ger", "el": "gre", "en": "eng", "es": "spa", "et": "est", "eu": "baq",
    "fa": "per", "fi": "fin", "fr": "fre", "ga": "gle", "gl": "glg", "he": "heb",
    "hi": "hin", "hr": "hrv", "hu": "hun", "hy": "arm", "id": "ind", "is": "ice",
    "it": "ita", "ja": "jpn", "ka": "geo", "kk": "kaz", "ko": "kor", "la": "lat",
    "lt": "lit", "lv": "lav", "mk": "mac", "ms": "may", "nl": "dut", "no": "nor",
    "pl": "pol", "pt": "por", "ro": "rum", "ru": "rus", "sk": "slo", "sl": "slv",
    "sq": "alb", "sr": "srp", "sv": "swe", "th": "tha", "tr": "tur", "uk": "ukr",
    "ur": "urd", "vi": "vie", "zh": "chi",
}

_LANGUAGE_NAMES: dict[str, str] = {
    "ara": "Arabic", "arm": "Armenian", "baq": "Basque", "bul": "Bulgarian",
    "cat": "Catalan", "chi": "Chinese", "cze": "Czech", "dan": "Danish",
    "dut": "Dutch", "eng": "English", "est": "Estonian", "fin": "Finnish",
    "fre": "French", "geo": "Georgian", "ger": "German", "gle": "Irish",
    "glg": "Galician", "gre": "Greek", "heb": "Hebrew", "hin": "Hindi",
    "hrv": "Croatian", "hun": "Hungarian", "ice": "Icelandic", "ind": "Indonesian",
    "ita": "Italian", "jpn": "Japanese", "kaz": "Kazakh", "kor": "Korean",
    "lat": "Latin", "lav": "Latvian", "lit": "Lithuanian", "mac": "Macedonian",
    "may": "Malay", "nor": "Norwegian", "per": "Persian", "pol": "Polish",
    "por": "Portuguese", "rum": "Romanian", "rus": "Russian", "slo": "Slovak",
    "slv": "Slovenian", "spa": "Spanish", "srp": "Serbian", "swe": "Swedish",
    "tha": "Thai", "tur": "Turkish", "ukr": "Ukrainian", "urd": "Urdu",
    "vie": "Vietnamese", "wel": "Welsh",
}

#: DVD stores audio/subpicture languages as a 2-char ISO 639-1 code.
_DVD_RESERVED_LANG = {"xx": None, "  ": None, "": None}


def normalise_language(code: str | None) -> str | None:
    """Return a canonical ISO 639-2/B language code, or ``None``.

    Accepts 2-letter ISO 639-1 (DVD) and 3-letter ISO 639-2 (Blu-ray) codes,
    plus the DVD "reserved"/unknown markers.  Unknown codes are returned
    lower-cased so the caller can still display *something*.
    """
    if not code:
        return None
    c = code.strip().lower()
    if c in _DVD_RESERVED_LANG or set(c) <= {"\x00"}:
        return None
    if len(c) == 2:
        return _ISO1_TO_ISO2B.get(c, c)
    if len(c) >= 3:
        return c[:3]
    return None


def language_name(code: str | None) -> str | None:
    """Human readable name for an ISO 639 code (either length)."""
    canonical = normalise_language(code)
    if canonical is None:
        return None
    return _LANGUAGE_NAMES.get(canonical, canonical.upper())


# --------------------------------------------------------------------------- #
# Streams / chapters / titles
# --------------------------------------------------------------------------- #


@dataclass
class StreamInfo:
    """A single elementary stream inside a title."""

    index: int
    kind: StreamKind
    codec: str
    codec_long: str = ""
    language: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    sample_rate: int | None = None
    bitrate: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    forced: bool = False
    default: bool = False
    subtitle_format: str | None = None
    source_id: str | None = None
    #: free-form extras from the parser/prober (never shown raw to the user
    #: except in the diagnostics panel)
    extras: dict[str, Any] = field(default_factory=dict)

    # -- convenience ------------------------------------------------------- #
    @property
    def language_label(self) -> str:
        if self.language_name:
            return self.language_name
        return "Unknown"

    @property
    def language_name(self) -> str | None:
        return language_name(self.language)

    @property
    def codec_label(self) -> str:
        return self.codec_long or self.codec

    @property
    def resolution_label(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "\u2014"

    @property
    def channels_label(self) -> str:
        if self.kind is not StreamKind.AUDIO:
            return "\u2014"
        if self.channel_layout:
            pretty = {"mono": "1.0", "stereo": "2.0", "2.1": "2.1",
                      "quad": "4.0", "4.0": "4.0", "5.0": "5.0", "5.1": "5.1",
                      "6.1": "6.1", "7.1": "7.1"}
            if self.channel_layout in pretty:
                return pretty[self.channel_layout]
        return f"{self.channels} ch" if self.channels else "\u2014"

    def summary(self) -> str:
        """One-line human description used in tables and the log."""
        bits = [self.kind.label, self.codec_label]
        if self.kind is StreamKind.VIDEO:
            bits.append(self.resolution_label)
            if self.fps:
                bits.append(f"{self.fps:.3f} fps".rstrip("0").rstrip("."))
        elif self.kind is StreamKind.AUDIO:
            bits.append(self.language_label)
            bits.append(self.channels_label)
            if self.sample_rate:
                bits.append(f"{self.sample_rate / 1000:.1f} kHz")
        else:
            bits.append(self.language_label)
            if self.forced:
                bits.append("forced")
        return " \u00b7 ".join(b for b in bits if b and b != "\u2014")


@dataclass
class Chapter:
    """A chapter marker (start time is authoritative; duration is derived)."""

    index: int
    start_seconds: float
    duration_seconds: float = 0.0

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


@dataclass
class Title:
    """A playable title: one DVD PGC or one Blu-ray playlist."""

    id: str
    index: int
    name: str = ""
    duration_seconds: float = 0.0
    chapters: list[Chapter] = field(default_factory=list)
    streams: list[StreamInfo] = field(default_factory=list)
    size_bytes: int | None = None
    angles: int = 1
    is_main_feature: bool = False
    notes: list[str] = field(default_factory=list)
    #: opaque per-format reference used by the extractor
    #: e.g. {"kind": "dvd-pgc", "vts": 1, "pgc": 1}
    source_ref: dict[str, Any] = field(default_factory=dict)

    # -- convenience ------------------------------------------------------- #
    @property
    def display_name(self) -> str:
        return self.name or f"Title {self.index}"

    @property
    def duration_label(self) -> str:
        return format_duration(self.duration_seconds)

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)

    def streams_of(self, kind: StreamKind) -> list[StreamInfo]:
        return [s for s in self.streams if s.kind is kind]

    def add(self, chapter: Chapter) -> None:
        self.chapters.append(chapter)

    def stream_label_map(self) -> dict[int, str]:
        return {s.index: s.summary() for s in self.streams}


def format_duration(seconds: float | int | None) -> str:
    """``1:02:03`` / ``12:34`` / ``0:00``."""
    if seconds is None:
        return "\u2014"
    total = int(round(float(seconds)))
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_bytes(num: int | None) -> str:
    if num is None:
        return "\u2014"
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover - unreachable


# --------------------------------------------------------------------------- #
# Disc-level information
# --------------------------------------------------------------------------- #


@dataclass
class DiscInfo:
    label: str = ""
    media_type: MediaType = MediaType.UNKNOWN
    capacity_bytes: int | None = None
    format_label: str = ""
    #: number of titlesets (DVD) / number of playlists (Blu-ray)
    feature_count: int = 0
    #: protection suspicion reported by the identifier, never used to bypass
    protection: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscAnalysis:
    """Result of analysing one disc."""

    disc: DiscInfo
    titles: list[Title] = field(default_factory=list)
    #: non-fatal problems encountered while parsing (shown in the log panel)
    warnings: list[str] = field(default_factory=list)
    #: which analyzer produced this (e.g. "dvd-ifo", "ffprobe")
    analyzer: str = ""
    #: message shown for the "empty" state, if there are no titles
    empty_reason: str | None = None

    def by_id(self, title_id: str) -> Title | None:
        for t in self.titles:
            if t.id == title_id:
                return t
        return None

    def main_feature(self) -> Title | None:
        for t in self.titles:
            if t.is_main_feature:
                return t
        return None

    def total_duration(self) -> float:
        return sum(t.duration_seconds for t in self.titles)


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


@dataclass
class TitleSelection:
    """Which parts of a title the user wants written to MKV."""

    title_id: str
    #: at most one video stream; ``None`` means the first/only video stream
    video_index: int | None = None
    audio_indices: tuple[int, ...] = ()
    subtitle_indices: tuple[int, ...] = ()
    include_chapters: bool = True
    #: 1-based inclusive chapter range, ``None`` = all chapters
    chapter_start: int | None = None
    chapter_end: int | None = None
    #: pull in subtitle streams flagged "forced" even if not explicitly picked
    include_forced_subtitles: bool = False
    #: explicit output file name (without directory); ``None`` = use template
    output_name: str | None = None

    def resolved_chapter_range(self, chapter_count: int) -> tuple[int, int]:
        """Return a validated, 1-based inclusive ``(first, last)`` chapter range."""
        if chapter_count <= 0:
            return (0, 0)
        first = 1 if self.chapter_start is None else int(self.chapter_start)
        last = chapter_count if self.chapter_end is None else int(self.chapter_end)
        first = max(1, min(first, chapter_count))
        last = max(first, min(last, chapter_count))
        return (first, last)

    @classmethod
    def all_streams(cls, title: Title) -> "TitleSelection":
        return cls(
            title_id=title.id,
            video_index=next((s.index for s in title.streams_of(StreamKind.VIDEO)), None),
            audio_indices=tuple(s.index for s in title.streams_of(StreamKind.AUDIO)),
            subtitle_indices=tuple(s.index for s in title.streams_of(StreamKind.SUBTITLE)),
        )


def validate_selection(title: Title, selection: TitleSelection) -> None:
    """Raise :class:`ValueError` if a selection references non-existent streams.

    Guarantees we never silently drop a requested track: any index the user
    asked for either exists (and is of the right kind) or this raises.
    """
    if selection.title_id != title.id:
        raise ValueError(
            f"selection targets title {selection.title_id!r} but got {title.id!r}")

    known = {s.index: s for s in title.streams}
    if selection.video_index is not None:
        s = known.get(selection.video_index)
        if s is None:
            raise ValueError(f"video stream {selection.video_index} does not exist")
        if s.kind is not StreamKind.VIDEO:
            raise ValueError(f"stream {selection.video_index} is not a video stream")

    for idx in selection.audio_indices:
        s = known.get(idx)
        if s is None:
            raise ValueError(f"audio stream {idx} does not exist")
        if s.kind is not StreamKind.AUDIO:
            raise ValueError(f"stream {idx} is not an audio stream")

    for idx in selection.subtitle_indices:
        s = known.get(idx)
        if s is None:
            raise ValueError(f"subtitle stream {idx} does not exist")
        if s.kind is not StreamKind.SUBTITLE:
            raise ValueError(f"stream {idx} is not a subtitle stream")

    if selection.chapter_start is not None and selection.chapter_end is not None:
        if selection.chapter_start > selection.chapter_end:
            raise ValueError("chapter range start is after end")


def selection_to_streams(title: Title, selection: TitleSelection) -> list[StreamInfo]:
    """Ordered streams a selection maps to (video, then audio, then subtitles)."""
    chosen: list[StreamInfo] = []
    if selection.video_index is not None:
        chosen.extend(s for s in title.streams
                      if s.index == selection.video_index and s.kind is StreamKind.VIDEO)
    else:
        chosen.extend(title.streams_of(StreamKind.VIDEO)[:1])

    for idx in selection.audio_indices:
        chosen.extend(s for s in title.streams
                      if s.index == idx and s.kind is StreamKind.AUDIO)
    for idx in selection.subtitle_indices:
        chosen.extend(s for s in title.streams
                      if s.index == idx and s.kind is StreamKind.SUBTITLE)

    if selection.include_forced_subtitles:
        for s in title.streams_of(StreamKind.SUBTITLE):
            if s.forced and s not in chosen:
                chosen.append(s)
    return chosen


def guess_main_feature(titles: Sequence[Title], *,
                       min_seconds: float = 1200.0) -> Title | None:
    """Heuristic "main feature" pick: longest title above ``min_seconds``.

    A title is only accepted if it is either the sole candidate or at least
    2x longer than the runner-up, which avoids flagging a disc of same-length
    episodes.  Titles shorter than ``min_seconds`` (20 min default) are
    ignored so menus/ads never win.
    """
    candidates = [t for t in titles if t.duration_seconds >= min_seconds]
    if not candidates:
        return None
    candidates.sort(key=lambda t: t.duration_seconds, reverse=True)
    return candidates[0]


def mark_main_feature(titles: Sequence[Title], *,
                      min_seconds: float = 1200.0) -> Title | None:
    """Flag the longest plausible title and annotate the ambiguity.

    The heuristic never hides the other titles: the GUI still shows every
    title and the user can override the pre-selection.
    """
    for t in titles:
        t.is_main_feature = False
    best = guess_main_feature(titles, min_seconds=min_seconds)
    if best is None:
        return None
    best.is_main_feature = True
    rivals = [t for t in titles
              if t is not best and t.duration_seconds > 0
              and t.duration_seconds >= 0.75 * best.duration_seconds]
    if rivals:
        note = (f"main feature guessed by duration, but {len(rivals)} other "
                f"title(s) are similarly long - please confirm")
        if note not in best.notes:
            best.notes.append(note)
    return best


# --------------------------------------------------------------------------- #
# Drives
# --------------------------------------------------------------------------- #


class DriveKind(str, enum.Enum):
    """How the media is exposed to us."""

    RAW = "raw"        # a block device such as /dev/sr0
    IMAGE = "image"    # an ISO/UDF image file
    FOLDER = "folder"  # an already-mounted VIDEO_TS / BDMV directory

    @property
    def label(self) -> str:
        return {"raw": "Optical drive", "image": "Disc image",
                "folder": "Disc folder"}[self.value]


class DriveState(str, enum.Enum):
    NO_DISC = "no-disc"
    READY = "ready"
    BUSY = "busy"
    INACCESSIBLE = "inaccessible"

    @property
    def label(self) -> str:
        return {"no-disc": "No disc", "ready": "Ready", "busy": "Busy",
                "inaccessible": "Inaccessible"}[self.value]


@dataclass
class Drive:
    """One usable media source, real or virtual.

    ``path`` is whatever the extractor will open: ``/dev/sr0`` for a raw
    drive, an ``.iso`` for an image, or a directory holding ``VIDEO_TS`` /
    ``BDMV`` for a mounted/folder source.  Every backend therefore works
    identically against hardware and against fixtures.
    """

    path: str
    kind: DriveKind = DriveKind.RAW
    #: stable id used by the GUI/queue (path is fine, but keep it explicit)
    id: str = ""
    vendor: str = ""
    model: str = ""
    serial: str = ""
    firmware: str = ""
    #: "sata" / "usb" / "virtual"
    bus: str = ""
    state: DriveState = DriveState.NO_DISC
    media_type: MediaType = MediaType.UNKNOWN
    volume_label: str = ""
    sector_size: int = 2048
    size_bytes: int | None = None
    mount_points: list[str] = field(default_factory=list)
    read_only: bool = True
    #: plain-language explanation for non-READY states
    status_detail: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = self.path

    # -- display helpers --------------------------------------------------- #
    @property
    def display_name(self) -> str:
        parts = [b for b in (self.vendor, self.model) if b]
        name = " ".join(parts).strip()
        return name or self.path

    @property
    def media_label(self) -> str:
        if self.state is DriveState.NO_DISC:
            return "No disc"
        if self.state is DriveState.INACCESSIBLE:
            return "Inaccessible"
        if self.media_type is MediaType.UNKNOWN and self.volume_label:
            return self.volume_label
        if self.volume_label:
            return f"{self.media_type.label} \u00b7 {self.volume_label}"
        return self.media_type.label

    @property
    def is_ready(self) -> bool:
        return self.state is DriveState.READY and self.media_type is not MediaType.EMPTY
# --------------------------------------------------------------------------- #
# Jobs / progress / results
# --------------------------------------------------------------------------- #


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class RipJob:
    """One queued rip of one title from one drive."""

    id: str
    drive_device: str
    title: Title
    selection: TitleSelection
    output_path: str            # full destination file path (str for JSON-ability)
    disc_label: str = ""
    media_type: MediaType = MediaType.UNKNOWN
    drive_kind: DriveKind = DriveKind.RAW
    status: JobStatus = JobStatus.QUEUED
    phase: RipPhase = RipPhase.QUEUED
    error: str | None = None
    error_title: str | None = None
    error_hint: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    attempts: int = 0
    result: "RipResult | None" = None

    @property
    def display_name(self) -> str:
        return self.title.display_name

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    # -- accessors the engine needs (jobs keep plain strings for JSON) ----- #
    def drive_path(self) -> str:
        return self.drive_device

    def drive_as_drive(self) -> "Drive":
        """Reconstruct a minimal :class:`Drive` view for the extractors."""
        return Drive(
            path=self.drive_device,
            kind=self.drive_kind,
            media_type=self.media_type,
            state=DriveState.READY,
        )


@dataclass
class ProgressUpdate:
    job_id: str
    phase: RipPhase
    #: 0.0 .. 1.0, or ``None`` when indeterminate
    fraction: float | None = None
    message: str = ""
    elapsed: float = 0.0
    eta: float | None = None
    speed: float | None = None           # bytes/s or x-realtime
    processed_bytes: int | None = None
    total_bytes: int | None = None

    @property
    def percent(self) -> int | None:
        if self.fraction is None:
            return None
        return max(0, min(100, int(round(self.fraction * 100))))


@dataclass
class ValidationReport:
    ok: bool = False
    container: str = ""
    duration_seconds: float = 0.0
    size_bytes: int = 0
    stream_summaries: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_issue(self, text: str) -> None:
        self.issues.append(text)
        self.ok = False


@dataclass
class RipResult:
    job_id: str
    ok: bool
    output_path: str
    duration_seconds: float = 0.0
    size_bytes: int = 0
    validation: ValidationReport | None = None
    warnings: list[str] = field(default_factory=list)


def asdict_safe(obj: Any) -> Any:
    """``dataclasses.asdict`` that survives enums, paths and nested models."""
    def _convert(value: Any) -> Any:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {k: _convert(v) for k, v in dataclasses.asdict(value).items()}
        if isinstance(value, enum.Enum):
            return value.value
        if isinstance(value, Mapping):
            return {str(k): _convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_convert(v) for v in value]
        if hasattr(value, "__fspath__"):
            return str(value)
        return value

    return _convert(obj)


