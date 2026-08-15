import http.client
import hashlib
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

import yaml

from options_radar.nas_runtime import NasRuntime, create_backup
from options_radar.opend_manager import OpenDState, OpenDStatus
from options_radar.setup_server import SetupConfigStore, create_setup_server


class SetupConfigTests(unittest.TestCase):
    def test_existing_config_receives_rule_channel_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "discord:\n  channel_names:\n    flow: old-flow\n    pa: old-pa\n"
                "    mr: old-mr\n    qmr: old-qmr\n    fpd: old-fpd\n",
                encoding="utf-8",
            )
            loaded = SetupConfigStore(path).load()
            self.assertEqual(loaded["discord"]["channel_names"]["flow"], "old-flow")
            self.assertEqual(loaded["discord"]["channel_names"]["fpd"], "old-fpd")
            self.assertEqual(loaded["discord"]["channel_names"]["newsfeed"], "newsfeed")

    def test_initializes_and_updates_only_whitelisted_non_secret_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            store = SetupConfigStore(path)
            initial = store.initialize()
            self.assertFalse(initial["setup_completed"])
            form = {
                "timezone": "Asia/Shanghai",
                "discord_server": "Alpha",
                "flow_channel": "flow-room",
                "pa_channel": "pa-room",
                "mr_channel": "mr-room",
                "qmr_channel": "qmr-room",
                "fpd_channel": "fpd-room",
                "newsfeed_channel": "newsfeed-room",
                "feishu_app_id": "cli_123",
                "ibkr_query_id": "456",
                "flash_model": "deepseek-chat",
                "pro_model": "deepseek-reasoner",
                "report_delay": "75",
                "deepseek_api_key": "must-not-be-saved",
                "secret_refs.deepseek_api_key": "/tmp/attacker",
            }
            saved = store.update_from_form(form)
            self.assertTrue(saved["setup_completed"])
            self.assertEqual(saved["discord"]["source_channels"]["flow-room"], "flow")
            self.assertEqual(saved["discord"]["source_channels"]["fpd-room"], "fpd")
            self.assertEqual(saved["discord"]["source_channels"]["newsfeed-room"], "newsfeed")
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("must-not-be-saved", text)
            self.assertNotIn("/tmp/attacker", text)
            self.assertEqual(saved["secret_refs"]["deepseek_api_key"], str(Path(directory) / "secrets" / "deepseek_api_key"))
            self.assertEqual(
                (Path(directory) / "secrets" / "deepseek_api_key").read_text(encoding="utf-8"),
                "must-not-be-saved",
            )

    def test_futu_password_is_immediately_stored_as_protocol_md5_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SetupConfigStore(Path(directory) / "config.yaml")
            form = {
                "timezone": "Asia/Shanghai", "discord_server": "Alpha",
                "flow_channel": "flow", "pa_channel": "pa", "mr_channel": "mr",
                "qmr_channel": "qmr", "fpd_channel": "fpd", "newsfeed_channel": "newsfeed",
                "feishu_app_id": "cli_123",
                "futu_user_id": "10001", "flash_model": "deepseek-chat",
                "pro_model": "deepseek-reasoner", "report_delay": "75",
                "futu_login_password": "login-secret",
            }
            saved = store.update_from_form(form)
            config_text = store.path.read_text(encoding="utf-8")
            credential = Path(saved["secret_refs"]["futu_login_password_md5"]).read_text(encoding="utf-8")
            self.assertNotIn("login-secret", config_text)
            self.assertNotEqual(credential, "login-secret")
            self.assertEqual(credential, hashlib.md5(b"login-secret").hexdigest())

    def test_rejects_duplicate_discord_channel_names(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SetupConfigStore(Path(directory) / "config.yaml")
            values = {field: "same" for field in (
                "discord_server", "flow_channel", "pa_channel", "mr_channel", "qmr_channel", "fpd_channel",
                "newsfeed_channel",
                "feishu_app_id", "ibkr_query_id",
            )}
            values.update(timezone="UTC", flash_model="model-a", pro_model="model-b", report_delay="75")
            with self.assertRaisesRegex(ValueError, "不可重复"):
                store.update_from_form(values)


class SetupHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SetupConfigStore(Path(self.temp.name) / "config.yaml")
        self.store.initialize()
        self.server = create_setup_server("127.0.0.1", 0, self.store, "0123456789abcdef01234567")
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

    def test_health_is_minimal_and_setup_requires_login(self):
        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertIn('"configured":false', body)
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("管理口令", body)
        self.assertNotIn("DeepSeek日常模型", body)

    def test_login_sets_http_only_session_cookie(self):
        body = urlencode({"token": "0123456789abcdef01234567"})
        status, headers, _ = self.request(
            "POST", "/login", body, {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))}
        )
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)


