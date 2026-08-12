import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from options_radar.db import Database
from options_radar.models import RawMessage
from options_radar.rulebook import RulebookCompiler


class RulebookTests(unittest.TestCase):
    def test_compile_versions_rules_profiles_and_terms(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "radar.db")
            compiler = RulebookCompiler(db)
            result = compiler.compile([
                RawMessage(
                    channel="使用指南", analyst="pa", observed_at=datetime(2026, 8, 11, 9),
                    content="入场看标的触发，止损是失效条件。方向 bull，信心 confidence。",
                ),
                RawMessage(
                    channel="订阅面板", analyst="qmr", observed_at=datetime(2026, 8, 11, 10),
                    content="QMR 用动量反转判断，展示胜率与风险。",
                ),
            ])
            self.assertTrue(result.version.startswith("20260811-"))
            self.assertEqual(result.analyst_profiles["qmr"]["family"], "momentum_reversal")
            self.assertEqual(result.terminology["入场"]["normalized_value"], "ENTRY")
            self.assertEqual(len(db.source_rules(active_only=True)), 2)
            self.assertEqual(len(db.analyst_profiles()), 2)


if __name__ == "__main__":
    unittest.main()
