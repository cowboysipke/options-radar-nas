import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from options_radar.config import load_config
from options_radar.models import RawMessage
from options_radar.pipeline import RadarPipeline


class PipelineTests(unittest.TestCase):
    def test_end_to_end_without_external_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text(
                "database_path: data/test.db\nevidence_dir: evidence\n"
                "futu:\n  enabled: false\nllm:\n  api_key: ''\n"
                "scoring:\n  recommendation_threshold: 65\npaper:\n  starting_cash: 100000\n",
                encoding="utf-8",
            )
            pipeline = RadarPipeline(load_config(str(root / "config.yaml")))
            observed = datetime(2026, 8, 6, 0, 31)
            messages = [
                RawMessage(channel="异常期权", analyst="flow", observed_at=observed,
                           content="FCX 69 P 2026-08-14 $263K AVG$2.21 9DTE"),
                RawMessage(channel="pa分析师", analyst="pa", observed_at=observed,
                           content="FCX 2026-08-14 69P | 解读 Premium $263,296 | DTE - 执行观点 不交易 neutral 置信2"),
                RawMessage(channel="mr分析师", analyst="mr", observed_at=observed,
                           content="FCX 2026-08-14 69P | MR 解读 Premium $263,296 | DTE - decision trade confidence_score 4"),
                RawMessage(channel="qmr分析师", analyst="qmr", observed_at=observed,
                           content="FCX 2026-08-14 69P | QMR 解读 Premium $263,296 | DTE - 结论：交易 | 方向：偏空 | 信心：高"),
                RawMessage(channel="fpd分析师", analyst="fpd", observed_at=observed,
                           content="FCX 2026-08-14 69P | 解读 Premium $263,296 | DTE - 执行观点 交易 bear 入场69.50 目标66.00 止损70.60 价格结构 Flow-Price Divergence 期权结构 偏向bear 置信4 风险提示 风险4"),
            ]
            results = pipeline.process_messages(messages, date(2026, 8, 5))
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].evaluation.final_direction, "BEAR")
            event_key = pipeline.database.flow_events_for_date(date(2026, 8, 5))[0].event_key
            signals = pipeline.database.signals_for_event(event_key)
            self.assertTrue(all(item.dte == 9 for item in signals))
            self.assertEqual(len(results[0].evaluation.votes), 4)


if __name__ == "__main__":
    unittest.main()
