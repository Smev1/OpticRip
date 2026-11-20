"""Browser front end for the headless engine.

The server is deliberately small and dependency-free: one HTTP thread per
request, one engine, one job at a time.  It is meant for ``localhost`` or a
trusted LAN; there is no authentication.

The API is JSON over HTTP and is not part of the ``makemkvcon``-compatible
surface:

    GET  /api/health       liveness probe
    GET  /api/state        snapshot: disc, job, progress, log, drives
    POST /api/open         {"spec": "disc:0" | "file:/..." | "iso:/..."}
    POST /api/save         {"title": 0, "dest": "/out", "streams": {"0": [1]}}
    POST /api/save_all     {"titles": [0, 1], "dest": "/out", "streams": {...}}
    POST /api/backup       {"spec": "disc:0", "dest": "/backup", "decrypt": true}
    POST /api/cancel       request cancellation of a running save/backup
    POST /api/close        close the current source
    POST /api/drives       rescan drive slots
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .. import PRODUCT, __version__
from ..engine import Engine, serialize_disc
from ..profiles import builtin_default
from ..robot import NullWriter
from ..settings import Settings

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 64 * 1024
_ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    ),
}


class WebState:
    """Thread-safe adapter between HTTP requests and one :class:`Engine`."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.engine = Engine(NullWriter(), self.settings, builtin_default())
        self.lock = threading.RLock()
        self.busy = False
        self.job = ""
        self.disc: dict[str, Any] | None = None
        self.progress: dict[str, Any] = {"current": 0, "total": 65536}
        self.log: deque[dict[str, Any]] = deque(maxlen=500)
        self.seq = 0
        self.error = ""
        self.last: dict[str, Any] | None = None
        self.drives: dict[int, dict[str, Any]] = {}
        self.engine.event_sink = self._on_event
        # The banner is useful in the log; enumerating drives on Linux only
        # produces the "no drive" message, so wait for an explicit scan.
        self.engine.startup(emit_drives=False)

    # -- engine events ---------------------------------------------------
    def _on_event(self, event: dict[str, Any]) -> None:
        with self.lock:
            kind = event.get("event")
            if kind == "message":
                self.log.append({
                    "seq": self.seq,
                    "time": time.time(),
                    "id": event.get("id", 0),
                    "text": event.get("text", ""),
                    "flags": event.get("flags", 0),
                })
                self.seq += 1
            elif kind == "progress":
                self.progress = dict(event)
            elif kind == "disc":
                self.disc = event.get("disc")
            elif kind == "drive":
                index = int(event.get("index", 0))
                if event.get("present"):
                    self.drives[index] = dict(event)
                else:
                    self.drives.pop(index, None)
            elif kind == "failure":
                text = " / ".join(part for part in (
                    str(event.get("headline") or ""),
                    str(event.get("advice") or "")) if part)
                self.log.append({
                    "seq": self.seq, "time": time.time(), "id": 0,
                    "text": text, "flags": 0,
                })
                self.seq += 1
                self.error = str(event.get("headline") or "")
            elif kind == "error":
                self.error = str(event.get("message", ""))

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "product": PRODUCT,
                "version": __version__,
                "busy": self.busy,
                "job": self.job,
                "disc": self.disc,
                "progress": dict(self.progress),
                "log": list(self.log)[-300:],
                "error": self.error,
                "last": self.last,
                "drives": [self.drives[key] for key in sorted(self.drives)],
                "output_dir": str(self.settings.output_dir),
                "source": self.engine.spec,
            }

    # -- jobs ------------------------------------------------------------
    def _start(self, name: str, work: Callable[[], dict[str, Any]]) -> bool:
        with self.lock:
            if self.busy:
                return False
            self.busy = True
            self.job = name
            self.error = ""
            self.progress = {"current": 0, "total": 65536, "phase": "starting"}
        self.engine.cancelled = False
        threading.Thread(target=self._run, args=(name, work), daemon=True).start()
        return True

    def _run(self, name: str, work: Callable[[], dict[str, Any]]) -> None:
        result: dict[str, Any]
        try:
            result = work()
        except Exception as exc:  # keep the server alive for the UI
            with self.lock:
                self.error = str(exc)
            result = {"ok": False, "kind": name, "message": str(exc)}
        with self.lock:
            self.last = result
            if not result.get("ok", True):
                self.error = str(result.get("message") or self.error)
            self.busy = False
            self.job = ""
            self.progress = {**self.progress, "phase": "done"}

    # -- validation ------------------------------------------------------
    @staticmethod
    def _destination(body: dict[str, Any], settings: Settings) -> Path:
        raw = body.get("dest")
        text = str(raw).strip() if raw is not None else str(settings.output_dir)
        path = Path(text).expanduser()
        if not text or not path.is_absolute():
            raise ValueError("destination must be an absolute path")
        return path

    @staticmethod
    def _streams(raw: Any) -> dict[str, list[int]] | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("streams must be an object keyed by title index")
        cleaned: dict[str, list[int]] = {}
        for key, values in raw.items():
            if not isinstance(values, (list, tuple)):
                raise ValueError("each streams entry must be a list")
            try:
                title = int(key)
                cleaned[str(title)] = [int(value) for value in values]
            except (TypeError, ValueError):
                raise ValueError("stream indexes must be integers") from None
        return cleaned

    @staticmethod
    def _titles(raw: Any, disc: Any) -> list[int]:
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ValueError("titles must be a non-empty list")
        valid = {title.index for title in disc.titles}
        titles: list[int] = []
        for value in raw:
            if isinstance(value, bool):
                raise ValueError("title indexes must be integers")
            try:
                index = int(value)
            except (TypeError, ValueError):
                raise ValueError("title indexes must be integers") from None
            if index not in valid:
                raise ValueError(f"unknown title {index}")
            titles.append(index)
        return titles

    def _require_disc(self) -> Any:
        if self.engine.disc is None:
            raise ValueError("open a source first")
        return self.engine.disc

    # -- operations ------------------------------------------------------
    def open_source(self, spec: str) -> bool:
        spec = str(spec).strip()
        if not spec:
            raise ValueError("source is required")

        def work() -> dict[str, Any]:
            result = self.engine.open_source(spec, emit_info=True, allow_mkv=True)
            if result.disc is None:
                with self.lock:
                    self.disc = None
                return {"ok": False, "kind": "open", "code": result.code,
                        "message": "Could not open the source; see the log."}
            with self.lock:
                self.disc = serialize_disc(result.disc)
            return {"ok": True, "kind": "open", "code": result.code,
                    "label": result.disc.label or result.disc.volume_name}

        return self._start("open", work)

    def save_one(self, body: dict[str, Any]) -> bool:
        disc = self._require_disc()
        if isinstance(body.get("title"), bool):
            raise ValueError("title must be an integer")
        try:
            index = int(body.get("title"))
        except (TypeError, ValueError):
            raise ValueError("title must be an integer") from None
        if index not in {title.index for title in disc.titles}:
            raise ValueError(f"unknown title {index}")
        destination = self._destination(body, self.settings)
        streams = self._streams(body.get("streams"))

        def work() -> dict[str, Any]:
            if streams is not None:
                self.engine.apply_selection(None, streams)
            title = self.engine.disc.titles[index]
            path = self.engine.save_title(
                title, destination, cancel=lambda: self.engine.cancelled)
            if path is None:
                return {"ok": False, "kind": "save",
                        "message": "Save failed; see the log."}
            self._remember_output(destination)
            return {"ok": True, "kind": "save", "path": str(path)}

        return self._start("save", work)

    def save_all(self, body: dict[str, Any]) -> bool:
        disc = self._require_disc()
        titles = self._titles(body.get("titles"), disc)
        destination = self._destination(body, self.settings)
        streams = self._streams(body.get("streams"))

        def work() -> dict[str, Any]:
            self.engine.apply_selection(titles, streams)
            count, code = self.engine.save_selected(
                destination, cancel=lambda: self.engine.cancelled)
            self._remember_output(destination)
            return {"ok": code == 0, "kind": "save", "count": count,
                    "code": code,
                    "message": "" if code == 0 else "One or more titles failed; see the log."}

        return self._start("save", work)

    def backup(self, body: dict[str, Any]) -> bool:
        spec = str(body.get("spec") or "").strip()
        if not spec:
            raise ValueError("source is required")
        destination = self._destination(body, self.settings)
        decrypt = body.get("decrypt")
        if decrypt is not None and not isinstance(decrypt, bool):
            raise ValueError("decrypt must be true or false")

        def work() -> dict[str, Any]:
            ok, code = self.engine.backup(
                spec, destination, decrypt,
                cancel=lambda: self.engine.cancelled)
            if ok:
                self._remember_output(destination)
            return {"ok": ok, "kind": "backup", "code": code,
                    "message": "" if ok else "Backup failed; see the log."}

        return self._start("backup", work)

    def _remember_output(self, destination: Path) -> None:
        try:
            self.settings.set_output_dir(destination)
            self.settings.save()
        except OSError:
            pass

    def cancel(self) -> bool:
        self.engine.cancelled = True
        with self.lock:
            self.log.append({"seq": self.seq, "time": time.time(), "id": 0,
                             "text": "Cancel requested", "flags": 0})
            self.seq += 1
        return True

    def close(self) -> bool:
        self.engine.close_source()
        with self.lock:
            self.disc = None
            self.progress = {"current": 0, "total": 65536, "phase": "idle"}
        return True

    def refresh_drives(self) -> bool:
        self.engine.emit_drives()
        return True

    def close_engine(self) -> None:
        self.engine.cancelled = True
        self.engine.close_source()


