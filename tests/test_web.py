import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from opticrip import __version__
from opticrip.settings import JsonBackend, Settings
from opticrip.web.server import WebState, create_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "OPTICRIP_DATA_DIR": self.tmp.name,
            "OPTICRIP_JSON_SETTINGS": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        settings = Settings(JsonBackend(Path(self.tmp.name) / "settings.json"))
        self.state = WebState(settings)
        self.server = create_server("127.0.0.1", 0, self.state)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body=None, headers=None):
        payload = None
        request_headers = dict(headers or {})
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request(method, path, payload, request_headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, data, dict(response.getheaders()))
        connection.close()
        return result

    def json_request(self, method: str, path: str, body=None, headers=None):
        status, data, response_headers = self.request(method, path, body, headers)
        return status, json.loads(data), response_headers

    def wait_idle(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.state.snapshot()["busy"]:
                return self.state.snapshot()
            time.sleep(0.02)
        self.fail("job did not finish")

    def test_health_and_index(self):
        status, data, headers = self.json_request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], __version__)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"OpticRip", body)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("default-src", headers["Content-Security-Policy"])

        status, body, headers = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])

    def test_unknown_asset_is_404(self):
        status, data, _ = self.json_request("GET", "/engine.py")
        self.assertEqual(status, 404)
        self.assertIn("error", data)

    def test_state_shape(self):
        status, data, _ = self.json_request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(data["busy"])
        self.assertIsNone(data["disc"])
        self.assertEqual(data["output_dir"], str(self.state.settings.output_dir))
        self.assertIsInstance(data["log"], list)
        self.assertIsInstance(data["drives"], list)

    def test_open_missing_iso_reports_failure(self):
        missing = Path(self.tmp.name) / "missing.iso"
        status, data, _ = self.json_request(
            "POST", "/api/open", {"spec": f"iso:{missing}"})
        self.assertEqual(status, 202)
        snapshot = self.wait_idle()
        self.assertFalse(snapshot["busy"])
        self.assertIsNone(snapshot["disc"])
        self.assertTrue(snapshot["error"])
        self.assertIn(5006, [entry["id"] for entry in snapshot["log"]])

    def test_save_without_a_source_is_rejected(self):
        status, data, _ = self.json_request(
            "POST", "/api/save", {"title": 0, "dest": str(Path(self.tmp.name) / "out")})
        self.assertEqual(status, 400)
        self.assertIn("open a source", data["error"])

    def test_cross_origin_post_is_refused(self):
        status, data, headers = self.json_request(
            "POST", "/api/open", {"spec": "iso:/tmp/x.iso"},
            {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", data["error"])
        # The request body was never read, so the socket must not be reused:
        # replaying the leftover body as a request line desynchronises the
        # connection and turns a clean 403 into a socket traceback.
        self.assertEqual(headers.get("Connection"), "close")

    def test_invalid_json_is_rejected(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("POST", "/api/open", b"not json",
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertIn("invalid JSON", data["error"])

    def test_drives_endpoint_accepts_scan(self):
        status, data, _ = self.json_request("POST", "/api/drives", {})
        self.assertEqual(status, 202)
        self.assertTrue(data["ok"])
        snapshot = self.wait_idle()
        self.assertIsInstance(snapshot["drives"], list)


if __name__ == "__main__":
    unittest.main()
