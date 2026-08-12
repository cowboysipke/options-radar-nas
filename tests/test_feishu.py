import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from options_radar.feishu import (
    FeishuBot,
    FeishuCallbacks,
    FeishuConfigurationError,
    FeishuStore,
    RETRY_DELAYS_SECONDS,
    build_card,
    load_feishu_credentials,
    parse_command,
)


def event(message_id, text, chat_id="oc_test"):
    return {
        "event": {
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            "sender": {"sender_id": {"open_id": "ou_test"}},
        }
    }


class MutableClock:
    def __init__(self, value=1_000.0):
        self.value = value

    def __call__(self):
        return self.value


class FeishuTests(unittest.TestCase):
    def callbacks(self, calls):
        return FeishuCallbacks(
            today_recommendations=lambda: calls.append(("today", None)) or [{"symbol": "AAPL", "score": 91}],
            positions=lambda: calls.append(("positions", None)) or [{"symbol": "TSLA", "quantity": 2}],
            add_watchlist=lambda symbol: calls.append(("add", symbol)) or "已添加",
            remove_watchlist=lambda symbol: calls.append(("remove", symbol)) or "已删除",
            explain_rank=lambda rank: calls.append(("explain", rank)) or "共识强",
            free_chat=lambda question: calls.append(("chat", question)) or "回答",
        )

    def test_import_has_no_lark_dependency(self):
        # Importing/reloading the transport must not trigger an SDK import.
        before = set(sys.modules)
        importlib.reload(sys.modules["options_radar.feishu"])
        newly_loaded = set(sys.modules) - before
        self.assertFalse(any(name == "lark_oapi" or name.startswith("lark_oapi.") for name in newly_loaded))

    def test_credentials_environment_then_docker_secret(self):
        credentials = load_feishu_credentials({"FEISHU_APP_ID": "cli_a", "FEISHU_APP_SECRET": "sensitive-value"})
        self.assertEqual(credentials.app_id, "cli_a")
        self.assertNotIn("sensitive-value", repr(credentials))
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "feishu_app_id").write_text("cli_file\n", encoding="utf-8")
            Path(directory, "feishu_app_secret").write_text("file_secret\n", encoding="utf-8")
            loaded = load_feishu_credentials({}, Path(directory))
            self.assertEqual((loaded.app_id, loaded.app_secret), ("cli_file", "file_secret"))
        with self.assertRaises(FeishuConfigurationError):
            load_feishu_credentials({}, Path("missing-secrets"))

    def test_fixed_chinese_commands(self):
        self.assertEqual(parse_command("今日推荐").name, "today")
        self.assertEqual(parse_command("持仓").name, "positions")
        self.assertEqual(parse_command("添加自选 aapl").argument, "AAPL")
        self.assertEqual(parse_command("删除自选 TSLA").argument, "TSLA")
        self.assertEqual(parse_command("解释第 12 名").argument, 12)
        self.assertEqual(parse_command("重新同步富途").name, "sync_futu")
        self.assertEqual(parse_command("系统状态").name, "system_status")
        self.assertEqual(parse_command("绑定").name, "bind")
        free = parse_command("英伟达现在贵吗？")
        self.assertEqual((free.name, free.argument), ("free_chat", "英伟达现在贵吗？"))

    def test_bind_persists_sender_and_dashboard_test_queues_card(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = FeishuBot(self.callbacks([]), Path(directory) / "state.db")
            self.assertTrue(bot.handle_event(event("bind-1", "绑定", chat_id="oc_private")))
            self.assertTrue(bot.process_one())
            self.assertEqual(bot.store.bound_sender_id(), "ou_test")
            self.assertEqual(bot.store.bound_chat_id(), "oc_private")
            # Consume the acknowledgement generated for the bind command.
            acknowledgement = bot.store.claim_outbox()
            self.assertIsNotNone(acknowledgement)
            result = bot.test_binding()
            self.assertEqual(result["status"], "queued")
            item = bot.store.claim_outbox()
            self.assertIsNotNone(item)
            self.assertEqual(item.receive_id, "oc_private")
            self.assertIn("飞书连接测试", item.content)
            bot.close()

    def test_fast_enqueue_deduplicates_before_callbacks_and_builds_cards(self):
        calls = []
        sent = []
        with tempfile.TemporaryDirectory() as directory:
            bot = FeishuBot(self.callbacks(calls), Path(directory) / "state.db", sender=sent.append)
            self.assertTrue(bot.handle_event(event("om_1", "今日推荐")))
            self.assertFalse(bot.handle_event(event("om_1", "今日推荐")))
            self.assertEqual(calls, [])
            self.assertEqual(bot.health()["inbox_pending"], 1)
            self.assertTrue(bot.process_one())
            self.assertEqual(calls, [("today", None)])
            self.assertTrue(bot.deliver_one())
            self.assertEqual(len(sent), 1)
            card = json.loads(sent[0].content)
            self.assertEqual(card["header"]["title"]["content"], "今日推荐")
            self.assertIn("AAPL", card["elements"][0]["content"])
            self.assertEqual(bot.health()["last_sent_at"][:4], str(time.gmtime().tm_year))
            bot.close()

    def test_all_callbacks_are_dispatched(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            bot = FeishuBot(self.callbacks(calls), Path(directory) / "state.db")
            texts = ("持仓", "添加自选 aapl", "删除自选 tsla", "解释第3名", "为什么？")
            for index, text in enumerate(texts):
                self.assertTrue(bot.handle_event(event("om_{}".format(index), text)))
            self.assertEqual(bot.process_pending(), len(texts))
            self.assertEqual(
                calls,
                [("positions", None), ("add", "AAPL"), ("remove", "TSLA"), ("explain", 3), ("chat", "为什么？")],
            )
            bot.close()

    def test_persistent_outbox_stable_uuid_and_retry_schedule(self):
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = FeishuStore(path, clock=clock)
            message_uuid = store.enqueue_outbox("om_retry", "oc_test", build_card("标题", "正文"))
            self.assertEqual(message_uuid, store.stable_uuid("om_retry"))
            # Re-enqueue of the same source/ordinal is idempotent.
            self.assertEqual(store.enqueue_outbox("om_retry", "oc_test", build_card("标题", "正文")), message_uuid)
            for delay in RETRY_DELAYS_SECONDS:
                item = store.claim_outbox()
                self.assertIsNotNone(item)
                self.assertEqual(item.message_uuid, message_uuid)
                actual = store.mark_delivery_failed(item, RuntimeError("temporary"))
                self.assertEqual(actual, delay)
                row = store.outbox_row(message_uuid)
                self.assertEqual(row["next_attempt_at"], clock.value + delay)
                self.assertIsNone(store.claim_outbox())
                clock.value += delay
            final_item = store.claim_outbox()
            self.assertIsNotNone(final_item)
            self.assertIsNone(store.mark_delivery_failed(final_item, RuntimeError("permanent")))
            self.assertEqual(store.outbox_row(message_uuid)["status"], "DEAD")
            store.close()

            # Rows survive reopening and UUID derivation stays stable.
            reopened = FeishuStore(path, clock=clock)
            self.assertEqual(reopened.outbox_row(message_uuid)["message_uuid"], message_uuid)
            reopened.close()

    def test_health_degrades_for_dead_letter(self):
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            bot = FeishuBot(self.callbacks([]), Path(directory) / "state.db", sender=lambda item: None, clock=clock)
            self.assertEqual(bot.health()["status"], "stopped")
            identifier = bot.store.enqueue_outbox("om_dead", "oc_test", build_card("x", "y"))
            for delay in RETRY_DELAYS_SECONDS:
                item = bot.store.claim_outbox()
                bot.store.mark_delivery_failed(item, RuntimeError("x"))
                clock.value += delay
            item = bot.store.claim_outbox()
            bot.store.mark_delivery_failed(item, RuntimeError("x"))
            health = bot.health()
            self.assertEqual(health["status"], "degraded")
            self.assertEqual(health["outbox_dead"], 1)
            self.assertEqual(bot.store.outbox_row(identifier)["attempts"], 4)
            bot.close()


if __name__ == "__main__":
    unittest.main()
