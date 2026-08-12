import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from options_radar.db import Database
from options_radar.models import AnalystVote, ConsensusEvaluation, WatchlistItem
from options_radar.reports import evaluation_markdown, portfolio_markdown, write_daily_report


class ReportEncodingTests(unittest.TestCase):
    def test_windows_report_has_utf8_bom_and_readable_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_daily_report(Database(root / "radar.db"), date(2026, 8, 10), root / "reports")
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            text = raw.decode("utf-8-sig")
            self.assertIn("异常期权日报", text)
            self.assertIn("不凑数", text)
            self.assertIn(b"\r\n", raw)
            self.assertNotIn("\ufffd", text)

    def test_user_facing_markdown_keeps_chinese_text(self):
        evaluation = ConsensusEvaluation(
            contract_key="US.SAP|2026-09-18|250|C",
            evaluated_at=__import__("datetime").datetime(2026, 8, 11),
            final_direction="BULL",
            score=82,
            grade="A",
            disagreement=False,
            consensus_strength=0.8,
            components={},
            votes=[AnalystVote(
                analyst="pa", family="pa", decision="TRADE", direction="BULL",
                confidence=0.8, weight=1.0, rationale=["趋势向上"],
            )],
            risk_flags=["价差偏宽"],
            eligible=True,
        )
        text = evaluation_markdown(evaluation)
        self.assertIn("一句话动作", text)
        self.assertIn("看多", text)
        self.assertIn("核心理由", text)
        self.assertIn("主要风险", text)
        self.assertNotIn("\ufffd", text)

        portfolio = portfolio_markdown({
            "SAP": SimpleNamespace(
                snapshot_at=None, in_watchlist=True, held_quantity=10,
                concentration=0.12,
            )
        })
        self.assertIn("富途持仓与自选匹配", portfolio)
        self.assertIn("自选 是", portfolio)
        self.assertNotIn("\ufffd", portfolio)

    def test_visible_source_files_are_strict_utf8_without_mojibake_markers(self):
        project = Path(__file__).resolve().parents[1]
        files = list((project / "options_radar").glob("*.py"))
        files += [
            project / "README.md", project / "config.example.yaml",
            project / "config.nas.example.yaml", project / "docs" / "nas.md",
            project / "docs" / "strategy.md", project / "nas-quickstart" / "README.md",
        ]
        mojibake_markers = ("锛", "銆", "鐨", "鍒嗘瀽", "鏈熸潈", "涓€")
        for path in files:
            with self.subTest(path=path.name):
                text = path.read_bytes().decode("utf-8-sig", errors="strict")
                self.assertNotIn("\ufffd", text)
                for marker in mojibake_markers:
                    self.assertNotIn(marker, text)

    def test_watchlist_default_group_is_readable_chinese(self):
        self.assertEqual(WatchlistItem("SAP").group_name, "默认")


if __name__ == "__main__":
    unittest.main()
