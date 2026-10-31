"""GUI-side client for the engine process.

The engine runs as a separate process (R-ARCH-01) and speaks one JSON object
per line.  This is the clone's own transport: the reference uses a named pipe
whose framing is UNKNOWN (U1), so nothing here claims wire compatibility.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any


class EngineClient:
    def __init__(self) -> None:
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.process: subprocess.Popen | None = None
        self._stderr_thread: threading.Thread | None = None
        self.start()

    def start(self) -> None:
        root = Path(__file__).resolve().parent.parent.parent
        creationflags = 0x08000000 if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "opticrip.cli", "guiserver"],
                cwd=str(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            self.events.put({"event": "error", "message": f"cannot start engine: {exc}"})
            return
        threading.Thread(target=self._read_stdout, daemon=True).start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.events.put(json.loads(line))
            except ValueError:
                self.events.put({"event": "stderr", "message": line})
        self.events.put({"event": "engine_exit",
                         "code": self.process.poll() if self.process else -1})

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            text = line.strip()
            if text:
                self.events.put({"event": "stderr", "message": text})

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def send(self, **request: Any) -> None:
        if not self.alive or self.process is None or self.process.stdin is None:
            self.events.put({"event": "error", "message": "engine is not running"})
            return
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
        except OSError as exc:
            self.events.put({"event": "error", "message": str(exc)})

    def close(self) -> None:
        if self.process is None:
            return
        try:
            self.send(cmd="quit")
        except Exception:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
