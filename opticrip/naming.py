"""Output file name template.

The reference default is::

    {NAME2}{-:CMNT2}{-:DT}{title:+DFLT}{_t:N2}

which yields ``title_t00.mkv`` for an unnamed title and ``YB8_t00.mkv`` for a
title whose comment is ``YB8`` (CLONE_SPEC §13, §25.6).  The mini language is
implemented as observed:

* ``{VALUE}`` inserts a value.
* ``{PREFIX:VALUE}`` inserts ``PREFIX`` then the value, but only when the value
  is non-empty; the prefix is also dropped when it would lead the name.
* ``{value:+DFLT}`` falls back to the literal ``title`` when the whole name
  would otherwise be empty.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .model import Disc, Title

TOKEN_RE = re.compile(r"\{([^{}]*)\}")
VALUE_TOKENS = {"name", "name1", "name2", "cmnt", "cmnt2", "title", "dt", "n", "n2", "disc"}
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str) -> str:
    cleaned = _INVALID.sub("", name).strip().rstrip(".")
    return cleaned or ""


def _value(token: str, title: Title, disc: Disc | None) -> str:
    token = token.lower()
    if token in ("name", "name1", "name2", "title"):
        return sanitize(title.name)
    if token in ("cmnt", "cmnt2"):
        return sanitize(title.comment)
    if token == "disc":
        return sanitize(disc.label if disc else "")
    if token == "dt":
        stamp = ""
        if disc and disc.timestamp:
            stamp = disc.timestamp
        try:
            parsed = datetime.fromisoformat(stamp)
            return parsed.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return ""
    if token in ("n", "n2"):
        return f"{title.index:02d}"
    return ""


def expand(template: str, title: Title, disc: Disc | None = None) -> str:
    template = template or "{title}"
    result = ""
    for match in TOKEN_RE.finditer(template):
        token = match.group(1)
        modifier = ""
        if "+" in token:
            token, _, modifier = token.partition("+")
            modifier = modifier.lower()
        prefix = ""
        value_token = token
        if token.lower() not in VALUE_TOKENS:
            head, sep, tail = token.partition(":")
            if sep and tail.lower() in VALUE_TOKENS:
                prefix, value_token = head, tail
            else:
                prefix, value_token = "", token
        value = _value(value_token, title, disc)
        if not value:
            # {title:+DFLT} supplies the literal fallback "title".
            if modifier == "dflt" and not result:
                value = "title"
            else:
                continue
        if prefix and result:
            result += prefix
        elif prefix and not result:
            # A separator must never lead a file name.
            if prefix.strip(" _-."):
                result += prefix
        result += value
    result = sanitize(result)
    if not result:
        result = "title"
    if not re.search(r"\.mkv$", result, re.IGNORECASE):
        result += ".mkv"
    return result


def default_template() -> str:
    return "{NAME2}{-:CMNT2}{-:DT}{title:+DFLT}{_t:N2}"


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    for counter in range(1, 1000):
        candidate = directory / f"{stem}_{counter}{candidate.suffix}"
        if not candidate.exists():
            return candidate
    return directory / name
