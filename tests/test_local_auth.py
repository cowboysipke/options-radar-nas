import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from options_radar.local_runtime import LocalRuntime
from options_radar.setup_server import SetupConfigStore, create_setup_server


class LocalAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        store = SetupConfigStore(Path(self.temp.name) / "config.yaml")
        store.initialize()
        self.server = create_setup_server(
            "127.0.0.1", 0, store, "", health=lambda: {"status": "ok"},
            local_mode=True,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read().decode("utf-8")
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result

    def test_loopback_is_automatically_authenticated(self):
        status, headers, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("今日推荐", page)
        self.assertIn("版本 v2-local", page)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["build"]["mode"], "local")

    def test_health_contains_build_identity(self):
        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["build"]["mode"], "local")

    def test_port_probe_accepts_free_ephemeral_port(self):
        LocalRuntime._assert_port_available("127.0.0.1", 0)

    def test_port_probe_rejects_existing_dashboard(self):
        with self.assertRaisesRegex(RuntimeError, "端口"):
            LocalRuntime._assert_port_available("127.0.0.1", self.port)


if __name__ == "__main__":
    unittest.main()
