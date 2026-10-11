"""User settings: defaults, JSON persistence, validation.

Stored under ``$XDG_CONFIG_HOME/opticrip/settings.json`` (``~/.config/...``)
so a packaged app and a source checkout behave the same.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from typing import Any, ClassVar

from . import errors
from .naming import DEFAULT_TEMPLATE

CONFIG_VERSION = 1


def default_config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "opticrip")


def default_config_path() -> str:
    override = os.environ.get("OPTICRIP_CONFIG")
    if override:
        return os.path.expanduser(override)
    return os.path.join(default_config_dir(), "settings.json")


def default_output_dir() -> str:
    for candidate in ("~/Videos", "~/Video", "~"):
        path = os.path.expanduser(candidate)
        if os.path.isdir(path):
            return os.path.join(path, "OpticRip")
    return os.path.join(os.path.expanduser("~"), "OpticRip")


OVERWRITE_POLICIES = ("ask", "overwrite", "skip", "rename")
REMUX_BACKENDS = ("auto", "ffmpeg", "mkvmerge")


@dataclass
class Settings:
    """Everything the user can change. Plain data, JSON round-trippable."""

    version: int = CONFIG_VERSION

    # -- output ------------------------------------------------------------ #
    output_dir: str = field(default_factory=default_output_dir)
    filename_template: str = DEFAULT_TEMPLATE
    overwrite_policy: str = "ask"          # one of OVERWRITE_POLICIES
    delete_temp_files: bool = True

    # -- selection defaults ------------------------------------------------ #
    include_chapters: bool = True
    include_forced_subtitles: bool = False
    #: titles shorter than this are considered extras, not the main feature
    min_feature_seconds: int = 1200
    #: preferred languages tried first when auto-selecting audio/subtitles
    preferred_audio_languages: list[str] = field(default_factory=lambda: ["eng"])
    preferred_subtitle_languages: list[str] = field(default_factory=lambda: ["eng"])

    # -- engine ------------------------------------------------------------ #
    remux_backend: str = "auto"            # one of REMUX_BACKENDS
    validate_after_rip: bool = True
    keep_failed_outputs: bool = False
    max_parallel_jobs: int = 1             # per drive
    allow_parallel_drives: bool = True
    read_retries: int = 3
    #: extra directories searched for ffmpeg/ffprobe/mkvmerge
    tool_search_paths: list[str] = field(default_factory=list)
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    mkvmerge_path: str = ""

    # -- interface --------------------------------------------------------- #
    theme: str = "dark"
    log_level: str = "INFO"
    show_technical_columns: bool = False

    #: keys that must never be persisted are listed here
    _NON_PERSISTED: ClassVar[tuple[str, ...]] = ()

    # ------------------------------------------------------------------ #
    def normalise(self) -> "Settings":
        """Clamp/repair values loaded from disk or set by a hand-edited file."""
        if self.overwrite_policy not in OVERWRITE_POLICIES:
            self.overwrite_policy = "ask"
        if self.remux_backend not in REMUX_BACKENDS:
            self.remux_backend = "auto"
        self.max_parallel_jobs = max(1, min(int(self.max_parallel_jobs or 1), 8))
        self.read_retries = max(0, min(int(self.read_retries or 0), 20))
        self.min_feature_seconds = max(0, int(self.min_feature_seconds or 0))
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self.log_level = "INFO"
        if not self.filename_template.strip():
            self.filename_template = DEFAULT_TEMPLATE
        self.output_dir = os.path.expanduser(self.output_dir or default_output_dir())
        self.preferred_audio_languages = [l for l in
                                          (self.preferred_audio_languages or []) if l]
        self.preferred_subtitle_languages = [l for l in
                                             (self.preferred_subtitle_languages or []) if l]
        self.tool_search_paths = [p for p in (self.tool_search_paths or []) if p]
        self.version = CONFIG_VERSION
        return self

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in self._NON_PERSISTED:
            data.pop(key, None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        known = {f.name for f in fields(cls)}
        unknown = {k: v for k, v in data.items() if k not in known}
        filtered = {k: v for k, v in data.items() if k in known}
        settings = cls(**filtered)
        if unknown:
            settings.discarded_keys = unknown  # type: ignore[attr-defined]
        return settings.normalise()

    # ------------------------------------------------------------------ #
    def save(self, path: str | None = None) -> str:
        target = path or default_config_path()
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        # atomic replace so a crash never leaves a truncated config
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, target)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise errors.PermissionError_(
                f"could not write settings to {target}",
                hint="Check that the configuration directory is writable.",
                detail=str(exc)) from exc
        return target

    @classmethod
    def load(cls, path: str | None = None) -> "Settings":
        target = path or default_config_path()
        if not os.path.exists(target):
            return cls().normalise()
        try:
            with open(target, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise errors.OpticRipError(
                f"settings file {target} is not valid JSON",
                hint="Delete or repair the file; defaults will be used next run.",
                detail=str(exc)) from exc
        except OSError as exc:
            raise errors.PermissionError_(
                f"cannot read settings from {target}", detail=str(exc)) from exc
        if not isinstance(data, dict):
            raise errors.OpticRipError(f"settings file {target} must contain an object")
        return cls.from_dict(data)
