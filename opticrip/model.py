"""Disc/title/stream data model (CLONE_SPEC.md §6.7)."""

from __future__ import annotations

from dataclasses import dataclass, field


def fmt_duration(seconds: float) -> str:
    """``0:03:43`` - the shape used by TINFO attribute 9.

    The reference truncates (3:43.8 prints as 0:03:43), so this does too.
    """
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def fmt_size(num_bytes: int) -> str:
    """Human size.  The reference divides by 1024 but prints ``MB``."""
    if num_bytes <= 0:
        return ""
    value = float(num_bytes)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "bytes":
                return f"{int(value)} bytes"
            # VERIFIED: the reference truncates, it does not round.  A
            # 488.672 MB title prints "488.6 MB", not "488.7 MB".
            return f"{int(value * 10) / 10:.1f} {unit}"
        value /= 1024.0
    return ""


def fmt_capacity(num_bytes: int) -> str:
    """``6.83 Gb`` - disc capacity uses lower-case ``b`` in the reference."""
    if num_bytes <= 0:
        return ""
    gb = num_bytes / (1024 ** 3)
    if gb >= 1:
        # VERIFIED: the reference truncates (6.8359 Gb prints "6.83 Gb").
        return f"{int(gb * 100) / 100:.2f} Gb"
    return f"{int(num_bytes / (1024 ** 2))} MB"


def fmt_bitrate(bits_per_second: int) -> str:
    if bits_per_second <= 0:
        return ""
    if bits_per_second >= 1_000_000:
        return f"{bits_per_second / 1_000_000:.1f} Mb/s"
    return f"{bits_per_second / 1000:.0f} kb/s"


# Stream flag bits (documented apdefs.h values).
FLAG_COMMENTARY = 1
FLAG_ALT_COMMENTARY = 2
FLAG_VISUALLY_IMPAIRED = 4
FLAG_CORE = 256
FLAG_SECONDARY = 512
FLAG_HAS_CORE = 1024
FLAG_DERIVED = 2048
FLAG_FORCED = 4096
FLAG_PROFILE_SECONDARY = 16384

_FLAG_LABELS = (
    (FLAG_COMMENTARY, "Director's comments"),
    (FLAG_ALT_COMMENTARY, "Alternate director's comments"),
    (FLAG_SECONDARY, "Secondary low-bitrate audio"),
    (FLAG_VISUALLY_IMPAIRED, "For visually impaired"),
    (FLAG_CORE, "Core audio"),
    (FLAG_FORCED, "Forced subtitles"),
    (FLAG_PROFILE_SECONDARY, "Profile-defined secondary stream"),
)


@dataclass
class Segment:
    """One contiguous source range that makes up a title.

    Blu-ray playlists use ``start``/``duration`` in seconds.  DVD cells use
    ``byte_offset``/``byte_size`` instead: the IFO addresses cells in 2048-byte
    sectors within the VTS VOB set, which lets the engine copy only the cells
    belonging to a title instead of every VOB that shares the set.
    """

    path: str
    start: float = 0.0
    duration: float = 0.0
    byte_offset: int = 0
    byte_size: int = 0


@dataclass
class Stream:
    kind: str                      # "video" | "audio" | "subtitle"
    index: int                     # per-title ordinal (SINFO index)
    codec_id: str = ""
    codec: str = ""
    codec_long: str = ""
    language: str = "und"
    language_name: str = ""
    channels: int = 0
    channel_layout: str = ""
    width: int = 0
    height: int = 0
    aspect: str = ""
    fps: str = ""
    bitrate: int = 0
    sample_rate: int = 0
    flags: int = 0
    forced: bool = False
    default: bool = False
    selected: bool = True
    output_codec: str = ""
    conversion: str = ""
    comment: str = ""
    source_index: int | None = None
    pid: int = 0
    stream_id: str = ""
    weight: int = 0

    @property
    def kind_label(self) -> str:
        return {"video": "Video", "audio": "Audio", "subtitle": "Subtitles"}.get(
            self.kind, self.kind.title()
        )

    @property
    def flags_text(self) -> str:
        if self.forced:
            return "(forced only)"
        names = [label for bit, label in _FLAG_LABELS if self.flags & bit]
        return ", ".join(names)

    @property
    def display(self) -> str:
        bits = [self.kind_label, self.codec or self.codec_id]
        if self.kind == "audio" and self.channels:
            bits.append(f"{self.channels}ch")
        if self.kind == "video" and self.width and self.height:
            bits.append(f"{self.width}x{self.height}")
        if self.language and self.language != "und":
            bits.append(self.language)
        if self.flags_text:
            bits.append(self.flags_text)
        return "  ".join(bits)


@dataclass
class Title:
    index: int
    name: str = ""
    comment: str = ""
    duration: float = 0.0
    size_bytes: int = 0
    chapter_times: list[float] = field(default_factory=list)
    streams: list[Stream] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    original_id: str = ""
    selected: bool = True
    angle: int = 1
    source: str = ""               # dvd | bluray | mkv
    vts: int = 0
    playlist: str = ""
    output_name: str = ""
    #: TINFO attribute 26: the cell range that makes up the title, e.g. ``1-17``.
    segment_map: str = ""

    @property
    def chapter_count(self) -> int:
        return len(self.chapter_times)

    def video_streams(self) -> list[Stream]:
        return [s for s in self.streams if s.kind == "video"]

    def audio_streams(self) -> list[Stream]:
        return [s for s in self.streams if s.kind == "audio"]

    def subtitle_streams(self) -> list[Stream]:
        return [s for s in self.streams if s.kind == "subtitle"]

    def selected_streams(self) -> list[Stream]:
        return [s for s in self.streams if s.selected]

    @property
    def duration_text(self) -> str:
        return fmt_duration(self.duration)

    @property
    def size_text(self) -> str:
        return fmt_size(self.size_bytes)

    @property
    def tree_text(self) -> str:
        """TINFO attribute 30, e.g. ``1 chapter(s) , 101.8 MB (YB8)``."""
        bits = [f"{self.chapter_count} chapter(s)"]
        if self.size_text:
            bits.append(self.size_text)
        tail = f"({self.comment})" if self.comment else ""
        text = " , ".join(bits)
        return f"{text} {tail}".strip()


@dataclass
class Disc:
    kind: str = "Data"              # DVD | Blu-ray | MKV | Data
    label: str = ""
    volume_name: str = ""
    protection: str = "None"
    size_bytes: int = 0
    titles: list[Title] = field(default_factory=list)
    encrypted: bool = False
    source_path: str = ""
    source_spec: str = ""
    title_count: int = 0
    drive_index: int = 0
    profile: str = ""
    timestamp: str = ""
    #: UHD Blu-ray: an AACS 2.x MKB was detected.  libaacs only implements
    #: AACS 1.0, and bus encryption needs a licensed host certificate.
    uhd: bool = False
    aacs_generation: str = ""

    @property
    def size_text(self) -> str:
        return fmt_capacity(self.size_bytes)

    @property
    def selected_titles(self) -> list[Title]:
        return [t for t in self.titles if t.selected]
