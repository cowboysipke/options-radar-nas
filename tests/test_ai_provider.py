import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from options_radar.ai_provider import DeepSeekProvider


def response(payload, prompt_tokens=100, completion_tokens=30):
    return {
        "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class DeepSeekProviderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "radar.db"
        self.env = patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "test-only-secret",
                "DEEPSEEK_FLASH_MODEL": "deepseek-v4-flash",
                "DEEPSEEK_PRO_MODEL": "deepseek-v4-pro",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tempdir.cleanup()

    def test_extract_validates_and_caches(self):
        calls = []

        def transport(url, payload, headers, timeout):
            calls.append((url, payload, headers, timeout))
            return response({
                "decision": "trade",
                "direction": "bull",
                "confidence": 0.78,
                "reason_summary": ["突破确认"],
                "risk_summary": ["临近财报"],
                "underlying_entry": 100.0,
                "underlying_target": 108.0,
                "underlying_stop": 97.0,
                "evidence_positions": [{"field": "decision", "quote": "执行观点 交易"}],
            })

        provider = DeepSeekProvider(self.db_path, transport=transport)
        first = provider.extract_signal("执行观点 交易；方向看多")
        second = provider.extract_signal("执行观点 交易；方向看多")

        self.assertFalse(first.ai_degraded)
        self.assertEqual(first.data.direction, "BULL")
        self.assertTrue(second.cached)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["response_format"], {"type": "json_object"})
        self.assertEqual(calls[0][2]["Authorization"], "Bearer test-only-secret")

    def test_invalid_json_retries_once_then_degrades(self):
        calls = []

        def transport(url, payload, headers, timeout):
            calls.append(payload)
            return response({"score": 99})

        provider = DeepSeekProvider(self.db_path, transport=transport)
        result = provider.extract_signal("自由文本")
        self.assertTrue(result.ai_degraded)
        self.assertEqual(result.reason, "schema_validation_failed")
        self.assertEqual(len(calls), 2)

    def test_pro_routing_limit_falls_back_to_flash(self):
        models = []

        def transport(url, payload, headers, timeout):
            models.append(payload["model"])
            return response({"text": "摘要"})

        with patch.dict(os.environ, {"DEEPSEEK_PRO_DAILY_LIMIT": "1"}, clear=False):
            provider = DeepSeekProvider(self.db_path, transport=transport)
            one = provider.summarize({"disagreement": True, "contract": "A"})
            two = provider.summarize({"disagreement": True, "contract": "B"})
        self.assertFalse(one.ai_degraded)
        self.assertFalse(two.ai_degraded)
        self.assertEqual(models, ["deepseek-v4-pro", "deepseek-v4-flash"])

    def test_budget_warning_and_exhaustion(self):
        def transport(url, payload, headers, timeout):
            return response({"text": "说明"}, prompt_tokens=1_000_000, completion_tokens=0)

        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_MONTHLY_BUDGET_CNY": "1",
                "DEEPSEEK_FLASH_INPUT_CNY_PER_M": "1",
            },
            clear=False,
        ):
            provider = DeepSeekProvider(self.db_path, transport=transport)
            first = provider.answer("为什么", {"symbol": "SAP"})
            health = provider.health()
            second = provider.answer("新的问题", {"symbol": "EGO"})
        self.assertFalse(first.ai_degraded)
        self.assertTrue(health["budget"]["warning_80_percent"])
        self.assertTrue(health["budget"]["exhausted"])
        self.assertTrue(second.ai_degraded)
        self.assertEqual(second.reason, "monthly_budget_exhausted")

    def test_sensitive_context_is_redacted(self):
        captured = {}

        def transport(url, payload, headers, timeout):
            captured.update(payload)
            return response({"text": "已说明"})

        provider = DeepSeekProvider(self.db_path, transport=transport)
        result = provider.answer(
            "风险如何",
            {
                "symbol": "SAP",
                "account_id": "U1234567",
                "note": "account U7654321 concentrated 12%",
                "api_key": "secret-value",
            },
        )
        self.assertFalse(result.ai_degraded)
        outbound = json.dumps(captured, ensure_ascii=False)
        self.assertNotIn("secret-value", outbound)
        self.assertNotIn("U1234567", outbound)
        self.assertNotIn("U7654321", outbound)
        self.assertIn("SAP", outbound)

    def test_missing_api_key_is_rule_only_mode(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            provider = DeepSeekProvider(self.db_path, transport=lambda *args: {})
            result = provider.extract_signal("分析文本")
            health = provider.health()
        self.assertTrue(result.ai_degraded)
        self.assertEqual(result.reason, "api_key_missing")
        self.assertEqual(health["status"], "disabled")


if __name__ == "__main__":
    unittest.main()

