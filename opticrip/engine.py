"""Headless engine.

Every disc operation lives here; the CLI and the GUI are thin shells
(R-ARCH-01).  Events go to an :class:`~opticrip.robot.EventWriter`; progress
values are delivered separately because the reference console ``mkv`` command
does **not** emit ``PRGV`` lines (R-ROB-08).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import BANNER, PRODUCT, __version__, messages, registration
from . import cc as cclib
from .aacs import mkb_generation, parse_mkb
from . import dvdcss as dvdcsslib
from .backup import BackupError, BackupWriter
from .bddecrypt import BlurayDecryptError, BlurayDecryptor
from .bdmv import BdmvError, parse_disc as parse_bdmv
from .discfs import CssFS, DiscFS, FolderFS, IsoFS, detect_kind
from .dvdcss import DvdcssError
from .dvd import DvdError, has_scrambled_sectors, parse_disc as parse_dvd
from .ffmpeg import (FFmpegError, can_decode_video, ffprobe_json, probe_title,
                     remux, streams_from_probe)
from .iso9660 import IsoError, UnsupportedImage, sniff
from .model import Disc, Stream, Title, fmt_bitrate, fmt_capacity, fmt_duration, fmt_size
from .naming import expand as expand_name, unique_path
from .profiles import Profile, ProfileError, builtin_default, load_named
from .robot import EventWriter, NullWriter
from .settings import Settings
from . import drives as drivelib
from . import selection as selection_rules

CINFO_TYPE_LABEL = {"DVD": "DVD disc", "Blu-ray": "Blu-ray disc",
                    "HD-DVD": "HD-DVD disc", "MKV": "MKV file", "Data": "Data"}

#: SINFO 6/7 spellings as emitted by the reference for DVD tracks.  The model
#: keeps the canonical codec names because profiles match on those.
_CODEC_LABELS = {
    "MPEG-2": ("Mpeg2", "Mpeg2"),
    "MPEG-1": ("Mpeg1", "Mpeg1"),
    "AC-3": ("DD", "Dolby Digital"),
    "DTS": ("DTS", "DTS"),
    "LPCM": ("LPCM", "LPCM"),
    "DVD subtitles": ("", "Dvd Subtitles"),
}

#: Fraction of the per-title bar used while source data is staged (DVD cells
#: copied to a seekable VOB, or AACS/BD+ clips decrypted for ffmpeg).
#: Staging and remuxing are two real phases of one title save; keeping the
#: first half for staging makes the bar move immediately and avoids a
#: backwards jump when ffmpeg starts.
_STAGING_WEIGHT = 0.5


def _codec_labels(stream: Stream) -> tuple[str, str, bool]:
    """Return ``(short, long, lossy)`` for SINFO attributes 6, 7 and 42."""
    lossy = stream.codec_id == "S_CC608/DVD"
    if lossy:
        return "CC", "Closed Captions", True
    short, long = _CODEC_LABELS.get(stream.codec, (stream.codec, stream.codec_long or stream.codec))
    return short, long, False


def _stream_summary(stream: Stream, short_name: str) -> str:
    """SINFO attribute 30, e.g. ``DD Stereo English`` or `` English``."""
    if stream.kind == "video":
        return short_name
    if stream.kind == "audio":
        bits = [short_name, stream.channel_layout, stream.language_name]
        return " ".join(bit for bit in bits if bit)
    if stream.codec_id == "S_CC608/DVD":
        return f"CC→Text {stream.language_name} ( Lossy conversion )".replace("  ", " ")
    return f" {stream.language_name}" if stream.language_name else ""


@dataclass
class OpenResult:
    disc: Disc | None
    code: int
    fs: DiscFS | None = None
    spec: str = ""
    path: str = ""


class Engine:
    def __init__(self, writer: EventWriter, settings: Settings | None = None,
                 profile: Profile | None = None) -> None:
        self.writer = writer
        self.settings = settings or Settings()
        self.profile = profile or builtin_default()
        self.disc: Disc | None = None
        self.fs: DiscFS | None = None
        self.spec = ""
        self.root = ""
        self.on_progress: Callable[[int, int], None] | None = None
        self.event_sink: Callable[[dict], None] | None = None
        self.cancelled = False
        self.decryptor: BlurayDecryptor | None = None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def message(self, ident: int, *params) -> None:
        self.writer.message(ident, *params)
        if self.event_sink is not None:
            self.event_sink({
                "event": "message", "id": ident,
                "params": [str(p) for p in params],
                "text": messages.render(ident, *params),
                "flags": messages.flags(ident),
            })

    def emit_progress(self, current: int, total: int, **fields) -> None:
        if self.on_progress is not None:
            self.on_progress(current, total)
        if self.event_sink is not None:
            event = {"event": "progress", "current": current, "total": total}
            event.update(fields)
            self.event_sink(event)

    def startup(self, emit_drives: bool = True) -> None:
        self.message(1005, BANNER)
        self.settings.bump_launch_counter()
        if emit_drives:
            self.emit_drives()

    def emit_drives(self) -> list[drivelib.DriveInfo]:
        found = drivelib.enumerate_drives()
        for index in range(drivelib.MAX_SLOTS):
            drive = found[index] if index < len(found) else None
            if drive is None:
                self.writer.drive(index, 256, 999, 0, "", "", "")
                if self.event_sink is not None:
                    self.event_sink({"event": "drive", "index": index, "present": False,
                                     "name": "", "label": "", "device": ""})
            else:
                self.writer.drive(index, drive.state, 999, drive.slot_visible,
                                  drive.name, drive.label, drive.device)
                if self.event_sink is not None:
                    self.event_sink({
                        "event": "drive", "index": index, "present": True,
                        "name": drive.name, "label": drive.label,
                        "device": drive.device, "kind": drive.media_kind,
                        "capacity": drive.capacity,
                    })
        if not found:
            self.message(5042)
        return found

    # ------------------------------------------------------------------
    # source resolution
    # ------------------------------------------------------------------
    @staticmethod
    def split_spec(spec: str) -> tuple[str, str]:
        prefix, sep, rest = spec.partition(":")
        if sep and prefix.lower() in ("iso", "file", "disc", "dev"):
            return prefix.lower(), rest
        return "", spec

    def close_source(self) -> None:
        if self.decryptor is not None:
            self.decryptor.close()
            self.decryptor = None
        if self.fs is not None:
            try:
                self.fs.close()
            except OSError:
                pass
        self.fs = None
        self.disc = None

    def open_source(self, spec: str, min_length: int | None = None,
                    emit_info: bool = False, allow_mkv: bool = False) -> OpenResult:
        self.cancelled = False
        self.close_source()
        if min_length is None:
            min_length = self.settings.get_int("app_MinTitleLength", 120)
        prefix, rest = self.split_spec(spec)
        self.spec = spec
        if prefix == "iso":
            return self._open_iso(rest, min_length, emit_info)
        if prefix == "file":
            return self._open_folder(rest, min_length, emit_info, explicit=True)
        if prefix == "disc":
            return self._open_disc(rest, min_length, emit_info)
        if prefix == "dev":
            return self._open_device(rest, min_length, emit_info)
        path = Path(spec)
        if path.is_dir():
            return self._open_folder(spec, min_length, emit_info, explicit=False)
        if allow_mkv and path.suffix.lower() == ".mkv":
            return self._open_mkv(spec)
        if path.suffix.lower() == ".iso":
            return self._open_iso(spec, min_length, emit_info)
        self.message(5006, spec)
        self.message(5010)
        self.writer.tcount(0)
        return OpenResult(None, 0, None, spec)

    def _uri(self, path: str) -> str:
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate.as_uri()
        return "file://" + path.replace("\\", "/")

    def _open_iso(self, path: str, min_length: int, emit_info: bool) -> OpenResult:
        image = Path(path)
        if not image.exists():
            self.message(5006, path)
            self.message(5010)
            self.writer.tcount(0)
            return OpenResult(None, 0, None, self.spec)
        if image.stat().st_size == 0:
            self.message(2003, "Internal error - Input parameter is incorrect (489)",
                         path, image.stat().st_size)
            self.message(5010)
            self.writer.tcount(0)
            return OpenResult(None, 0, None, self.spec)
        kind_of_fs = sniff(image)
        if kind_of_fs not in ("iso9660", "udf"):
            # A zero-filled image fails as a read error at EOF, exactly like
            # the reference capture (MSG 2003 + offset == file size).
            try:
                head = image.read_bytes()[: 1 << 20]
            except OSError:
                head = b""
            if head and not any(head):
                self.message(2003, "Internal error - Input parameter is incorrect (489)",
                             path, image.stat().st_size)
            else:
                self.message(9024)
            self.message(5010)
            self.writer.tcount(0)
            return OpenResult(None, 0, None, self.spec)
        try:
            fs = IsoFS(image)
        except (UnsupportedImage, IsoError) as exc:
            self.message(9024)
            self.message(5010)
            self.writer.tcount(0)
            return OpenResult(None, 0, None, self.spec, str(exc))
        self.message(3006, self._uri(path))
        label = Path(path).stem
        # libdvdcss can open an image on POSIX, but its Windows backend only
        # accepts a drive letter.  CSS images therefore need the disc drive.
        css_target = None if sys.platform == "win32" else path
        return self._build(fs, label, min_length, emit_info, self.spec,
                           css_target=css_target, iso_image=True)

    def _open_folder(self, path: str, min_length: int, emit_info: bool,
                     explicit: bool) -> OpenResult:
        folder = Path(path)
        if not folder.is_dir():
            if folder.suffix.lower() == ".mkv":
                self.message(3006, self._uri(path))
                return OpenResult(None, 127, None, self.spec)
            return OpenResult(None, 10, None, self.spec)
        root = folder
        if folder.name.upper() == "VIDEO_TS":
            root = folder.parent
        fs = FolderFS(root)
        kind = detect_kind(fs)
        if kind == "Data":
            # Verified behaviour: a folder with no disc files exits 10 with no
            # message (CLONE_SPEC §12.3).
            return OpenResult(None, 10, None, self.spec)
        self.message(3006, self._uri(str(root)))
        return self._build(fs, root.name, min_length, emit_info, self.spec)

    def _slots(self) -> list:
        return drivelib.drive_slots()

    def _open_disc(self, index_text: str, min_length: int,
                   emit_info: bool) -> OpenResult:
        try:
            index = int(index_text)
        except ValueError:
            self.message(5010)
            self.writer.tcount(0)
            return OpenResult(None, 0, None, self.spec)
        slots = self._slots()
        drive = slots[index] if 0 <= index < len(slots) else None
        if drive is None or not drive.present:
            self.message(5010)
            self.writer.tcount(0)
            return OpenResult(None, 0, None, self.spec)
        self.message(3007)
        self.message(1011, "1.0")
        fs = FolderFS(drive.path)
        return self._build(fs, drive.label or f"Disc {index}", min_length,
                           emit_info, self.spec, drive_index=index,
                           css_target=drive.device)

    def _open_device(self, device: str, min_length: int, emit_info: bool) -> OpenResult:
        wanted = device.upper().rstrip(":\\")
        for index, drive in enumerate(drivelib.enumerate_drives()):
            if drive.device.upper().rstrip(":\\") == wanted:
                return self._open_disc(str(index), min_length, emit_info)
        self.message(2024, device)
        self.message(5010)
        self.writer.tcount(0)
        return OpenResult(None, 0, None, self.spec)

    def _open_mkv(self, path: str) -> OpenResult:
        self.message(9023, path)
        title = probe_title(path)
        disc = Disc(kind="MKV", label=Path(path).stem, volume_name=Path(path).stem,
                    source_path=path, source_spec=self.spec,
                    size_bytes=title.size_bytes, titles=[title], title_count=1)
        title.output_name = expand_name(
            self.settings.get_str("app_DefaultOutputFileName"), title, disc)
        selection_rules.apply(title, self.settings.get_str("app_DefaultSelectionString"),
                              self.settings.get_str("app_PreferredLanguage"))
        self.disc = disc
        self.fs = None
        self.root = path
        self._emit_info(disc)
        return OpenResult(disc, 0, None, self.spec, path)

    @staticmethod
    def _first_vob(fs: DiscFS) -> str | None:
        try:
            entries = fs.listdir("VIDEO_TS")
        except OSError:
            return None
        for entry in entries:
            upper = entry.upper()
            if (upper.startswith("VTS_") and upper.endswith(".VOB")
                    and not upper.endswith("_0.VOB")):
                return f"VIDEO_TS/{entry}"
        return None

    def _looks_scrambled(self, fs: DiscFS) -> bool:
        """True when the first title VOB has CSS scrambling-control bits.

        A sequence header is not a reliable marker: it can sit in the clear
        part of a scrambled sector.  Sector flags are checked first, with the
        sequence-header/decode test only as a fallback for odd sources.
        """

        path = self._first_vob(fs)
        if path is None:
            return False
        try:
            head = fs.read(path, 0, 128 * 1024)
        except OSError:
            head = b""
        if head:
            if has_scrambled_sectors(head):
                return True
            return b"\x00\x00\x01\xb3" not in head
        local = fs.local(path)
        if local:
            decodable = can_decode_video(local)
            return decodable is False
        return False

    def _try_css(self, target: str) -> CssFS | None:
        explicit = self.settings.get_str("app_DvdcssPath").strip()
        if not explicit:
            explicit = os.environ.get("OPTICRIP_DVDCSS", "").strip()
        try:
            library, used = dvdcsslib.resolve(explicit or None,
                                              self.settings.data_dir)
        except DvdcssError as exc:
            self.message(9120, str(exc))
            return None
        try:
            css = CssFS(target, library)
        except (DvdcssError, IsoError, OSError) as exc:
            self.message(9122, target, str(exc))
            return None
        self.message(9121, str(used))
        return css

    def _aacs_mkb(self, fs: DiscFS) -> tuple[int, int]:
        """Read the disc's MKB type/version, or ``(0, 0)`` when absent."""
        for root in ("AACS", "BDMV/AACS", "BDAV/AACS"):
            for name in ("MKBROM.AACS", "DUPLICATE/MKBROM.AACS"):
                path = f"{root}/{name}"
                try:
                    if not fs.exists(path):
                        continue
                    blob = fs.read(path, 0, 1 << 20)
                except OSError:
                    continue
                found = parse_mkb(blob)
                if found != (0, 0):
                    return found
        return 0, 0

    def _bluray_decryptor(self) -> BlurayDecryptor | None:
        """Prepare AACS/BD+ decoding for the open disc, once."""
        if self.decryptor is not None:
            return self.decryptor
        if self.fs is None or self.disc is None:
            return None
        decryptor = BlurayDecryptor(self.fs, self.settings, self.message)
        try:
            decryptor.prepare(num_titles=max(1, self.disc.title_count))
        except (BlurayDecryptError, OSError) as exc:
            decryptor.close()
            self.message(9130, str(exc))
            return None
        self.decryptor = decryptor
        return decryptor

    def _build(self, fs: DiscFS, label: str, min_length: int, emit_info: bool,
               spec: str, drive_index: int = 0, css_target: str | None = None,
               iso_image: bool = False) -> OpenResult:
        kind = detect_kind(fs)
        css_active = False
        if kind == "DVD" and css_target is not None and not isinstance(fs, CssFS):
            if self._looks_scrambled(fs):
                css = self._try_css(css_target)
                if css is not None:
                    fs.close()
                    fs = css
                    css_active = True
        try:
            if kind == "DVD":
                disc = parse_dvd(fs, min_length, self.message,
                                 skip_vob_reads=css_active)
            elif kind == "Blu-ray":
                disc = parse_bdmv(fs, min_length, self.message)
                if not any(fs.exists(path) for path in
                           ("AACS", "BDMV/AACS", "BDAV/AACS")):
                    self.message(3305)
            else:
                self.message(9024)
                self.message(5010)
                self.writer.tcount(0)
                return OpenResult(None, 0, fs, spec)
        except (DvdError, BdmvError, IsoError, OSError) as exc:
            if kind == "DVD":
                self.message(9025)
            else:
                self.message(9024)
            self.message(5010)
            self.writer.tcount(0)
            if css_active:
                fs.close()
                fs = None
            return OpenResult(None, 0, fs, spec, str(exc))
        if css_active:
            # The VOBs are now readable, but the disc is still CSS-protected.
            # Keeping the flag shows the correct protection label and prevents
            # a later fallback to a raw (still-scrambled) source.
            disc.encrypted = True
        if (kind == "DVD" and disc.encrypted and iso_image
                and sys.platform == "win32" and not css_active):
            self.message(9119)
        if kind == "DVD" and not disc.encrypted:
            # A scrambled VOB still has clear pack headers, so decode one frame
            # to decide whether the payload is readable.
            local = None
            if disc.titles and disc.titles[0].segments and fs.local is not None:
                try:
                    local = fs.local(disc.titles[0].segments[0].path)
                except OSError:
                    local = None
            if local:
                decodable = can_decode_video(local)
                if decodable is False:
                    disc.encrypted = True
        disc.label = label or "UNNAMED DISC"
        disc.volume_name = label or disc.volume_name
        disc.source_spec = spec
        disc.source_path = self.root = (fs.root if hasattr(fs, "root") else "")
        disc.drive_index = drive_index
        bdplus_metadata = (kind == "Blu-ray"
                           and (fs.exists("BDSVM/00000.svm")
                                or fs.exists("BDMV/BDSVM/00000.svm")
                                or fs.exists("BDAV/BDSVM/00000.svm")))
        if bdplus_metadata:
            # BD+ always rides on AACS, but treat stray BDSVM data as protected
            # too: writing those clips without the VM would corrupt them.
            disc.encrypted = True
        if kind == "Blu-ray":
            mkb_type, _mkb_version = self._aacs_mkb(fs)
            generation = mkb_generation(mkb_type)
            if generation:
                # AACS 2.x means UHD Blu-ray.  libaacs implements AACS 1.0,
                # and bus encryption additionally needs a licensed drive and
                # host certificate, so these discs cannot be decrypted here.
                disc.uhd = True
                disc.aacs_generation = generation
                disc.encrypted = True
        disc.protection = (("AACS %s (UHD)" % disc.aacs_generation)
                           if kind == "Blu-ray" and disc.uhd
                           else "CSS/CPPM" if kind == "DVD" and disc.encrypted
                           else "AACS/BD+" if kind == "Blu-ray" and disc.encrypted
                           and bdplus_metadata
                           else "AACS" if kind == "Blu-ray" and disc.encrypted else "None")
        disc.timestamp = ""
        if kind == "DVD":
            try:
                stat = fs.size("VIDEO_TS/VIDEO_TS.IFO")
                _ = stat
            except OSError:
                pass
        if disc.title_count == 0:
            self.message(5010)
            self.writer.tcount(0)
            if isinstance(fs, CssFS):
                fs.close()
                fs = None
            return OpenResult(None, 0, fs, spec)
        rule = self.settings.get_str("app_DefaultSelectionString")
        preferred = self.settings.get_str("app_PreferredLanguage")
        for title in disc.titles:
            selection_rules.apply(title, rule, preferred)
            # The reference adds a CC->SRT rule automatically when the profile
            # has no <trackSettings input="CC"> (CLONE_SPEC §15), so the
            # synthetic caption track is always ticked initially.
            for stream in title.streams:
                if stream.codec_id == "S_CC608/DVD":
                    stream.selected = True
            title.output_name = expand_name(
                self.settings.get_str("app_DefaultOutputFileName"), title, disc)
        self.disc = disc
        self.fs = fs
        if disc.encrypted and kind == "Blu-ray":
            self.message(9021)
        if emit_info:
            self._emit_info(disc)
        elif self.event_sink is not None:
            self.event_sink({"event": "disc", "disc": serialize_disc(disc)})
        return OpenResult(disc, 0, fs, spec, disc.source_path)

    # ------------------------------------------------------------------
    # info records
    # ------------------------------------------------------------------
    def _emit_info(self, disc: Disc) -> None:
        label = CINFO_TYPE_LABEL.get(disc.kind, disc.kind)
        self.writer.cinfo(1, 6206, label)
        self.writer.cinfo(2, 0, disc.label)
        self.writer.cinfo(30, 0, disc.label)
        self.writer.cinfo(31, 6119, "<b>Source information</b><br>")
        self.writer.cinfo(32, 0, disc.volume_name or disc.label)
        self.writer.cinfo(33, 0, "0")
        for title in disc.titles:
            t = title.index
            self.writer.tinfo(t, 8, 0, str(title.chapter_count))
            self.writer.tinfo(t, 9, 0, title.duration_text)
            self.writer.tinfo(t, 10, 0, title.size_text)
            self.writer.tinfo(t, 11, 0, str(title.size_bytes))
            self.writer.tinfo(t, 24, 0, title.original_id)
            # VERIFIED: TINFO 26 is the cell map ("1-17"), and TINFO 25 is the
            # number of maps, not the number of VOB segments.
            segment_map = title.segment_map or str(len(title.segments) or 1)
            self.writer.tinfo(t, 25, 0, str(segment_map.count(",") + 1))
            self.writer.tinfo(t, 26, 0, segment_map)
            self.writer.tinfo(t, 27, 0, title.output_name)
            self.writer.tinfo(t, 30, 0, title.tree_text)
            self.writer.tinfo(t, 31, 6120, "<b>Title information</b><br>")
            self.writer.tinfo(t, 33, 0, "0")
            self.writer.tinfo(t, 49, 0, title.comment)
            for stream in title.streams:
                self._emit_stream(t, stream)
        self.writer.tcount(disc.title_count)
        if self.event_sink is not None:
            self.event_sink({"event": "disc", "disc": serialize_disc(disc)})

    def _emit_stream(self, title_index: int, stream: Stream) -> None:
        s = stream.index
        code = 6201 if stream.kind == "video" else 6202 if stream.kind == "audio" else 6203
        self.writer.sinfo(title_index, s, 1, code, stream.kind_label)
        # VERIFIED layout of a DVD track (CLONE_SPEC §25.3): video carries no
        # channel/language rows, audio carries a coded layout, subtitles only a
        # language.
        if stream.kind == "audio":
            if stream.channel_layout:
                self.writer.sinfo(title_index, s, 2, 5091, stream.channel_layout)
            self.writer.sinfo(title_index, s, 3, 0, stream.language)
            self.writer.sinfo(title_index, s, 4, 0, stream.language_name)
        elif stream.kind == "subtitle":
            self.writer.sinfo(title_index, s, 3, 0, stream.language)
            self.writer.sinfo(title_index, s, 4, 0, stream.language_name)
        self.writer.sinfo(title_index, s, 5, 0, stream.codec_id)
        short_name, long_name, conversion = _codec_labels(stream)
        self.writer.sinfo(title_index, s, 6, 0, short_name)
        self.writer.sinfo(title_index, s, 7, 0, long_name)
        if stream.bitrate:
            self.writer.sinfo(title_index, s, 13, 0, fmt_bitrate(stream.bitrate))
        if stream.kind == "audio":
            if stream.channels:
                self.writer.sinfo(title_index, s, 14, 0, str(stream.channels))
            if stream.sample_rate:
                self.writer.sinfo(title_index, s, 17, 0, str(stream.sample_rate))
        if stream.width and stream.height:
            self.writer.sinfo(title_index, s, 19, 0, f"{stream.width}x{stream.height}")
        if stream.aspect:
            self.writer.sinfo(title_index, s, 20, 0, stream.aspect)
        if stream.fps:
            self.writer.sinfo(title_index, s, 21, 0, stream.fps)
        self.writer.sinfo(title_index, s, 22, 0, str(stream.flags))
        self.writer.sinfo(title_index, s, 30, 0, _stream_summary(stream, short_name))
        self.writer.sinfo(title_index, s, 31, 6121, "<b>Track information</b><br>")
        self.writer.sinfo(title_index, s, 33, 0, "0" if stream.kind == "video" else "90")
        self.writer.sinfo(title_index, s, 38, 0, "d" if stream.default else "")
        if stream.default:
            self.writer.sinfo(title_index, s, 39, 0, "Default")
        if stream.kind == "audio":
            # VERIFIED: 2.0 prints "stereo", 5.1 prints "5.1(side)".
            layout = {2: "stereo", 6: "5.1(side)", 8: "7.1"}.get(stream.channels)
            if layout:
                self.writer.sinfo(title_index, s, 40, 0, layout)
        if stream.codec_id == "S_CC608/DVD":
            self.writer.sinfo(title_index, s, 34, 0, "Text subtitles ( Lossy conversion )")
            self.writer.sinfo(title_index, s, 41, 0, "Text")
        self.writer.sinfo(title_index, s, 42, 5087 if conversion else 5088,
                          "( Lossy conversion )" if conversion else "( Lossless conversion )")

    # ------------------------------------------------------------------
    # saving
    # ------------------------------------------------------------------
    def _resolve_inputs(self, title: Title,
                        workdir: Path | None = None,
                        on_staging: Callable[[int, int], None] | None = None,
                        cancelled: Callable[[], bool] | None = None,
                        decryptor: BlurayDecryptor | None = None,
                        ) -> tuple[list[str], tempfile.TemporaryDirectory | None]:
        if title.source == "mkv" and self.disc:
            return [self.disc.source_path], None
        if title.source == "dvd" and title.segments and all(
                segment.byte_size > 0 for segment in title.segments):
            return self._materialise_dvd(title.segments, workdir, on_staging,
                                         cancelled)
        if (title.source == "bluray" and title.segments
                and decryptor is not None and decryptor.active):
            return decryptor.stage(title.segments, workdir, on_staging,
                                   cancelled)
        temporary = None
        paths: list[str] = []
        for segment in title.segments:
            local = self.fs.local(segment.path) if self.fs is not None else None
            if local is None:
                if temporary is None:
                    temporary = tempfile.TemporaryDirectory(
                        prefix="opticrip-iso-",
                        dir=str(workdir) if workdir is not None else None,
                    )
                target = Path(temporary.name) / segment.path.replace("\\", "/")
                extractor = getattr(self.fs, "extract", None)
                if extractor is None:
                    raise OSError("source cannot be materialised for ffmpeg")
                extractor([segment.path], Path(temporary.name))
                local = str(target)
            paths.append(local)
        return paths, temporary

    def _materialise_dvd(self, segments: list,
                         workdir: Path | None = None,
                         on_progress: Callable[[int, int], None] | None = None,
                         cancelled: Callable[[], bool] | None = None
                         ) -> tuple[list[str], tempfile.TemporaryDirectory | None]:
        """Copy the title's PGC cells into one seekable VOB.

        A VTS can hold several titles and menus; saving every VOB verbatim
        makes their timestamp zero points collide, which players turn into
        speed-up/slow-down judder.  The IFO already gives exact cell sectors,
        so copy only those ranges and concatenate them in playback order.
        """
        if self.fs is None:
            raise OSError("source is not open")
        if cancelled is not None and cancelled():
            raise KeyboardInterrupt
        if workdir is not None:
            workdir = Path(workdir).resolve()
        first = segments[0]
        if len(segments) == 1 and first.byte_offset == 0:
            local = self.fs.local(first.path)
            try:
                whole = self.fs.size(first.path)
            except OSError:
                whole = 0
            if local and whole > 0 and first.byte_size >= whole:
                return [local], None
        total = sum(max(0, int(segment.byte_size)) for segment in segments)
        copied = 0
        last_reported = -1
        report_step = max(1, total // 200) if total else 1

        def report(value: int) -> None:
            nonlocal last_reported
            if on_progress is None or total <= 0:
                return
            value = min(max(0, int(value)), total)
            if value == last_reported:
                return
            if (value == 0 or value == total
                    or value - last_reported >= report_step):
                last_reported = value
                on_progress(value, total)

        temporary = tempfile.TemporaryDirectory(
            prefix="opticrip-dvd-",
            dir=str(workdir) if workdir is not None else None,
        )
        target = Path(temporary.name) / "title.vob"
        try:
            report(0)
            with target.open("wb") as out:
                for segment in segments:
                    base = copied
                    copied += self._copy_range(
                        segment, out, lambda count, base=base: report(base + count),
                        cancelled,
                    )
            if cancelled is not None and cancelled():
                raise KeyboardInterrupt
            report(copied)
            if target.stat().st_size == 0:
                raise OSError("title has no readable video data")
        except (Exception, KeyboardInterrupt):
            temporary.cleanup()
            raise
        return [str(target)], temporary

    def _copy_range(self, segment, out,
                    on_chunk: Callable[[int], None] | None = None,
                    cancelled: Callable[[], bool] | None = None) -> int:
        """Write one cell range, decrypting through DiscFS when necessary."""
        local = self.fs.local(segment.path) if self.fs is not None else None
        remaining = int(segment.byte_size)
        offset = int(segment.byte_offset)
        copied = 0

        def check_cancel() -> None:
            if cancelled is not None and cancelled():
                raise KeyboardInterrupt

        check_cancel()
        if local:
            with open(local, "rb") as source:
                source.seek(offset)
                while remaining > 0:
                    check_cancel()
                    chunk = source.read(min(remaining, 4 << 20))
                    if not chunk:
                        break
                    check_cancel()
                    out.write(chunk)
                    copied += len(chunk)
                    remaining -= len(chunk)
                    if on_chunk is not None:
                        on_chunk(copied)
            return copied
        while remaining > 0:
            check_cancel()
            chunk = self.fs.read(segment.path, offset, min(remaining, 4 << 20))
            if not chunk:
                break
            check_cancel()
            out.write(chunk)
            copied += len(chunk)
            offset += len(chunk)
            remaining -= len(chunk)
            if on_chunk is not None:
                on_chunk(copied)
        return copied

    def apply_selection(self, titles: Iterable[int] | None = None,
                        streams: dict[str, Iterable[int]] | None = None) -> None:
        """Apply the UI's title/track choices before a save.

        The GUI owns its own copy of the title tree, so the engine must be told
        which rows are ticked; otherwise every save would ignore the user's
        selection.  ``streams`` maps a title index (as a string, because it
        travels as JSON) to the stream ordinals that stay selected.
        """
        if self.disc is None:
            return
        wanted_titles = None if titles is None else {int(value) for value in titles}
        chosen_streams = {str(key): {int(v) for v in value}
                          for key, value in (streams or {}).items()}
        for title in self.disc.titles:
            if wanted_titles is not None:
                title.selected = title.index in wanted_titles
            chosen = chosen_streams.get(str(title.index))
            if chosen is not None:
                for stream in title.streams:
                    stream.selected = stream.index in chosen

    def _apply_profile(self, title: Title) -> None:
        for stream in title.streams:
            if not stream.selected:
                continue
            if stream.codec_id == "S_CC608/DVD":
                # Captions are never copied: they are decoded to SubRip.
                stream.output_codec = "SRT"
                stream.conversion = "SRT"
                continue
            output = self.profile.output_for(stream)
            mapping = {"directCopy": stream.codec, "FLAC": "FLAC", "AAC": "AAC",
                       "SRT": "SubRip", "LPCM-raw": "PCM", "LPCM-wavex": "PCM"}
            stream.output_codec = mapping.get(output.output_format, stream.codec)
            stream.conversion = output.output_format

    def save_title(self, title: Title, destination: Path,
                   cancel: Callable[[], bool] | None = None,
                   title_number: int = 1, title_total: int = 1) -> Path | None:
        if self.disc is None:
            self.message(9018)
            return None
        if self.disc.uhd:
            self.message(9143, self.disc.aacs_generation)
            return None
        decryptor: BlurayDecryptor | None = None
        if self.disc.encrypted and not isinstance(self.fs, CssFS):
            if self.disc.kind == "Blu-ray":
                decryptor = self._bluray_decryptor()
                if decryptor is None:
                    self.message(9100)
                    return None
            else:
                self.message(9020)
                self.message(9100)
                return None
        destination = Path(destination).resolve()
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.message(9015, str(destination))
            return None
        selected = title.selected_streams()
        if not selected:
            self.message(9027, title.index)
            return None
        # Tell the GUI which title owns this progress so the bar resets per
        # title and the ETA is measured against this title's length.
        fields = {
            "title": title.index,
            "title_number": title_number,
            "title_total": title_total,
            "duration": float(title.duration or 0.0),
        }
        self.emit_progress(0, 65536, phase="preparing", **fields)
        self._apply_profile(title)
        staging_seen = False

        def staging_progress(copied: int, total: int) -> None:
            nonlocal staging_seen
            if total <= 0:
                return
            staging_seen = True
            fraction = max(0.0, min(1.0, copied / total))
            self.emit_progress(
                int(fraction * 65536 * _STAGING_WEIGHT), 65536,
                phase="staging", phase_fraction=fraction,
                stage_current=int(copied), stage_total=int(total), **fields,
            )

        # Blu-ray stream ordinals from the STN table must be matched to the
        # indices ffmpeg assigns to the clip.
        try:
            inputs, temporary = self._resolve_inputs(
                title, destination, on_staging=staging_progress,
                cancelled=cancel, decryptor=decryptor,
            )
        except KeyboardInterrupt:
            self.message(9010)
            raise
        except (OSError, DvdcssError, BlurayDecryptError) as exc:
            self.message(9006, str(exc))
            return None
        if not inputs:
            self.message(9006, "no source segments")
            return None
        offset = _STAGING_WEIGHT if staging_seen else 0.0
        output: Path | None = None
        caption_dir: tempfile.TemporaryDirectory | None = None
        caption_srt: Path | None = None
        try:
            if any(stream.codec_id == "S_CC608/DVD" for stream in selected):
                caption_dir = tempfile.TemporaryDirectory(
                    prefix="opticrip-cc-", dir=str(destination))
                target = Path(caption_dir.name) / "captions.srt"
                try:
                    caption_srt = cclib.extract_srt(inputs, target,
                                                    title.duration, cancelled=cancel)
                except OSError:
                    caption_srt = None
                if caption_srt is None:
                    # The track was listed from a sample but the title has no
                    # decodable captions after all; skip the empty stream.
                    selected = [stream for stream in selected
                                if stream.codec_id != "S_CC608/DVD"]
            if title.source == "bluray":
                self._map_pids(inputs[0], title)
            name = expand_name(self.settings.get_str("app_DefaultOutputFileName"),
                               title, self.disc)
            output = unique_path(destination, name)
            self.message(9004, title.index, output.name)
            conversion = next((s.conversion for s in selected
                               if s.conversion in ("FLAC", "AAC")), None)
            compression = None
            for stream in selected:
                settings = self.profile.output_for(stream)
                if settings.compression_level:
                    compression = str(settings.compression_level)
                    break
            started = time.time()
            # Announce the transition before ffmpeg emits its first record, so
            # the bar does not sit on the staging label after the copy ends.
            self.emit_progress(int(offset * 65536), 65536, phase="saving",
                               phase_fraction=0.0, **fields)

            def progress(fraction: float) -> None:
                fraction = max(0.0, min(1.0, fraction))
                overall = offset + (1.0 - offset) * fraction
                self.emit_progress(int(overall * 65536), 65536,
                                   phase="saving", phase_fraction=fraction,
                                   **fields)
            remux(inputs, output, selected, title,
                  chapters=bool(title.chapter_times), compression=compression,
                  on_progress=progress, cancelled=cancel, workdir=destination,
                  caption_srt=caption_srt)
            if not output.exists() or output.stat().st_size == 0:
                raise FFmpegError("empty output")
            self.emit_progress(65536, 65536, phase="saving",
                               phase_fraction=1.0, **fields)
            elapsed = time.time() - started
            _ = elapsed
            title.output_name = output.name
            self.message(9005, title.index, output.name)
            return output
        except KeyboardInterrupt:
            self.message(9010)
            if output is not None and output.exists() and output.stat().st_size == 0:
                output.unlink(missing_ok=True)
            raise
        except FFmpegError as exc:
            self.message(9006, str(exc))
            if output is not None and output.exists() and output.stat().st_size == 0:
                output.unlink(missing_ok=True)
            return None
        finally:
            if temporary is not None:
                temporary.cleanup()
            if caption_dir is not None:
                caption_dir.cleanup()

    def _map_pids(self, first_input: str, title: Title) -> None:
        try:
            info = ffprobe_json(first_input)
        except FFmpegError:
            return
        by_pid: dict[int, int] = {}
        by_ordinal: list[int] = []
        for entry in info.get("streams", []):
            index = int(entry.get("index", -1))
            by_ordinal.append(index)
            raw = entry.get("id")
            if isinstance(raw, str) and raw.startswith("0x"):
                by_pid[int(raw, 16)] = index
        for stream in title.streams:
            if stream.pid and stream.pid in by_pid:
                stream.source_index = by_pid[stream.pid]
            elif stream.source_index is not None and stream.source_index < len(by_ordinal):
                stream.source_index = by_ordinal[stream.source_index]

    def save_selected(self, destination: Path,
                      cancel: Callable[[], bool] | None = None) -> tuple[int, int]:
        if self.disc is None:
            self.message(9018)
            return 0, 11
        selected = [t for t in self.disc.titles if t.selected]
        if not selected:
            self.message(9026)
            return 0, 11
        self.message(5014, len(selected), str(destination))
        saved = 0
        for number, title in enumerate(selected, 1):
            if self.cancelled or (cancel is not None and cancel()):
                self.message(9010)
                return saved, 11
            try:
                if self.save_title(title, destination, cancel,
                                   title_number=number,
                                   title_total=len(selected)) is not None:
                    saved += 1
            except KeyboardInterrupt:
                # save_title already reports 9010 for the cancelled title.
                return saved, 11
        self.message(5005, saved)
        self.message(5036, saved)
        return saved, 0 if saved == len(selected) else 11

    # ------------------------------------------------------------------
    # backup
    # ------------------------------------------------------------------
    def backup(self, spec: str, destination: Path,
               decrypt: bool | None = None,
               cancel: Callable[[], bool] | None = None) -> tuple[bool, int]:
        self.close_source()
        prefix, rest = self.split_spec(spec)
        if prefix != "disc":
            self.message(9109)
            return False, 10
        result = self._open_disc(rest, 0, False)
        if result.disc is None:
            return False, 10
        if result.disc.uhd:
            self.message(9143, result.disc.aacs_generation)
            return False, 11
        if decrypt is None:
            decrypt = self.settings.get_bool("app_BackupDecrypted", True)
        disc = result.disc
        destination = Path(destination)
        decryptor: BlurayDecryptor | None = None
        if decrypt and disc.encrypted:
            if disc.kind == "Blu-ray":
                decryptor = self._bluray_decryptor()
                if decryptor is None:
                    self.message(9100)
                    return False, 11
            elif disc.kind != "DVD" or not isinstance(self.fs, CssFS):
                self.message(9100)
                return False, 11
        self.message(9007, str(destination))

        def progress(done: int, total: int) -> None:
            if total <= 0:
                return
            self.emit_progress(int(65536 * min(done, total) / total), 65536,
                               phase="backup")

        writer = BackupWriter(
            self.fs, destination, kind=disc.kind,
            decrypt=bool(decrypt and disc.encrypted),
            decryptor=decryptor, message=self.message,
        )
        try:
            copied = writer.run(progress, cancel)
        except KeyboardInterrupt:
            self.message(9010)
            return False, 11
        except (BackupError, BlurayDecryptError, DvdcssError, OSError) as exc:
            self.message(9009, str(exc))
            return False, 11
        self.message(9008, copied)
        return True, 0

    # ------------------------------------------------------------------
    # registration and firmware tool
    # ------------------------------------------------------------------
    def register(self, text: str) -> tuple[bool, int]:
        candidate = text
        if Path(text).is_file():
            candidate = registration.read_key_file(text)
        valid, key = registration.validate(candidate)
        if not valid:
            self.message(9105)
            return False, 1
        self.settings.set("app_Key", key)
        self.settings.set("app_KeyHash", registration.key_hash(key))
        try:
            self.settings.save()
        except OSError:
            self.message(9103)
            return False, 1
        self.message(9011)
        return True, 0

    def firmware(self, args: list[str]) -> int:
        if not args:
            self.message(9111)
            return 2
        if args[0].lower() in ("list", "-l", "--list"):
            for drive in drivelib.enumerate_drives():
                self.writer.drive(drive.index, drive.state, 999, 1,
                                  drive.name, drive.label, drive.device)
            return 0
        self.message(9111)
        return 2

    # ------------------------------------------------------------------
    # GUI protocol
    # ------------------------------------------------------------------
    def serve(self) -> int:
        """JSON-lines protocol on stdin/stdout (clone IPC, R-ARCH-02).

        Saves run on a worker thread so the stdin loop stays responsive: the
        GUI's Cancel button must be able to set the flag while ffmpeg runs.
        """
        lock = threading.Lock()

        def sink(event: dict) -> None:
            line = json.dumps(event) + "\n"
            with lock:
                try:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                except (BrokenPipeError, ValueError):
                    pass

        # Robot lines must not reach stdout: it carries one JSON event per
        # line in this mode.
        self.writer = NullWriter()
        self.event_sink = sink
        worker: threading.Thread | None = None

        def run_job(job) -> None:
            nonlocal worker
            if worker is not None and worker.is_alive():
                sink({"event": "error", "message": "a save is already running"})
                return
            # A cancel request from before this job must not poison it.
            self.cancelled = False
            worker = threading.Thread(target=job, daemon=True)
            worker.start()

        sink({"event": "hello", "version": __version__, "product": PRODUCT})
        self.startup()
        sink({"event": "ready"})
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                continue
            command = request.get("cmd")
            try:
                if command == "open":
                    result = self.open_source(request["spec"],
                                              request.get("min_length"),
                                              emit_info=True,
                                              allow_mkv=True)
                    sink({"event": "opened", "ok": result.disc is not None,
                          "code": result.code})
                elif command == "drives":
                    self.emit_drives()
                elif command == "save":
                    self.apply_selection(request.get("titles"), request.get("streams"))
                    title_index = int(request["title"])
                    destination = Path(request["dest"])

                    def save_one(title_index=title_index, destination=destination) -> None:
                        try:
                            title = self.disc.titles[title_index]
                            path = self.save_title(title, destination,
                                                   cancel=lambda: self.cancelled)
                            sink({"event": "saved", "ok": path is not None,
                                  "path": str(path) if path else "",
                                  "cancelled": False})
                        except KeyboardInterrupt:
                            sink({"event": "saved", "ok": False, "path": "",
                                  "cancelled": True})
                        except Exception as exc:  # keep the engine alive
                            sink({"event": "error", "message": str(exc)})
                            sink({"event": "saved", "ok": False, "path": ""})

                    run_job(save_one)
                elif command == "save_all":
                    self.apply_selection(request.get("titles"), request.get("streams"))
                    destination = Path(request["dest"])

                    def save_all(destination=destination) -> None:
                        try:
                            count, code = self.save_selected(destination,
                                                             cancel=lambda: self.cancelled)
                            sink({"event": "saved", "ok": code == 0,
                                  "count": count, "code": code,
                                  "cancelled": bool(self.cancelled)})
                        except KeyboardInterrupt:
                            sink({"event": "saved", "ok": False, "count": 0,
                                  "code": 11, "cancelled": True})
                        except Exception as exc:  # keep the engine alive
                            sink({"event": "error", "message": str(exc)})
                            sink({"event": "saved", "ok": False, "count": 0, "code": 11})

                    run_job(save_all)
                elif command == "backup":
                    spec = request["spec"]
                    destination = Path(request["dest"])
                    decrypt = request.get("decrypt")

                    def backup(spec=spec, destination=destination, decrypt=decrypt) -> None:
                        try:
                            ok, code = self.backup(spec, destination, decrypt,
                                                   cancel=lambda: self.cancelled)
                            sink({"event": "backup_done", "ok": ok, "code": code})
                        except KeyboardInterrupt:
                            sink({"event": "backup_done", "ok": False, "code": 11})
                        except Exception as exc:  # keep the engine alive
                            sink({"event": "error", "message": str(exc)})
                            sink({"event": "backup_done", "ok": False, "code": 11})

                    run_job(backup)
                elif command == "settings":
                    values = request.get("values") or {}
                    for key, value in values.items():
                        self.settings.set(key, value)
                    self.settings.save()
                    sink({"event": "settings_saved", "ok": True})
                elif command == "register":
                    ok, code = self.register(str(request.get("key", "")))
                    sink({"event": "registered", "ok": ok, "code": code})
                elif command == "cancel":
                    self.cancelled = True
                    sink({"event": "cancelling"})
                elif command == "close":
                    self.close_source()
                    sink({"event": "closed"})
                elif command == "quit":
                    self.cancelled = True
                    sink({"event": "bye"})
                    if worker is not None and worker.is_alive():
                        worker.join(timeout=2)
                    return 0
            except Exception as exc:  # keep the engine alive for the UI
                sink({"event": "error", "message": str(exc)})
        return 0


def serialize_stream(stream: Stream) -> dict:
    return {
        "kind": stream.kind, "index": stream.index, "codec": stream.codec,
        "codec_id": stream.codec_id, "codec_long": stream.codec_long,
        "language": stream.language, "language_name": stream.language_name,
        "channels": stream.channels, "channel_layout": stream.channel_layout,
        "width": stream.width, "height": stream.height, "aspect": stream.aspect,
        "fps": stream.fps, "bitrate": stream.bitrate, "flags": stream.flags,
        "forced": stream.forced, "selected": stream.selected,
        "output_codec": stream.output_codec, "display": stream.display,
        "kind_label": stream.kind_label,
    }


def serialize_title(title: Title) -> dict:
    return {
        "index": title.index, "name": title.name, "comment": title.comment,
        "duration": title.duration, "duration_text": title.duration_text,
        "size_bytes": title.size_bytes, "size_text": title.size_text,
        "chapters": title.chapter_count, "tree_text": title.tree_text,
        "selected": title.selected, "output_name": title.output_name,
        "original_id": title.original_id, "streams": [serialize_stream(s) for s in title.streams],
    }


def serialize_disc(disc: Disc) -> dict:
    return {
        "kind": disc.kind, "label": disc.label, "volume_name": disc.volume_name,
        "protection": disc.protection, "size_bytes": disc.size_bytes,
        "size_text": disc.size_text, "encrypted": disc.encrypted,
        "uhd": disc.uhd, "aacs_generation": disc.aacs_generation,
        "titles": [serialize_title(t) for t in disc.titles],
        "title_count": disc.title_count,
    }
