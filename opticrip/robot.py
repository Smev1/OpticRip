"""Robot-mode and human console output.

Line grammar (CLONE_SPEC.md §6):

    MSG:<id>,<flags>,<param-count>,"<rendered>","<template>","<param>"...
    DRV:<index>,<state>,<code>,<visible>,"<drive>","<disc>","<device>"
    TCOUNT:<n>
    CINFO:<attr>,<msgcode>,"<value>"
    TINFO:<title>,<attr>,<msgcode>,"<value>"
    SINFO:<title>,<stream>,<attr>,<msgcode>,"<value>"
    PRGV:<current>,<total>,<max>
    PRGC:<code>,<id>,"<action>"
    PRGT:<code>,<id>,"<operation>"

Every string parameter is double quoted and backslash/quote escaped; all
parameters (including numbers) are quoted in MSG lines, which is what the
reference capture shows (analysis/evidence/rip_title0.txt).
"""

from __future__ import annotations

import sys
from typing import IO, Sequence

from . import BANNER, messages


def quote(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


class EventWriter:
    """Base writer; the GUI adapter and the console writers share this API."""

    def message(self, ident: int, *params: object) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def drive(self, index: int, state: int, code: int, visible: int,
              name: str, disc: str, device: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def tcount(self, count: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def cinfo(self, attr: int, msgcode: int, value: object) -> None:  # pragma: no cover
        raise NotImplementedError

    def tinfo(self, title: int, attr: int, msgcode: int, value: object) -> None:  # pragma: no cover
        raise NotImplementedError

    def sinfo(self, title: int, stream: int, attr: int, msgcode: int,
              value: object) -> None:  # pragma: no cover
        raise NotImplementedError

    def progress(self, current: int, total: int, maximum: int = 65536) -> None:
        return None

    def action(self, code: int, ident: int, name: str) -> None:
        return None

    def operation(self, code: int, ident: int, name: str) -> None:
        return None


class RobotWriter(EventWriter):
    """Machine interface.  One flushed line per event, stdout only (R-CLI-04)."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self.stream = stream or sys.stdout

    def _line(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def message(self, ident: int, *params: object) -> None:
        definition = messages.definition(ident)
        rendered = messages.render(ident, *params)
        pieces = [
            f"MSG:{ident},{definition.flags},{len(params)}",
            quote(rendered),
            quote(definition.template),
        ]
        pieces.extend(quote(p) for p in params)
        self._line(",".join(pieces))

    def drive(self, index: int, state: int, code: int, visible: int,
              name: str, disc: str, device: str) -> None:
        self._line(
            f"DRV:{index},{state},{code},{visible},"
            f"{quote(name)},{quote(disc)},{quote(device)}"
        )

    def tcount(self, count: int) -> None:
        self._line(f"TCOUNT:{count}")

    def cinfo(self, attr: int, msgcode: int, value: object) -> None:
        self._line(f"CINFO:{attr},{msgcode},{quote(value)}")

    def tinfo(self, title: int, attr: int, msgcode: int, value: object) -> None:
        self._line(f"TINFO:{title},{attr},{msgcode},{quote(value)}")

    def sinfo(self, title: int, stream: int, attr: int, msgcode: int,
              value: object) -> None:
        self._line(f"SINFO:{title},{stream},{attr},{msgcode},{quote(value)}")

    def progress(self, current: int, total: int, maximum: int = 65536) -> None:
        self._line(f"PRGV:{current},{total},{maximum}")

    def action(self, code: int, ident: int, name: str) -> None:
        self._line(f"PRGC:{code},{ident},{quote(name)}")

    def operation(self, code: int, ident: int, name: str) -> None:
        self._line(f"PRGT:{code},{ident},{quote(name)}")


class HumanWriter(EventWriter):
    """Plain-text console output (no robot consumer)."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self.stream = stream or sys.stdout

    def _line(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def message(self, ident: int, *params: object) -> None:
        self._line(messages.render(ident, *params))

    def drive(self, index: int, state: int, code: int, visible: int,
              name: str, disc: str, device: str) -> None:
        if visible:
            self._line(f"Drive {index}: {name} ({device}) - {disc}")

    def tcount(self, count: int) -> None:
        self._line(f"Total {count} titles")

    def cinfo(self, attr: int, msgcode: int, value: object) -> None:
        if attr in (1, 2, 32):
            self._line(str(value))

    def tinfo(self, title: int, attr: int, msgcode: int, value: object) -> None:
        if attr == 30:
            self._line(f"Title {title:2d}: {value}")

    def sinfo(self, title: int, stream: int, attr: int, msgcode: int,
              value: object) -> None:
        return None

    def progress(self, current: int, total: int, maximum: int = 65536) -> None:
        return None

    def action(self, code: int, ident: int, name: str) -> None:
        self._line(f"Current action: {name}")

    def operation(self, code: int, ident: int, name: str) -> None:
        self._line(f"Current operation: {name}")


class NullWriter(EventWriter):
    """Discards every event; used when the engine talks JSON to the GUI."""

    def message(self, ident: int, *params: object) -> None:
        return None

    def drive(self, index: int, state: int, code: int, visible: int,
              name: str, disc: str, device: str) -> None:
        return None

    def tcount(self, count: int) -> None:
        return None

    def cinfo(self, attr: int, msgcode: int, value: object) -> None:
        return None

    def tinfo(self, title: int, attr: int, msgcode: int, value: object) -> None:
        return None

    def sinfo(self, title: int, stream: int, attr: int, msgcode: int,
              value: object) -> None:
        return None


class MultiWriter(EventWriter):
    """Fan out one event to several writers (console + GUI log)."""

    def __init__(self, writers: Sequence[EventWriter]) -> None:
        self.writers = list(writers)

    def message(self, ident: int, *params: object) -> None:
        for w in self.writers:
            w.message(ident, *params)

    def drive(self, index: int, state: int, code: int, visible: int,
              name: str, disc: str, device: str) -> None:
        for w in self.writers:
            w.drive(index, state, code, visible, name, disc, device)

    def tcount(self, count: int) -> None:
        for w in self.writers:
            w.tcount(count)

    def cinfo(self, attr: int, msgcode: int, value: object) -> None:
        for w in self.writers:
            w.cinfo(attr, msgcode, value)

    def tinfo(self, title: int, attr: int, msgcode: int, value: object) -> None:
        for w in self.writers:
            w.tinfo(title, attr, msgcode, value)

    def sinfo(self, title: int, stream: int, attr: int, msgcode: int,
              value: object) -> None:
        for w in self.writers:
            w.sinfo(title, stream, attr, msgcode, value)

    def progress(self, current: int, total: int, maximum: int = 65536) -> None:
        for w in self.writers:
            w.progress(current, total, maximum)

    def action(self, code: int, ident: int, name: str) -> None:
        for w in self.writers:
            w.action(code, ident, name)

    def operation(self, code: int, ident: int, name: str) -> None:
        for w in self.writers:
            w.operation(code, ident, name)


def banner(writer: EventWriter) -> None:
    """Emit the startup banner exactly like the reference (id 1005)."""
    writer.message(1005, BANNER)
