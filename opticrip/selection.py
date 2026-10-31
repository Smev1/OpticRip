"""Track selection language (CLONE_SPEC §16).

Supported grammar, which is what the shipped default rule uses::

    -sel:all,+sel:(favlang|nolang|single),-sel:(havemulti|havecore),
    +sel:mvcvideo,=100:all,-10:favlang

Rules are applied left to right; a later rule wins for the streams it matches.
``=N:`` / ``-N:`` only adjust weight, they do not select.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import FLAG_HAS_CORE, Stream, Title

DEFAULT_RULE = (
    "-sel:all,+sel:(favlang|nolang|single),-sel:(havemulti|havecore),"
    "+sel:mvcvideo,=100:all,-10:favlang"
)

_RULE_RE = re.compile(r"^([+\-=])(\d+)?(?:sel:)?(.+)$")


@dataclass
class Rule:
    action: str          # "+" | "-" | "=" | "-N"
    weight: int
    groups: list[str]
    explicit_sel: bool


def _split_groups(text: str) -> list[str]:
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return [part.strip().lower() for part in text.split("|") if part.strip()]


def parse(rule_text: str) -> list[Rule]:
    rules: list[Rule] = []
    depth = 0
    current = ""
    parts: list[str] = []
    for char in rule_text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if current:
        parts.append(current)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        match = _RULE_RE.match(part)
        if not match:
            continue
        sign, weight_text, body = match.groups()
        if sign in ("=",) or (sign == "-" and weight_text):
            weight = int(weight_text or 0)
            if sign == "-":
                weight = -weight
            rules.append(Rule("=", weight, _split_groups(body), False))
        elif sign == "+":
            rules.append(Rule("+", 0, _split_groups(body), True))
        else:
            rules.append(Rule("-", 0, _split_groups(body), True))
    return rules


def _group_matches(group: str, stream: Stream, title: Title,
                   preferred: str, kind_count: dict[str, int]) -> bool:
    if group == "all":
        return True
    if group == "true":
        return True
    if group == "favlang":
        return bool(preferred) and stream.language == preferred
    if group == "nolang":
        return stream.language in ("", "und")
    if group == "single":
        return kind_count.get(stream.kind, 0) == 1
    if group == "havemulti":
        return stream.kind == "audio" and stream.channels > 2
    if group == "havecore":
        return bool(stream.flags & FLAG_HAS_CORE)
    if group == "mvcvideo":
        return stream.kind == "video" and "mvc" in (stream.codec + stream.codec_id).lower()
    return False


def apply(title: Title, rule_text: str = DEFAULT_RULE,
          preferred: str = "") -> None:
    rules = parse(rule_text)
    counts: dict[str, int] = {}
    for stream in title.streams:
        counts[stream.kind] = counts.get(stream.kind, 0) + 1
    for stream in title.streams:
        stream.selected = True
        for rule in rules:
            if not any(_group_matches(g, stream, title, preferred, counts)
                       for g in rule.groups):
                continue
            if rule.action == "+":
                stream.selected = True
            elif rule.action == "-":
                stream.selected = False
            elif rule.action == "=":
                # Weight rules only influence ordering; they never select.
                stream.weight += rule.weight
        # A title must keep a video stream selected; dropping it would produce
        # an audio-only file the reference never writes.
    if not any(s.selected for s in title.video_streams()):
        for stream in title.video_streams():
            stream.selected = True
    if not any(s.selected for s in title.streams):
        for stream in title.streams:
            stream.selected = True


def selected(title: Title) -> list[Stream]:
    return [s for s in title.streams if s.selected]
