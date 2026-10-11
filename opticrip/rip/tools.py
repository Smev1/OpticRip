"""External tool discovery and capability probing.

OpticRip never assumes a capability - it asks the tool.  ``Toolchain.detect()``
locates ``ffmpeg``/``ffprobe``/``mkvmerge`` on ``PATH`` (plus configured
search paths) and records:

* the version string of each tool;
* which demuxers/muxers/protocols it was built with (``dvdvideo``, ``bluray``,
  ``matroska``);
* whether DVD/Blu-ray navigation is therefore available.

Results are cached and surfaced in the GUI's diagnostics panel.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

from ..core.errors import ToolMissingError
from ..core.logging_bus import LogBus

#: demuxers/protocols a fully featured build must expose
REQUIRED_FFMPEG_DEMUXERS = ("dvdvideo", "matroska")
OPTIONAL_FFMPEG_PROTOCOLS = ("bluray",)
REQUIRED_FFMPEG_MUXERS = ("matroska",)


@dataclass
class ToolInfo:
    name: str
    path: str
    version: str = ""
    available: bool = False
    features: dict[str, list[str]] = field(default_factory=dict)
    error: str = ""

    @property
    def version_line(self) -> str:
        return f"{self.name}: {self.path} ({self.version or 'unknown version'})" \
            if self.available else f"{self.name}: NOT FOUND{(' - ' + self.error) if self.error else ''}"


def _run(args: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", str(exc)


def _frozen_bins_dir() -> str | None:
    """Directory of bundled external binaries in a PyInstaller frozen app.

    PyInstaller onefile extracts to ``sys._MEIPASS``; onedir keeps the
    layout next to the executable. External binaries are bundled under
    ``_bin/`` by the .spec file.
    """
    import sys
    base = getattr(sys, "frozen", False) and (
        getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable))
    if not base:
        return None
    candidate = os.path.join(base, "_bin")
    return candidate if os.path.isdir(candidate) else None


def find_executable(name: str, *, extra_paths: list[str] | None = None,
                    explicit: str = "") -> str | None:
    """Locate ``name``, honouring an explicit setting then extra directories.

    Search order: explicit -> bundled _bin/ (frozen apps) -> extra_paths ->
    PATH.
    """
    if explicit:
        expanded = os.path.expanduser(explicit)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    frozen_dir = _frozen_bins_dir()
    if frozen_dir:
        candidate = os.path.join(frozen_dir, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    for directory in extra_paths or []:
        candidate = os.path.join(os.path.expanduser(directory), name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which(name)
    if found:
        return found
    # common non-PATH locations (snap/flatpak/local installs)
    for candidate in (f"/usr/bin/{name}", f"/usr/local/bin/{name}",
                      f"/snap/bin/{name}", f"/var/lib/flatpak/exports/bin/{name}"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# ``ffmpeg -demuxers``/``-muxers``/``-decoders``/``-encoders`` print lines of
# the form "<flags> <name> <description>"; the middle token is the name.
# ``-protocols`` instead prints "Input:"/"Output:" headings with one protocol
# per indented line.
_COMPONENT_LINE = re.compile(
    r"^\s*([A-Za-z.]{1,8})\s+([A-Za-z0-9_][A-Za-z0-9_,.+\-]*)\s+(\S.*)$")

_PROTOCOL_HEADER = re.compile(r"^(Input|Output):\s*$")
_PROTOCOL_NAME = re.compile(r"^\s{2,}([A-Za-z0-9_][A-Za-z0-9_,.+\-]*)\s*$")


def _parse_component_listing(text: str) -> list[str]:
    """Component names from ``-demuxers``/``-muxers``/``-decoders``/``-encoders``."""
    names: list[str] = []
    for line in text.splitlines():
        match = _COMPONENT_LINE.match(line)
        if not match:
            continue
        flags, name = match.group(1), match.group(2)
        if flags.strip(".") == "":       # a legend line such as " D.. "
            continue
        # "matroska,webm" style names list every accepted alias
        for candidate in (n.strip() for n in name.split(",")):
            if candidate and candidate not in names:
                names.append(candidate)
    return names


def _parse_protocols(text: str) -> list[str]:
    """Protocol names from ``ffmpeg -protocols`` (both Input: and Output:)."""
    names: list[str] = []
    in_section = False
    for line in text.splitlines():
        if _PROTOCOL_HEADER.match(line):
            in_section = True
            continue
        if not in_section:
            continue
        match = _PROTOCOL_NAME.match(line)
        if match:
            name = match.group(1)
            if name not in names:
                names.append(name)
        elif line.strip():
            in_section = False
    return names


class Toolchain:
    """Discovered tools + their capabilities."""

    def __init__(self, log: LogBus | None = None) -> None:
        self._log = log or LogBus()
        self.ffmpeg = ToolInfo("ffmpeg", "")
        self.ffprobe = ToolInfo("ffprobe", "")
        self.mkvmerge = ToolInfo("mkvmerge", "")
        self.probed = False

    # -- discovery ------------------------------------------------------ #
    @classmethod
    def detect(cls, *, ffmpeg_path: str = "", ffprobe_path: str = "",
               mkvmerge_path: str = "", extra_paths: list[str] | None = None,
               log: LogBus | None = None) -> "Toolchain":
        chain = cls(log=log)
        chain.ffmpeg = _probe_ffmpeg(
            "ffmpeg", find_executable("ffmpeg", extra_paths=extra_paths,
                                      explicit=ffmpeg_path))
        chain.ffprobe = _probe_ffmpeg(
            "ffprobe", find_executable("ffprobe", extra_paths=extra_paths,
                                       explicit=ffprobe_path))
        chain.mkvmerge = _probe_mkvmerge(
            find_executable("mkvmerge", extra_paths=extra_paths,
                            explicit=mkvmerge_path))
        chain.probed = True
        for tool in (chain.ffmpeg, chain.ffprobe, chain.mkvmerge):
            chain._log.info(tool.version_line, context="tools")
        return chain

    # -- capability queries --------------------------------------------- #
    def has(self, name: str) -> bool:
        return getattr(self, name).available

    def dvd_navigation(self) -> bool:
        return (self.ffmpeg.available
                and "dvdvideo" in self.ffmpeg.features.get("demuxers", []))

    def bluray_navigation(self) -> bool:
        return (self.ffmpeg.available
                and "bluray" in self.ffmpeg.features.get("protocols", []))

    def mkv_muxing(self) -> bool:
        return (self.ffmpeg.available
                and "matroska" in self.ffmpeg.features.get("muxers", [])) \
            or self.mkvmerge.available

    def capabilities_report(self) -> list[tuple[str, bool, str]]:
        """Rows for the diagnostics table: (capability, ok, detail)."""
        rows: list[tuple[str, bool, str]] = []
        rows.append(("ffprobe (media inspection)", self.ffprobe.available,
                     self.ffprobe.version or self.ffprobe.error))
        rows.append(("ffmpeg (remuxing to MKV)", self.ffmpeg.available,
                     self.ffmpeg.version or self.ffmpeg.error))
        rows.append(("DVD navigation (libdvdread/libdvdnav)",
                     self.dvd_navigation(),
                     "dvdvideo demuxer " + ("present" if self.dvd_navigation() else "missing")))
        rows.append(("Blu-ray navigation (libbluray)", self.bluray_navigation(),
                     "bluray: protocol " + ("present" if self.bluray_navigation() else "missing")))
        rows.append(("MKV muxing", self.mkv_muxing(),
                     "ffmpeg matroska muxer" if self.ffmpeg.available else "not available"))
        rows.append(("mkvmerge backend (optional)", self.mkvmerge.available,
                     self.mkvmerge.version or "not installed"))
        return rows

    def require(self, name: str) -> str:
        tool: ToolInfo = getattr(self, name)
        if not tool.available:
            raise ToolMissingError(
                f"{name} is required but was not found",
                hint=_install_hint(name),
                detail=tool.error)
        return tool.path


def _probe_ffmpeg(name: str, path: str | None) -> ToolInfo:
    info = ToolInfo(name, path or "")
    if not path:
        info.error = "not found on PATH"
        return info
    info.available = True
    code, out, err = _run([path, "-hide_banner", "-version"])
    if code == 0 and out:
        info.version = out.splitlines()[0].strip()
    elif err:
        info.error = err.strip().splitlines()[:1] and err.strip().splitlines()[0] or ""
    for key, args in (("demuxers", ["-demuxers"]), ("muxers", ["-muxers"]),
                      ("decoders", ["-decoders"]), ("encoders", ["-encoders"])):
        code, out, _ = _run([path, "-hide_banner", *args])
        if code == 0:
            info.features[key] = _parse_component_listing(out)
    code, out, _ = _run([path, "-hide_banner", "-protocols"])
    if code == 0:
        info.features["protocols"] = _parse_protocols(out)
    code, out, _ = _run([path, "-hide_banner", "-buildconf"])
    if code == 0:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("--enable-"):
                info.features.setdefault("build", []).append(line[9:])
    return info


def _probe_mkvmerge(path: str | None) -> ToolInfo:
    info = ToolInfo("mkvmerge", path or "")
    if not path:
        info.error = "not installed (optional)"
        return info
    code, out, err = _run([path, "--version"])
    blob = (out or err).strip().splitlines()
    if code == 0 and blob:
        info.available = True
        info.version = blob[0].strip()
    else:
        info.error = blob[0].strip() if blob else "could not run --version"
    return info


def _install_hint(name: str) -> str:
    return {
        "ffmpeg": "Install it: sudo apt install ffmpeg  (Debian/Ubuntu)",
        "ffprobe": "ffprobe ships with ffmpeg: sudo apt install ffmpeg",
        "mkvmerge": "Optional: sudo apt install mkvtoolnix",
    }.get(name, f"Install {name} and ensure it is on PATH.")


@lru_cache(maxsize=1)
def default_toolchain() -> Toolchain:
    """Process-wide toolchain used when the caller does not inject one."""
    return Toolchain.detect()