class BackupTests(unittest.TestCase):
    def test_backup_uses_sqlite_backup_api_and_includes_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                "root": root,
                "config": root / "config.yaml",
                "database": root / "options_radar.db",
                "browser": root / "browser-profile",
                "evidence": root / "evidence",
                "strategies": root / "strategies",
                "backups": root / "backups",
            }
            paths["config"].write_text("timezone: UTC\n", encoding="utf-8")
            connection = sqlite3.connect(paths["database"])
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('ok')")
            connection.commit()
            connection.close()
            archive = create_backup(paths)
            self.assertTrue(archive.exists())
            self.assertGreater(archive.stat().st_size, 0)


def runtime_paths(root):
    return {
        "root": root,
        "config": root / "config.yaml",
        "database": root / "options_radar.db",
        "browser": root / "browser-profile",
        "evidence": root / "evidence",
        "strategies": root / "strategies",
        "backups": root / "backups",
    }


class FakeOpenDManager:
    def __init__(self, root):
        self.root = Path(root)
        self.config_path = self.root / "opend-profile" / "FutuOpenD.xml"
        self.running = False
        self.calls = []

    def install(self):
        self.calls.append(("install",))

    def write_config(self, account, digest):
        self.calls.append(("write_config", account, digest))
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text("configured", encoding="utf-8")

    def start(self, monitor=True):
        self.calls.append(("start", monitor))
        self.running = True
        return self.status()

    def stop(self):
        self.calls.append(("stop",))
        self.running = False

    def status(self):
        return OpenDStatus(
            OpenDState.READY if self.running else OpenDState.STOPPED,
            "fixture", self.running, 42 if self.running else None, 0, "ready", False,
        )

    def request_phone_code(self):
        self.calls.append(("request_phone",))
        return "sent"

    def submit_phone_code(self, code):
        self.calls.append(("submit_phone", code))
        return "accepted"

    def request_captcha(self):
        self.calls.append(("request_captcha",))
        return self.root / "missing-captcha.png"

    def submit_captcha(self, code):
        self.calls.append(("submit_captcha", code))
        return "accepted"

    def relogin(self):
        self.calls.append(("relogin",))
        return "accepted"


class RuntimeOpenDIntegrationTests(unittest.TestCase):
    @staticmethod
    def configure(runtime):
        config = runtime.store.load()
        config["setup_completed"] = True
        config["futu"]["user_id"] = "10001"
        config["secret_refs"] = runtime.store.fixed_secret_refs()
        secret = Path(config["secret_refs"]["futu_login_password_md5"])
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("a" * 32, encoding="utf-8")
        runtime.store.save(config)

    def test_setup_credentials_install_configure_start_and_health(self):
        with tempfile.TemporaryDirectory() as directory:
            created = []

            def factory(root):
                manager = FakeOpenDManager(root)
                created.append(manager)
                return manager

            runtime = NasRuntime(runtime_paths(Path(directory)), opend_manager_factory=factory)
            self.configure(runtime)
            runtime._ensure_opend()
            runtime._ensure_opend()
            manager = created[0]
            self.assertIn(("write_config", "10001", "a" * 32), manager.calls)
            self.assertEqual(manager.calls.count(("start", True)), 1)
            self.assertEqual(runtime.health()["opend"]["state"], "ready")
            runtime.stop()
            self.assertIn(("stop",), manager.calls)

    def test_dashboard_verification_callbacks_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = NasRuntime(runtime_paths(Path(directory)), opend_manager_factory=FakeOpenDManager)
            self.configure(runtime)
            runtime._ensure_opend()
            callbacks = runtime.dashboard_callbacks()
            self.assertEqual(callbacks["futu_send_verification"]({})["type"], "phone")
            submitted = callbacks["futu_submit_verification"]({
                "verification_code": "123456", "captcha_code": "AB12",
            })
            self.assertEqual(submitted["status"], "submitted")
            self.assertEqual(callbacks["futu_relogin"]({})["status"], "submitted")
            archive = callbacks["backup"]({})["archive"]
            self.assertTrue(Path(archive).is_file())
            self.assertIn(("submit_phone", "123456"), runtime.opend_manager.calls)
            self.assertIn(("submit_captcha", "AB12"), runtime.opend_manager.calls)

    def test_component_callbacks_are_generic_and_service_independent(self):
        class Component:
            def __init__(self, **_kwargs):
                self.started = False

            def start(self):
                self.started = True

            def collect(self, backfill=False):
                return {"collected": True, "backfill": backfill}

            def positions(self):
                return {"positions": 2}

            def stop(self):
                self.started = False

        with tempfile.TemporaryDirectory() as directory:
            runtime = NasRuntime(
                runtime_paths(Path(directory)),
                opend_manager_factory=FakeOpenDManager,
                component_factory=lambda **kwargs: Component(**kwargs),
            )
            self.configure(runtime)
            callbacks = runtime.dashboard_callbacks()
            self.assertEqual(callbacks["collect"]({"backfill": True})["backfill"], True)
            self.assertEqual(callbacks["portfolio"]({}), {"positions": 2})
            self.assertTrue(runtime.component.started)
            runtime.stop()


if __name__ == "__main__":
    unittest.main()