class _Handler(BaseHTTPRequestHandler):
    server_version = f"OpticRipWeb/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- response helpers ------------------------------------------------
    def _headers(self, status: int, content_type: str, length: int,
                 cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()

    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _reject(self, payload: dict[str, Any], status: int) -> None:
        """Answer a request whose body we deliberately did not read.

        Writing the error alone is not enough: with HTTP/1.1 keep-alive the
        unread body stays in the socket buffer, so the parser would read it as
        the next request line, fail, and then fail again to report that second
        error on the closing connection.  Closing with the response keeps the
        framing honest.
        """
        self.close_connection = True
        self._json(payload, status)

    def _static(self, name: str) -> None:
        content_type = _ASSETS.get(name)
        if content_type is None:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            data = (STATIC_DIR / name).read_bytes()
        except OSError:
            self._json({"error": "asset missing"}, HTTPStatus.NOT_FOUND)
            return
        self._headers(HTTPStatus.OK, content_type, len(data), cache="no-cache")
        self.wfile.write(data)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc == self.headers.get("Host", "")

    def _body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return {}
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if length < 0 or length > MAX_BODY:
            raise ValueError("request body is too large")
        if length == 0:
            return {}
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            raise ValueError("invalid JSON") from None
        if not isinstance(data, dict):
            raise ValueError("a JSON object is required")
        return data

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        state: WebState = self.server.state  # type: ignore[attr-defined]
        if path == "/api/health":
            self._json({"ok": True, "product": PRODUCT, "version": __version__})
        elif path == "/api/state":
            self._json(state.snapshot())
        elif path == "/":
            self._static("index.html")
        elif path in ("/index.html", "/app.js", "/style.css"):
            self._static(path.lstrip("/"))
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._same_origin():
            self._reject({"error": "cross-origin request refused"},
                         HTTPStatus.FORBIDDEN)
            return
        state: WebState = self.server.state  # type: ignore[attr-defined]
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/open":
                accepted = state.open_source(str(body.get("spec", "")))
            elif path == "/api/save":
                accepted = state.save_one(body)
            elif path == "/api/save_all":
                accepted = state.save_all(body)
            elif path == "/api/backup":
                accepted = state.backup(body)
            elif path == "/api/cancel":
                accepted = state.cancel()
            elif path == "/api/close":
                accepted = state.close()
            elif path == "/api/drives":
                accepted = state.refresh_drives()
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
        except ValueError as exc:
            # A bad length or content type leaves the body unread, so this is
            # never safe to keep alive either.
            self._reject({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if not accepted:
            self._json({"error": "another job is already running"},
                       HTTPStatus.CONFLICT)
            return
        self._json({"ok": True, "state": state.snapshot()}, HTTPStatus.ACCEPTED)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep stdout/stderr for real problems, not one line per request."""
        return


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(host: str = "127.0.0.1", port: int = 8080,
                  state: WebState | None = None) -> ThreadingHTTPServer:
    """Create (but do not start) the HTTP server; useful in tests."""
    server = _Server((host, port), _Handler)
    server.state = state or WebState()  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m opticrip.web",
        description="Serve the OpticRip browser UI.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080,
                        help="port to listen on (default: 8080)")
    parser.add_argument("--data-dir", default="",
                        help="settings/data directory (overrides OPTICRIP_DATA_DIR)")
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ["OPTICRIP_DATA_DIR"] = args.data_dir

    state = WebState()
    server = create_server(args.host, args.port, state)
    shown = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    print(f"OpticRip web UI on http://{shown}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        state.close_engine()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
