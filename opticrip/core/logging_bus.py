"""A tiny in-process pub/sub log bus shared by the engine and the GUI.

The engine must not import Qt, but the GUI needs a live log panel.  The bus
decouples them: engine code calls ``bus.log(...)``, the GUI subscribes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass(frozen=True)
class LogRecord:
    time: float
    level: str
    message: str
    context: str = ""

    @property
    def time_label(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.time))

    def to_line(self) -> str:
        prefix = f"[{self.level:<7}]"
        where = f" ({self.context})" if self.context else ""
        return f"{self.time_label} {prefix}{where} {self.message}"


class LogBus:
    """Thread-safe fan-out of log records with a bounded replay buffer.

    A late subscriber (the GUI opening after the engine started) still sees
    recent history thanks to the replay buffer.
    """

    def __init__(self, history: int = 500) -> None:
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[LogRecord], None]] = []
        self._history: deque[LogRecord] = deque(maxlen=history)

    # -- subscription ------------------------------------------------------ #
    def subscribe(self, callback: Callable[[LogRecord], None],
                  *, replay: bool = True) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)
            backlog = list(self._history) if replay else []
        for record in backlog:
            callback(record)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def history(self) -> list[LogRecord]:
        with self._lock:
            return list(self._history)

    # -- publishing -------------------------------------------------------- #
    def emit(self, record: LogRecord) -> None:
        with self._lock:
            self._history.append(record)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(record)
            except Exception:  # a broken listener must never break the engine
                logging.getLogger(__name__).exception("log subscriber failed")

    def log(self, level: str, message: str, *, context: str = "") -> None:
        self.emit(LogRecord(time=time.time(), level=level.upper(),
                            message=message, context=context))

    def debug(self, message: str, *, context: str = "") -> None:
        self.log("DEBUG", message, context=context)

    def info(self, message: str, *, context: str = "") -> None:
        self.log("INFO", message, context=context)

    def warning(self, message: str, *, context: str = "") -> None:
        self.log("WARNING", message, context=context)

    def error(self, message: str, *, context: str = "") -> None:
        self.log("ERROR", message, context=context)

    def bind(self, context: str) -> "BoundLog":
        return BoundLog(self, context)


class BoundLog:
    """A :class:`LogBus` view that stamps every record with a context label."""

    __slots__ = ("_bus", "_context")

    def __init__(self, bus: LogBus, context: str) -> None:
        self._bus = bus
        self._context = context

    def debug(self, message: str) -> None:
        self._bus.log("DEBUG", message, context=self._context)

    def info(self, message: str) -> None:
        self._bus.log("INFO", message, context=self._context)

    def warning(self, message: str) -> None:
        self._bus.log("WARNING", message, context=self._context)

    def error(self, message: str) -> None:
        self._bus.log("ERROR", message, context=self._context)

    def child(self, context: str) -> "BoundLog":
        return BoundLog(self._bus, f"{self._context}:{context}")


def records_to_text(records: Iterable[LogRecord]) -> str:
    return "\n".join(r.to_line() for r in records)
