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
            "providers": callback("providers", {"items": [{"provider": "ibkr", "connected": True}]}),
            "signals": callback("signals", []),
            "provider_status": callback("provider_status", {"provider": "ibkr", "quality": "realtime"}),
            "provider_action": callback("provider_action", {"status": "ok"}),
            "market_provenance": callback("market_provenance", {"provider": "ibkr"}),
            "market_compare": callback("market_compare", {"conflict": False}),
            "ibkr_discover": callback("ibkr_discover", {"ports": [7497]}),
            "ibkr_sync": callback("ibkr_sync", {"status": "ok", "positions": 2}),
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
            "/": "今日推荐",
            "/setup": "本地配置",
            "/signals": "信号明细",
            "/portfolio": "自选与持仓",
            "/providers": "数据源诊断",
            "/system": "系统诊断",
        }
        for path, text in pages.items():
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path, headers={"Cookie": self.cookie})
                self.assertEqual(status, 200)
                self.assertIn("charset=utf-8", headers["Content-Type"])
                self.assertIn('<meta charset="utf-8">', body)
                self.assertIn(text, body)
        status, _, body = self.request("GET", "/signals")
        self.assertEqual(status, 200)
        self.assertIn("信号明细", body)

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

    def test_provider_page_has_chinese_navigation_and_one_click_actions(self):
        status, _, page = self.request("GET", "/providers", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn("数据源诊断", page)
        self.assertIn("IBKR", page)
        self.assertIn("Massive", page)
        self.assertIn('/api/providers/ibkr/test', page)
        self.assertIn(("providers", {}), self.calls)

    def test_dynamic_provider_status_and_provenance_routes(self):
        status, _, body = self.request(
            "GET", "/api/providers/ibkr/status?detail=1", headers={"Cookie": self.cookie},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["quality"], "realtime")
        self.assertIn(("provider_status", {"detail": "1", "provider": "ibkr"}), self.calls)

        status, _, body = self.request(
            "GET", "/api/market/provenance/AAPL%2020260116C00200000", headers={"Cookie": self.cookie},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["provider"], "ibkr")
        self.assertIn(
            ("market_provenance", {"contract_key": "AAPL 20260116C00200000"}), self.calls,
        )

    def test_provider_and_market_post_routes_keep_csrf_and_route_payload(self):
        payload = json.dumps({"priority": 2}).encode("utf-8")
        headers = {
            "Cookie": self.cookie,
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "X-CSRF-Token": self.csrf,
        }
        status, _, _ = self.request("POST", "/api/providers/ibkr/priority", payload, headers)
        self.assertEqual(status, 200)
        self.assertIn(
            ("provider_action", {"priority": 2, "provider": "ibkr", "action": "priority"}), self.calls,
        )

        status, _, _ = self.request(
            "POST", "/api/market/compare/SPY%2020260116P00500000", payload, headers,
        )
        self.assertEqual(status, 200)
        self.assertIn(
            ("market_compare", {"priority": 2, "contract_key": "SPY 20260116P00500000"}), self.calls,
        )

        no_csrf = dict(headers)
        no_csrf.pop("X-CSRF-Token")
        status, _, body = self.request("POST", "/api/ibkr/discover", payload, no_csrf)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["status"], "forbidden")
        status, _, body = self.request("POST", "/api/ibkr/sync", payload, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["positions"], 2)


if __name__ == "__main__":
    unittest.main()
