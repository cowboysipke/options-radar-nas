import http.client
import json
import os
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
            "gex": callback("gex", {"status": "empty", "symbol": "NVDA"}),
            "dealer": callback("dealer", {"status": "ok", "symbol": "NVDA"}),
            "spark": callback("spark", {"status": "ok", "symbol": "NVDA", "bars": []}),
            "provider_status": callback("provider_status", {"provider": "ibkr", "quality": "realtime"}),
            "provider_action": callback("provider_action", {"status": "ok"}),
            "market_provenance": callback("market_provenance", {"provider": "ibkr"}),
            "market_compare": callback("market_compare", {"conflict": False}),
            "ibkr_discover": callback("ibkr_discover", {"ports": [7497]}),
            "ibkr_sync": callback("ibkr_sync", {"status": "ok", "positions": 2}),
            "futu_sync": callback("futu_sync", {"status": "ok", "synced": 4}),
            "feishu_test": callback("feishu_test", {"status": "queued", "queue_id": "q1"}),
            "gex_snapshot": callback("gex_snapshot", {"status": "ok", "rows": 3}),
            "discord_login": callback("discord_login", {"status": "login_required", "message": "请登录"}),
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

    def test_dealer_and_spark_get_apis_forward_symbol(self):
        for path, name in (("/api/dealer", "dealer"), ("/api/spark", "spark")):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path + "?symbol=nvda", headers={"Cookie": self.cookie})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["symbol"], "NVDA")
                self.assertIn((name, {"symbol": "nvda"}), self.calls)

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

    def test_gex_snapshot_action_requires_csrf_and_runs(self):
        headers = {"Cookie": self.cookie, "X-CSRF-Token": self.csrf}
        status, _, body = self.request("POST", "/api/actions/gex-snapshot", None, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["rows"], 3)
        self.assertIn(("gex_snapshot", {}), self.calls)
        # Without CSRF the action is rejected.
        no_csrf = {"Cookie": self.cookie}
        status, _, body = self.request("POST", "/api/actions/gex-snapshot", None, no_csrf)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["status"], "forbidden")

    def test_setup_page_exposes_futu_opend_login_and_verification(self):
        status, _, page = self.request("GET", "/setup", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn("富途 OpenD", page)
        self.assertIn('name="futu_user_id"', page)
        self.assertIn('name="futu_login_password"', page)
        self.assertIn('/futu/send-code', page)
        self.assertIn('/futu/submit-code', page)
        self.assertIn('手机验证码', page)
        self.assertIn('/futu/import-watchlist', page)
        self.assertIn("API配置", page)
        self.assertIn("DeepSeek API Key", page)
        self.assertIn("飞书 App Secret", page)
        self.assertIn("Massive API Key", page)
        self.assertIn("打开Discord登录", page)

    def test_setup_page_exposes_discord_poll_settings(self):
        status, _, page = self.request("GET", "/setup", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn('name="poll_minutes"', page)
        self.assertIn('name="session_start_hour"', page)
        self.assertIn('name="session_end_hour"', page)

    def test_config_export_import_rewrites_paths_and_writes_secrets(self):
        os.environ["DATA_DIR"] = self.temp.name
        try:
            # 导出当前配置（含 config 与 secrets 两个字段）
            status, _, body = self.request("GET", "/api/config/export", headers={"Cookie": self.cookie})
            self.assertEqual(status, 200)
            bundle = json.loads(body)
            self.assertIn("config", bundle)
            self.assertIn("secrets", bundle)

            # 构造一个带桌面路径 + 密钥的配置包，模拟桌面版 → NAS 迁移
            import_bundle = {
                "config": {
                    "timezone": "Asia/Shanghai",
                    "database_path": "data-local/options_radar.db",
                    "evidence_dir": "data-local/evidence",
                    "discord": {"source_server": "Alpha夜航社", "channel_names": {"flow": "异常期权"}},
                    "futu": {"user_id": "13548899395", "host": "127.0.0.1", "port": 11111},
                    "providers": {"market_priority": ["futu"]},
                    "secret_refs": {"deepseek_api_key": "E:\\data-local\\secrets\\deepseek_api_key"},
                    "setup_completed": True,
                },
                "secrets": {"deepseek_api_key": "sk-test-123"},
            }
            status, _, body = self.request(
                "POST", "/api/config/import",
                body=json.dumps(import_bundle),
                headers={"Cookie": self.cookie, "X-CSRF-Token": self.csrf, "Content-Type": "application/json"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["status"], "ok")

            # 路径被重写到容器布局
            data = self.store.load()
            self.assertEqual(data["database_path"], str(Path(self.temp.name) / "options_radar.db"))
            self.assertEqual(data["evidence_dir"], str(Path(self.temp.name) / "evidence"))
            self.assertEqual(data["futu"]["user_id"], "13548899395")
            ref = data["secret_refs"]["deepseek_api_key"]
            self.assertEqual(ref, str(Path(self.temp.name) / "secrets" / "deepseek_api_key"))
            # 密钥文件被写入
            self.assertTrue(Path(ref).is_file())
            self.assertEqual(Path(ref).read_text(encoding="utf-8").strip(), "sk-test-123")
        finally:
            os.environ.pop("DATA_DIR", None)

    def test_poll_schedule_parsers_validate(self):
        from options_radar.setup_server import _hour, _minutes
        self.assertEqual(_minutes("10"), 10)
        with self.assertRaises(ValueError):
            _minutes("0")
        with self.assertRaises(ValueError):
            _minutes("61")
        self.assertEqual(_hour("23"), 23)
        with self.assertRaises(ValueError):
            _hour("24")
        with self.assertRaises(ValueError):
            _hour("-1")

    def test_json_serializes_datetime_results(self):
        from datetime import datetime
        self.server.app.callbacks["backup"] = lambda _payload: {"status": "ok", "at": datetime(2026, 8, 12)}
        payload = json.dumps({}).encode("utf-8")
        headers = {
            "Cookie": self.cookie,
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "X-CSRF-Token": self.csrf,
        }
        status, _, body = self.request("POST", "/api/actions/backup", payload, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["at"], "2026-08-12 00:00:00")

    def test_system_page_offers_feishu_test(self):
        status, _, page = self.request("GET", "/system", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn('/api/actions/feishu-test', page)
        self.assertIn('/api/providers/massive/test', page)
        self.assertIn('/api/actions/deepseek-test', page)

    def test_provider_page_has_chinese_navigation_and_one_click_actions(self):
        status, _, page = self.request("GET", "/providers", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn("数据源诊断", page)
        self.assertIn("富途 OpenD", page)
        self.assertIn("Alpaca", page)
        self.assertIn("Massive", page)
        self.assertIn('/api/providers/futu/test', page)
        # IBKR is kept only for position sync, not as a market-data provider.
        self.assertNotIn('/api/providers/ibkr/test', page)
        self.assertIn('/api/ibkr/sync', page)
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
