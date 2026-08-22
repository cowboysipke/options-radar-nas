# -*- coding: utf-8 -*-
"""Strategy matrix, short-gamma plan and expiration guidance (doc 9.1-9.3)."""
import unittest

from options_radar.service import OptionsRadarService


class StrategyHintTests(unittest.TestCase):
    def hint(self, direction, iv_rank, regime=None, event_risk="LOW", trend=None):
        return OptionsRadarService._strategy_hint(
            direction, iv_rank, regime, event_risk=event_risk, trend=trend,
        )

    def test_expensive_positive_gamma_sells_directionally(self):
        self.assertIn("Sell Put（IV 贵 + 环境允许）", self.hint("BULL", 80, "positive"))
        self.assertIn("Sell Call（IV 贵 + 环境允许）", self.hint("BEAR", 80, "positive"))

    def test_expensive_negative_gamma_prefers_credit_spread(self):
        self.assertIn("Credit Spread 保护", self.hint("BULL", 80, "negative"))
        self.assertIn("Credit Spread 保护", self.hint("BEAR", 75, "negative"))

    def test_cheap_iv_uptrend_goes_buy_stock(self):
        self.assertIn("Buy Stock", self.hint("BULL", 20, "positive", trend=1))

    def test_cheap_iv_uptrend_with_earnings_uses_spread(self):
        hint = self.hint("BULL", 20, "positive", event_risk="HIGH", trend=1)
        self.assertIn("Buy Stock / Bull Call Spread", hint)
        self.assertIn("防财报", hint)

    def test_cheap_iv_no_trend_or_bear_waits(self):
        self.assertIn("观望", self.hint("BULL", 20, "positive", trend=-1))
        self.assertIn("观望", self.hint("BEAR", 20, "positive", trend=1))

    def test_mixed_gamma_or_no_direction_means_no_trade(self):
        self.assertIn("不交易", self.hint("BULL", 80, "mixed"))
        self.assertIn("不交易", self.hint("NEUTRAL", 80, "positive"))

    def test_mid_iv_is_neutral(self):
        self.assertIn("IV 中性", self.hint("BULL", 50, "positive"))


class ShortGammaPlanTests(unittest.TestCase):
    def test_high_blocks_naked_short(self):
        plan = OptionsRadarService._short_gamma_plan("HIGH")
        self.assertIn("Naked Short Call BLOCK", plan)
        self.assertIn("Naked Short Put BLOCK", plan)
        self.assertIn("Credit Spread CAUTION", plan)
        self.assertIn("Defined Risk PREFERRED", plan)

    def test_medium_cautions(self):
        plan = OptionsRadarService._short_gamma_plan("MEDIUM")
        self.assertIn("CAUTION", plan)
        self.assertIn("PREFERRED", plan)
        self.assertNotIn("BLOCK", plan)

    def test_low_is_empty(self):
        self.assertEqual(OptionsRadarService._short_gamma_plan("LOW"), "")


class ExpirationHintTests(unittest.TestCase):
    def hint(self, dte, event_risk="LOW", next_earnings=None):
        return OptionsRadarService._expiration_hint(dte, event_risk, next_earnings)

    def test_high_event_risk_avoids_earnings_week(self):
        self.assertIn("财报后一期", self.hint(20, "HIGH", 3))
        self.assertIn("10 天后财报", self.hint(20, "HIGH", 10))

    def test_dte_bands(self):
        self.assertIn("本周", self.hint(5))
        self.assertIn("下周", self.hint(12))
        self.assertIn("2W~1M", self.hint(20))
        self.assertIn("1M+", self.hint(50))

    def test_missing_dte(self):
        self.assertIsNone(self.hint(None))


if __name__ == "__main__":
    unittest.main()
