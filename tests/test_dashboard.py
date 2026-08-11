import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from options_radar.setup_server import SetupConfigStore, create_setup_server


class DashboardHttpTests(unittest.TestCase):
    TOKEN = "0123456789abcdef01234567"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SetupConfigStore(Path(self.temp.name) / "config.yaml")
        self.store.initialize()
        self.calls = []

        def callback(name, result):
            def invoke(payload):
                self.calls.append((name, dict(payload)))
                return result
            return invoke

        callbacks = {
            "status": callback("status", {"status": "ok", "opend": "READY"}),
            "futu_status": callback("futu_status", {"status": "ready", "quote": "实时"}),
            "recommendations": callback("recommendations", {"items": [{"symbol": "SAP", "score": 82}]}),
            "contracts": callback("contracts", {"items": []}),
            "rules": callback("rules", {"version": 3, "title": "使用指南"}),
            "portfolio": callback("portfolio", {"watchlist": ["SAP"]}),
            "analysts": callback("analysts", {"pa": {"weight": 1.0}}),
            "backtest": callback("backtest", {"win_rate": 0.61}),
            "futu_sync": callback("futu_sync", {"status": "ok", "synced": 4}),
            "futu_send_verification": callback("futu_send_verification", {"status": "sent"}),
        }
        self.server = create_setup_server(
            "127.0.0.1", 0, self.store, self.TOKEN,
            health=lambda: {"status": "ok"}, callbacks=callbacks,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.cookie, self.csrf = self._login()

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

    def _login(self):
        body = urlencode({"token": self.TOKEN})
        status, headers, _ = self.request(
            "POST", "/login", body,
            {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))},
        )
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, page = self.request("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        return cookie, csrf

    def test_all_chinese_pages_are_authenticated_and_utf8(self):
        pages = {
            "/": "今日概览",
            "/setup": "一次性配置",
            "/contracts": "合约与推荐",
            "/rules": "规则库",
            "/portfolio": "富途组合",
            "/analysts": "分析师",
            "/backtest": "回测",
            "/system": "系统",
        }
        for path, text in pages.items():
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path, headers={"Cookie": self.cookie})
                self.assertEqual(status, 200)
                self.assertIn("charset=utf-8", headers["Content-Type"])
                self.assertIn('<meta charset="utf-8">', body)
                self.assertIn(text, body)
        status, _, body = self.request("GET", "/rules")
        self.assertEqual(status, 200)
        self.assertIn("管理口令", body)
        self.assertNotIn("使用指南", body)

    def test_json_apis_require_login_and_return_unicode(self):
        status, _, body = self.request("GET", "/api/futu/status")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["status"], "unauthorized")
        status, headers, body = self.request("GET", "/api/futu/status", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn("application/json; charset=utf-8", headers["Content-Type"])
        self.assertEqual(json.loads(body)["quote"], "实时")

    def test_post_callback_requires_csrf_and_forwards_payload(self):
        payload = json.dumps({"symbols": ["SAP"]}, ensure_ascii=False).encode("utf-8")
        headers = {"Cookie": self.cookie, "Content-Type": "application/json", "Content-Length": str(len(payload))}
        status, _, body = self.request("POST", "/api/futu/sync", payload, headers)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["status"], "forbidden")

        headers["X-CSRF-Token"] = self.csrf
        status, _, body = self.request("POST", "/api/futu/sync", payload, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["synced"], 4)
        self.assertIn(("futu_sync", {"symbols": ["SAP"]}), self.calls)

    def test_browser_futu_action_returns_setup_page_with_feedback(self):
        body = urlencode({"csrf": self.csrf})
        status, headers, page = self.request(
            "POST", "/futu/send-code", body,
            {
                "Cookie": self.cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(body)),
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("charset=utf-8", headers["Content-Type"])
        self.assertIn("一次性配置", page)
        self.assertIn("操作已完成", page)
        self.assertIn(("futu_send_verification", {}), self.calls)


if __name__ == "__main__":
    unittest.main()
