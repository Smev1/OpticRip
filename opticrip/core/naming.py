"""Output filename templates.

Templates use ``{placeholder}`` tokens and are rendered with ``str.format_map``
against a namespace that never raises ``KeyError`` - an unknown placeholder is
left verbatim so the user immediately sees their typo instead of losing the
name entirely.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import unicodedata

from .models import Title, TitleSelection

DEFAULT_TEMPLATE = "{disc}_{title}"

#: tokens accepted in templates, with a short description for the settings UI
TOKENS: dict[str, str] = {
    "disc": "Disc volume label (or drive name)",
    "title": "Title name, e.g. \"Title 1\"",
    "index": "Title index on the disc (1-based)",
    "duration": "Title duration, e.g. 1h32m",
    "date": "Current date, YYYY-MM-DD",
    "year": "Current year",
    "id": "Title id from the disc structure",
    "chapters": "Selected chapter range, e.g. ch01-12",
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DOTS = re.compile(r"^\.+|\.+$")
_SPACES = re.compile(r"\s+")


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # keep the token, don't crash
        return "{" + key + "}"


def sanitise_component(name: str, *, fallback: str = "untitled",
                       max_length: int = 120) -> str:
    """Make ``name`` safe as a single path component on every platform."""
    if not name:
        return fallback
    name = unicodedata.normalize("NFC", name)
    name = _ILLEGAL.sub("_", name)
    name = _SPACES.sub(" ", name).strip().strip(".")
    name = _DOTS.sub("", name)
    # Windows reserved device names would break future portability.
    stem = name.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}:
        name = f"_{name}"
    if len(name) > max_length:
        root, ext = os.path.splitext(name)
        keep = max_length - len(ext)
        name = root[:keep].rstrip() + ext
    return name or fallback


def _duration_token(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(max(0, total), 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def build_namespace(title: Title, selection: TitleSelection, *,
                    disc_label: str = "",
                    now: _dt.datetime | None = None) -> dict[str, object]:
    now = now or _dt.datetime.now()
    chapter_token = ""
    if selection.chapter_start or selection.chapter_end:
        count = title.chapter_count or 1
        first, last = selection.resolved_chapter_range(count)
        chapter_token = f"ch{first:02d}-{last:02d}"
    return {
        "disc": disc_label or "disc",
        "title": title.display_name,
        "index": int(title.index),
        "duration": _duration_token(title.duration_seconds),
        "date": now.strftime("%Y-%m-%d"),
        "year": int(now.strftime("%Y")),
        "id": title.id,
        "chapters": chapter_token,
    }


def render_filename(template: str, title: Title, selection: TitleSelection, *,
                    disc_label: str = "", extension: str = "mkv",
                    now: _dt.datetime | None = None) -> str:
    """Render + sanitise a filename (no directory, no path separators)."""
    template = (template or DEFAULT_TEMPLATE).strip()
    namespace = build_namespace(title, selection, disc_label=disc_label, now=now)
    try:
        rendered = template.format_map(_SafeDict(namespace))
    except (ValueError, IndexError):
        # malformed braces in a user template: fall back to the default
        rendered = DEFAULT_TEMPLATE.format_map(_SafeDict(namespace))
    if not rendered.strip():
        rendered = DEFAULT_TEMPLATE.format_map(_SafeDict(namespace))
    name = sanitise_component(rendered)
    if extension and not name.lower().endswith("." + extension.lower()):
        name = f"{name}.{extension}"
    return name


def resolve_output_path(directory: str, template: str, title: Title,
                        selection: TitleSelection, *, disc_label: str = "",
                        extension: str = "mkv",
                        now: _dt.datetime | None = None) -> str:
    """Full destination path, honouring an explicit ``selection.output_name``."""
    if selection.output_name:
        leaf = sanitise_component(os.path.basename(selection.output_name))
        if not leaf.lower().endswith("." + extension.lower()):
            leaf = f"{leaf}.{extension}"
    else:
        leaf = render_filename(template, title, selection,
                               disc_label=disc_label, extension=extension, now=now)
    return os.path.join(os.path.expanduser(directory), leaf)
