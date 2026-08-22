# -*- coding: utf-8 -*-
"""Test D historical validation (HV/score stratification) and price structure."""
import json
import math
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from options_radar.db import Database
from options_radar.service import OptionsRadarService


def bars_json(closes, base_t=datetime(2026, 7, 1, 13, 0)):
    items = []
    for i, close in enumerate(closes):
        items.append({
            "d": base_t.date().isoformat(), "o": close, "h": close * 1.01,
            "l": close * 0.99, "c": close,
        })
    return json.dumps(items)


def volatile_closes(base=100.0, amp=0.01, n=21):
    closes = [base]
    for i in range(1, n):
        closes.append(closes[-1] * (1 + amp if i % 2 else 1 - amp))
    return closes


class HvHelperTests(unittest.TestCase):
    def test_hv_positive_for_varying_closes(self):
        # Alternating ±5% daily moves -> annualized HV well above 50%.
        hv = Database._hv_from_bars_json(bars_json(volatile_closes(amp=0.05)))
        self.assertIsNotNone(hv)
        self.assertGreater(hv, 0.50)

    def test_hv_none_for_empty_or_flat(self):
        self.assertIsNone(Database._hv_from_bars_json(""))
        self.assertIsNone(Database._hv_from_bars_json("[1,2]"))
        self.assertIsNone(Database._hv_from_bars_json(bars_json([100.0] * 5)))


class StratifiedOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "radar.db")

    def tearDown(self):
        self.temp.cleanup()

    def insert_series(self, analyst, contract, bars):
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO analyst_backtest_series
                (analyst, analyst_family, contract_key, symbol, session_date, direction,
                 option_type, expiry, entry_day, atm_strike, atm_ticker,
                 underlying_bars_json, option_bars_json, flow_option_bars_json, observed_at)
                VALUES (?, 'pa', ?, 'NVDA', '2026-08-01', 'BULL', 'C', '2026-09-18',
                        '2026-08-03', 100.0, 'O:NVDA260918C00100000', ?, '[]', '[]', ?)""",
                (analyst, contract, bars, datetime.utcnow().isoformat()),
            )

    def insert_outcome(self, analyst, contract, pnl, premium=None):
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO analyst_backtest_outcomes
                (analyst, analyst_family, contract_key, symbol, session_date, direction,
                 option_type, horizon_days, strategy_status, strategy_pnl_pct,
                 strategy_premium_pct, observed_at)
                VALUES (?, 'pa', ?, 'NVDA', '2026-08-01', 'BULL', 'C', 0, 'filled', ?, ?, ?)""",
                (analyst, contract, pnl, premium, datetime.utcnow().isoformat()),
            )

    def test_hv_stratified_buckets(self):
        low_bars = bars_json([100.0 * (1.001 ** i) for i in range(21)])  # quiet
        high_bars = bars_json(volatile_closes(amp=0.06))                 # wild
        self.insert_series("pa", "US.NVDA|2026-09-18|100|C", low_bars)
        self.insert_series("qmr", "US.NVDA|2026-09-18|100|C", low_bars)
        self.insert_series("mr", "US.NVDA|2026-09-18|110|C", high_bars)
        self.insert_outcome("pa", "US.NVDA|2026-09-18|100|C", 0.20, 0.35)
        self.insert_outcome("qmr", "US.NVDA|2026-09-18|100|C", -0.10, -0.20)
        self.insert_outcome("mr", "US.NVDA|2026-09-18|110|C", 0.05, 0.10)
        rows = self.db.hv_stratified_outcomes(0)
        by_bucket = {row["bucket"]: row for row in rows}
        self.assertIn("低 HV <30%", by_bucket)
        self.assertIn("高 HV >60%", by_bucket)
        self.assertEqual(by_bucket["低 HV <30%"]["n"], 2)
        self.assertAlmostEqual(by_bucket["低 HV <30%"]["win_rate"], 0.5)
        self.assertEqual(by_bucket["高 HV >60%"]["n"], 1)

    def test_score_stratified_buckets(self):
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO recommendations
                (contract_key, session_date, evaluated_at, score, grade, final_direction,
                 disagreement, eligible, strategy_type, payload_json, signal_ids_json)
                VALUES (?, '2026-08-01', ?, 72, 'B', 'BULL', 0, 1, 'sell', ?, '[]')""",
                ("US.NVDA|2026-09-18|100|C", datetime.utcnow().isoformat(),
                 json.dumps({"contract_key": "US.NVDA|2026-09-18|100|C", "score": 72})),
            )
            connection.execute(
                """INSERT INTO recommendations
                (contract_key, session_date, evaluated_at, score, grade, final_direction,
                 disagreement, eligible, strategy_type, payload_json, signal_ids_json)
                VALUES (?, '2026-08-01', ?, 45, 'D', 'BEAR', 0, 1, 'sell', ?, '[]')""",
                ("US.NVDA|2026-09-18|110|C", datetime.utcnow().isoformat(),
                 json.dumps({"contract_key": "US.NVDA|2026-09-18|110|C", "score": 45})),
            )
        self.insert_outcome("pa", "US.NVDA|2026-09-18|100|C", 0.30)
        self.insert_outcome("pa", "US.NVDA|2026-09-18|110|C", -0.15)
        rows = self.db.score_stratified_outcomes(0)
        by_bucket = {row["bucket"]: row for row in rows}
        self.assertIn("≥65（B+）", by_bucket)
        self.assertIn("<50（D）", by_bucket)
        self.assertAlmostEqual(by_bucket["≥65（B+）"]["avg_pnl_pct"], 0.30)
        self.assertAlmostEqual(by_bucket["<50（D）"]["avg_pnl_pct"], -0.15)


class PriceStructureTests(unittest.TestCase):
    def make_bars(self, closes):
        out = []
        for i, close in enumerate(closes):
            out.append({"t": 1000000 + i, "o": close, "h": close * 1.01,
                        "l": close * 0.99, "c": close})
        return out

    def test_ma_and_support_resistance(self):
        closes = [100.0 + i for i in range(50)]  # rising; ma20 < ma50 < last close
        structure = OptionsRadarService._price_structure_from_bars(self.make_bars(closes))
        self.assertEqual(len(structure["ma20"]), 50)
        self.assertIsNone(structure["ma20"][10])  # insufficient history early on
        self.assertAlmostEqual(structure["ma20"][-1], sum(closes[-20:]) / 20, places=6)
        self.assertAlmostEqual(structure["ma50"][-1], sum(closes) / 50, places=6)
        self.assertAlmostEqual(structure["support"], min(closes[-20:]) * 0.99, places=6)
        self.assertAlmostEqual(structure["resistance"], max(closes[-20:]) * 1.01, places=6)

    def test_empty_bars(self):
        structure = OptionsRadarService._price_structure_from_bars([])
        self.assertEqual(structure, {"ma20": [], "ma50": [], "support": None, "resistance": None})


if __name__ == "__main__":
    unittest.main()
