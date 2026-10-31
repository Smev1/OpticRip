"""Conversion profiles (``.mmcp.xml``).

Schema and defaults follow CLONE_SPEC §15.  Only the elements the clone acts
on are interpreted; unknown elements are ignored so that a profile written for
the reference still loads (U8 in the spec is exactly this ambiguity).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .model import Stream


class ProfileError(Exception):
    pass


@dataclass
class OutputSettings:
    name: str
    output_format: str = "directCopy"
    description: str = ""
    compression: str = ""
    compression_level: int = 0
    extra_args: list[str] = field(default_factory=list)

    @property
    def is_copy(self) -> bool:
        return self.output_format == "directCopy"


@dataclass
class TrackRule:
    input: str
    output_settings_name: str
    default_selection: str = ""

    @property
    def inputs(self) -> list[str]:
        return [part.strip() for part in self.input.split("|") if part.strip()]


@dataclass
class AudioMix:
    name: str
    output_bps: int = 16
    channel_layout: str = "stereo"
    mix: str = ""
    extras: dict[str, str] = field(default_factory=dict)


@dataclass
class Profile:
    name: str = "Default"
    path: str = ""
    mkv_settings: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, OutputSettings] = field(default_factory=dict)
    tracks: list[TrackRule] = field(default_factory=list)
    mixes: dict[str, AudioMix] = field(default_factory=dict)

    def output_for(self, stream: Stream) -> OutputSettings:
        for rule in self.tracks:
            if any(self._matches(item, stream) for item in rule.inputs):
                found = self.outputs.get(rule.output_settings_name)
                if found is not None:
                    return found
        for rule in self.tracks:
            if any(item == "default" for item in rule.inputs):
                found = self.outputs.get(rule.output_settings_name)
                if found is not None:
                    return found
        if self.outputs:
            return next(iter(self.outputs.values()))
        return OutputSettings("copy")

    @staticmethod
    def _matches(rule_input: str, stream: Stream) -> bool:
        key = rule_input.lower()
        if key == "default":
            return True
        if key.startswith("lpcm"):
            if stream.codec.upper() not in ("LPCM", "PCM"):
                return False
            return ("multi" in key and stream.channels > 2) or (
                "stereo" in key and stream.channels <= 2
            )
        if key.startswith("truehd"):
            return stream.codec.lower() == "truehd"
        if key == "cc":
            return stream.codec.lower() in ("eia-608", "cc")
        return key in stream.codec.lower()


def load_profile(path: str | Path) -> Profile:
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ProfileError(str(exc)) from exc
    if root.tag.lower() != "profile":
        raise ProfileError("root element is not <profile>")
    profile = Profile(path=str(path))
    for element in root.findall("name"):
        profile.name = (element.text or profile.name).strip()
    settings = root.find("mkvSettings")
    if settings is not None:
        profile.mkv_settings = dict(settings.attrib)
    for element in root.findall("outputSettings"):
        name = element.get("name") or "copy"
        output = OutputSettings(
            name=name,
            output_format=element.get("outputFormat") or "directCopy",
        )
        for description in element.findall("description"):
            output.description = (description.text or "").strip()
        compression = element.find("mkvTrackSettings")
        if compression is not None:
            output.compression = compression.get("compression", "")
            try:
                output.compression_level = int(compression.get("compressionLevel", "0"))
            except ValueError:
                output.compression_level = 0
        output.extra_args = [(arg.text or "").strip() for arg in element.findall("extraArgs")]
        profile.outputs[name] = output
    for element in root.findall("audioMixSettings"):
        mix = AudioMix(
            name=element.get("name") or "mix",
            output_bps=int(element.get("outputBPS", "16") or 16),
            channel_layout=element.get("outputChannelLayout", "stereo"),
            mix=element.get("mix", ""),
            extras=dict(element.attrib),
        )
        profile.mixes[mix.name] = mix
    for element in root.findall("trackSettings"):
        for output in element.findall("output"):
            profile.tracks.append(TrackRule(
                input=element.get("input", "default"),
                output_settings_name=output.get("outputSettingsName", "copy"),
                default_selection=output.get("defaultSelection", ""),
            ))
    if not profile.outputs:
        profile.outputs["copy"] = OutputSettings("copy")
    return profile


def bundled_directory() -> Path:
    return Path(__file__).resolve().parent / "profiles"


def discover(directories: list[Path] | None = None) -> dict[str, Path]:
    """Map profile name -> file, user profiles override bundled ones."""
    found: dict[str, Path] = {}
    search = list(directories or [])
    search.append(bundled_directory())
    for directory in search:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.mmcp.xml")):
            found[path.stem.replace(".mmcp", "")] = path
    return found


def load_named(name: str, directories: list[Path] | None = None) -> Profile:
    available = discover(directories)
    path = available.get(name)
    if path is None:
        raise ProfileError(f"profile '{name}' not found")
    return load_profile(path)


def builtin_default() -> Profile:
    return Profile(
        name="Default",
        outputs={"copy": OutputSettings("copy")},
        tracks=[TrackRule("default", "copy")],
        mkv_settings={
            "ignoreForcedSubtitlesFlag": "true",
            "useISO639Type2T": "false",
            "setFirstAudioTrackAsDefault": "true",
            "setFirstSubtitleTrackAsDefault": "true",
            "setFirstForcedSubtitleTrackAsDefault": "true",
            "insertFirstChapter00IfMissing": "true",
        },
    )
