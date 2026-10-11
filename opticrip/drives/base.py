"""Provider composition and change detection."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ..core.interfaces import DriveProvider
from ..core.logging_bus import LogBus
from ..core.models import Drive


class CompositeProvider(DriveProvider):
    """Merges several providers, de-duplicating by device path."""

    name = "composite"

    def __init__(self, providers: Iterable[DriveProvider]) -> None:
        self.providers = list(providers)

    def list_drives(self) -> list[Drive]:
        seen: dict[str, Drive] = {}
        for provider in self.providers:
            try:
                for drive in provider.list_drives():
                    seen.setdefault(drive.path, drive)
            except Exception:  # one broken provider must not hide the others
                LogBus().error(f"drive provider {provider.name!r} failed",
                               context="drives")
        return list(seen.values())


@dataclass
class DriveSetChange:
    """Diff between two drive snapshots."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def diff_drives(old: dict[str, Drive], new: dict[str, Drive]) -> DriveSetChange:
    change = DriveSetChange()
    for path, drive in new.items():
        if path not in old:
            change.added.append(path)
        else:
            prev = old[path]
            if (prev.state, prev.media_type, prev.volume_label) != \
                    (drive.state, drive.media_type, drive.volume_label):
                change.changed.append(path)
    change.removed = [p for p in old if p not in new]
    return change


class DriveWatcher:
    """Polls a provider and calls back whenever the drive set changes.

    Discs are inserted and ejected while the app is running, so a light poll
    (default every 2 s) is the portable way to notice.  ``SDiskWatcher.stop()``
    is safe to call from any thread and from a GUI shutdown handler.
    """

    def __init__(self, provider: DriveProvider, *,
                 on_change: Callable[[DriveSetChange, list[Drive]], None] | None = None,
                 interval: float = 2.0,
                 log: LogBus | None = None) -> None:
        self.provider = provider
        self.on_change = on_change
        self.interval = interval
        self._log = log or LogBus()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._drives: dict[str, Drive] = {}

    # -- lifecycle ------------------------------------------------------ #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._drives = {d.path: d for d in self.provider.list_drives()}
        self._thread = threading.Thread(target=self._run, name="opticrip-drivewatch",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def drives(self) -> list[Drive]:
        return list(self._drives.values())

    def poll_once(self) -> DriveSetChange:
        try:
            current = {d.path: d for d in self.provider.list_drives()}
        except Exception as exc:
            self._log.error(f"drive rescan failed: {exc}", context="drives")
            return DriveSetChange()
        change = diff_drives(self._drives, current)
        self._drives = current
        if not change.empty and self.on_change:
            try:
                self.on_change(change, list(current.values()))
            except Exception:
                self._log.error("drive change callback raised", context="drives")
        return change

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.poll_once()


def poll_until_changed(watcher: DriveWatcher, timeout: float, *,
                       interval: float = 0.25) -> DriveSetChange:
    """Blocking helper used by tests and the CLI ``--watch`` mode."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        change = watcher.poll_once()
        if not change.empty:
            return change
        time.sleep(interval)
    return DriveSetChange()
