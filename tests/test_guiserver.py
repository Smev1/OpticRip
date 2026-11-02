"""The GUI's JSON-lines protocol, end to end.

The engine runs in its own process, so this test drives the real
``guiserver`` command: build a tiny two-stream MKV with FFmpeg, open it, tick
only its audio track, save, and probe the result.  It is the regression guard
for the selection plumbing and for the worker-thread save path (which keeps
Cancel responsive).

The source clip is generated instead of committed, so the repository carries
no media binaries and the stream layout the assertions rely on stays visible.
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from opticrip.ffmpeg import available, ffprobe_json, tool

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SECONDS = 4


@unittest.skipUnless(available(), "ffmpeg is required")
class GuiServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = self._build_fixture()
        self.env = mock.patch.dict(os.environ, {
            "OPTICRIP_JSON_SETTINGS": "1",
            "OPTICRIP_DATA_DIR": self.tmp.name,
        })
        self.env.start()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "opticrip.cli", "guiserver"],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self.events: "queue.Queue[dict]" = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def tearDown(self):
        try:
            self.send("quit")
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()
        self.env.stop()
        self.tmp.cleanup()

    def _build_fixture(self) -> Path:
        """Synthesise the two-stream MKV this class saves from.

        FFmpeg is already required by the assertions (the output is probed),
        so generating the clip costs a second and keeps a 400 KB opaque blob
        out of the repository.
        """
        target = Path(self.tmp.name) / "fixture.mkv"
        command = [
            tool("ffmpeg"), "-y", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"testsrc=size=320x240:rate=25:duration={FIXTURE_SECONDS}",
            "-f", "lavfi", "-i",
            f"sine=frequency=440:duration={FIXTURE_SECONDS}",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", "-shortest",
            str(target),
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0 or not target.exists():
            self.skipTest("could not build the MKV fixture: "
                          f"{completed.stderr.strip()[-300:]}")
        return target

    def _reader(self):
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self.events.put(json.loads(line))

    def send(self, cmd, **fields):
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"cmd": cmd, **fields}) + "\n")
        self.proc.stdin.flush()

    def wait_for(self, name, timeout=20, progress=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            event = self.events.get(timeout=max(0.1, deadline - time.time()))
            if event.get("event") == "progress" and progress is not None:
                progress.append(event)
            if event.get("event") == name:
                return event
        self.fail(f"no {name!r} event; stderr={self.proc.stderr.read(2000)}")

    def test_save_honours_title_and_track_selection(self):
        self.wait_for("ready")
        self.send("open", spec=str(self.fixture))
        disc_event = self.wait_for("disc")
        titles = disc_event["disc"]["titles"]
        self.assertEqual(len(titles), 1)
        audio = next(s for s in titles[0]["streams"] if s["kind"] == "audio")
        out = Path(self.tmp.name) / "out"
        progress = []
        self.send("save_all", dest=str(out), titles=[0],
                  streams={"0": [audio["index"]]})
        saved = self.wait_for("saved", timeout=30, progress=progress)
        self.assertTrue(saved.get("ok"), saved)
        self.assertFalse(saved.get("cancelled"))
        files = list(out.glob("*.mkv"))
        self.assertEqual(len(files), 1)
        info = ffprobe_json(str(files[0]))
        self.assertEqual([s["codec_type"] for s in info["streams"]], ["audio"])
        self.assertTrue(progress)
        self.assertTrue(any(event.get("title_number") == 1
                            and event.get("title_total") == 1
                            and "duration" in event for event in progress))
        self.assertTrue(any(event.get("phase") == "saving"
                            and "phase_fraction" in event for event in progress))
        self.assertTrue(all(0 <= event["current"] <= event["total"]
                            for event in progress))


if __name__ == "__main__":
    unittest.main()
