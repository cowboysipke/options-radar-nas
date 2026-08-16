import unittest
from datetime import datetime

from options_radar.models import RawMessage
from options_radar.parser import parse_analyst_message, parse_flow_message


class ParserTests(unittest.TestCase):
    def test_raw_flow(self):
        message = RawMessage(
            channel="异常期权", analyst="flow", observed_at=datetime(2026, 8, 5, 22, 51),
            content="EGO 45 C 2026-11-20 $123K AVG$1.85 107DTE Informational purposes only.",
        )
        event = parse_flow_message(message)
        self.assertIsNotNone(event)
        self.assertEqual(event.contract_key, "US.EGO|2026-11-20|45|C")
        self.assertEqual(event.premium, 123000)
        self.assertEqual(event.dte, 107)
        self.assertEqual(event.session_date.isoformat(), "2026-08-05")

    def test_pa_underlying_levels(self):
        message = RawMessage(
            channel="pa分析师", analyst="pa", observed_at=datetime(2026, 8, 5, 23, 27),
            content=("ALAB 2026-08-21 400C | 解读 Premium $5,531,342 | DTE - "
                     "执行观点 交易 bull 入场329.70 目标345.00 止损322.00 "
                     "价格结构 4h趋势向上，1h形成看涨反转 形态ii + two_bar_reversal "
                     "期权结构 偏向bull 置信5 执行计划 入场329.70 目标345.00 止损322.00 "
                     "失效条件 破位322.00 胜率55% 风险提示 风险3 置信5"),
        )
        signal = parse_analyst_message(message)
        self.assertEqual(signal.decision, "TRADE")
        self.assertEqual(signal.direction, "BULL")
        self.assertEqual(signal.underlying_entry, 329.70)
        self.assertEqual(signal.win_rate, 0.55)
        self.assertEqual(signal.risk_score, 3)

    def test_pa_real_format_colon_trade(self):
        """Real Discord text: 执行观点: 交易 (colon-separated)."""
        message = RawMessage(
            channel="fpd", analyst="fpd", observed_at=datetime(2026, 8, 10, 16, 15),
            content=("<@&1470429452256415745>\n"
                     "GOOG 2026-08-14 342.5C | 解读\n"
                     "Premium $1,125,799 | DTE -\n"
                     "执行观点: 交易 bull 入场340.60 目标345.00 止损338.50\n"
                     "价格结构: 短期下跌趋势，接近支撑位，出现看多期权流背离 形态看多背离（价格下跌+买入CALL）\n"
                     "期权结构: 偏向bull 置信5\n"
                     "执行计划: 入场340.60 目标345.00 止损338.50\n"
                     "失效条件: 破位338.50 胜率55%\n"
                     "风险提示: 风险4 置信5\n"
                     "Informational purposes only. Not financial advice."),
        )
        signal = parse_analyst_message(message)
        self.assertEqual(signal.decision, "TRADE")
        self.assertEqual(signal.direction, "BULL")
        self.assertEqual(signal.underlying_entry, 340.60)
        self.assertEqual(signal.underlying_target, 345.00)
        self.assertEqual(signal.underlying_stop, 338.50)
        self.assertEqual(signal.win_rate, 0.55)

    def test_pa_real_format_colon_no_trade(self):
        message = RawMessage(
            channel="pa", analyst="pa", observed_at=datetime(2026, 8, 10, 16, 15),
            content=("<@&1467764895855542467>\n"
                     "ONON 2026-09-25 26P | 解读\n"
                     "Premium $491,468 | DTE -\n"
                     "执行观点: 不交易 bear 入场- 目标- 止损-\n"
                     "价格结构: 1小时趋势为强空头（always_in_short）\n"
                     "期权结构: 偏向bear 置信1\n"
                     "Informational purposes only. Not financial advice."),
        )
        signal = parse_analyst_message(message)
        self.assertEqual(signal.decision, "NO_TRADE")
        self.assertEqual(signal.direction, "BEAR")

    def test_pa_real_format_colon_trade_wen(self):
        message = RawMessage(
            channel="pa", analyst="pa", observed_at=datetime(2026, 8, 11, 15, 0),
            content=("WEN 2026-09-18 9C | 解读\n"
                     "Premium $251,670 | DTE -\n"
                     "执行观点: 交易 bull 入场8.81 目标9.40 止损8.59\n"
                     "期权结构: 偏向bull 置信4\n"
                     "执行计划: 入场8.81 目标9.40 止损8.59\n"
                     "失效条件: 破位8.59 胜率65%\n"
                     "Informational purposes only. Not financial advice."),
        )
        signal = parse_analyst_message(message)
        self.assertEqual(signal.decision, "TRADE")
        self.assertEqual(signal.direction, "BULL")
        self.assertEqual(signal.underlying_entry, 8.81)
        self.assertEqual(signal.underlying_stop, 8.59)

    def test_mr_direction_is_inferred_only_for_trade(self):
        message = RawMessage(
            channel="mr分析师", analyst="mr", observed_at=datetime(2026, 8, 6, 0, 31),
            content="FCX 2026-08-14 69P | MR 解读 Premium $263,296 | DTE - decision trade confidence_score 4",
        )
        signal = parse_analyst_message(message)
        self.assertEqual(signal.direction, "BEAR")
        self.assertEqual(signal.direction_source, "inferred_from_contract")
        self.assertEqual(signal.analyst_family, "mean_reversion")

    def test_qmr_categorical_confidence(self):
        message = RawMessage(
            channel="qmr分析师", analyst="qmr", observed_at=datetime(2026, 8, 6, 0, 31),
            content=("FCX 2026-08-14 69P | QMR 解读 Premium $263,296 | DTE - "
                     "结论：交易 | 方向：偏空 | 信心：高 执行要点：结构偏向空头延续"),
        )
        signal = parse_analyst_message(message)
        self.assertEqual(signal.decision, "TRADE")
        self.assertEqual(signal.direction, "BEAR")
        self.assertEqual(signal.confidence, 0.8)


if __name__ == "__main__":
    unittest.main()
