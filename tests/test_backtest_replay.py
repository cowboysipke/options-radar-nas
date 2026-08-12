import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from options_radar.backtest_service import BacktestCoordinator
from options_radar.db import Database
from options_radar.history_adapters import SyntheticHistoryAdapter
from options_radar.models import (
    AnalystVote,
    ConsensusEvaluation,
    SignalOutcome,
)


def _evaluation(key="US.TEST|2026-10-16|100|C"):
    return ConsensusEvaluation(
        contract_key=key, evaluated_at=datetime(2026, 8, 3), final_direction="BULL",
        score=79, grade="B", disagreement=False, consensus_strength=1.0,
        components={}, votes=[
            AnalystVote("pa", "price", "TRADE", "BULL", 0.9, 1.0, ["breakout"], underlying_entry=101),
        ], eligible=True,
    )


class BacktestReplayTests(unittest.TestCase):
    def _seed(self, database):
        session = date(2026, 8, 3)
        rec_id = database.save_recommendation(_evaluation(), [1], session)
        database.attach_execution(rec_id, {
            "strategy": "TRIGGERED_ENTRY", "contract_key": "US.TEST|2026-10-16|100|C",
            "max_entry_price": 5.0, "take_profit": 2.0, "stop_loss": 0.8,
        })
        return rec_id

    def test_replay_settles_outcomes_with_synthetic_bars(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "radar.db")
            self._seed(database)
            coordinator = BacktestCoordinator(database, SyntheticHistoryAdapter(), {})
            result = coordinator.replay(date(2026, 8, 1), date(2026, 8, 10))
            self.assertGreaterEqual(result["candidates"], 1)
            self.assertGreaterEqual(result["saved"], 1)
            outcomes = database.signal_outcomes()
            self.assertTrue(outcomes)
            self.assertIn(outcomes[0].status, {"filled", "no-fill"})

    def test_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "radar.db")
            self._seed(database)
            coordinator = BacktestCoordinator(database, SyntheticHistoryAdapter(), {})
            first = coordinator.replay(date(2026, 8, 1), date(2026, 8, 10))
            second = coordinator.replay(date(2026, 8, 1), date(2026, 8, 10))
            self.assertEqual(second["saved"], 0)
            self.assertEqual(len(database.signal_outcomes()), first["saved"])

    def test_replay_summary_returns_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "radar.db")
            self._seed(database)
            coordinator = BacktestCoordinator(database, SyntheticHistoryAdapter(), {})
            coordinator.replay(date(2026, 8, 1), date(2026, 8, 10))
            summary = coordinator.replay_summary(date(2026, 8, 1), date(2026, 8, 10))
            self.assertGreaterEqual(len(summary["outcomes"]), 1)
            self.assertIn("avg_net_return", summary)

    def test_synthetic_bars_are_deterministic(self):
        adapter = SyntheticHistoryAdapter()
        first = adapter.aggregate_bars("US.TEST|2026-10-16|100|C", date(2026, 8, 4), date(2026, 8, 7))
        second = adapter.aggregate_bars("US.TEST|2026-10-16|100|C", date(2026, 8, 4), date(2026, 8, 7))
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertEqual([bar for bar in first if bar["c"] <= 0], [])


if __name__ == "__main__":
    unittest.main()
