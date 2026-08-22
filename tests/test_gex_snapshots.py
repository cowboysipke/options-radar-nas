# -*- coding: utf-8 -*-
"""GEX forward-validation snapshots: storage, daily worker, coverage summary."""
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from options_radar.db import Database
from options_radar.models import FlowEvent
from options_radar.service import OptionsRadarService


def fake_gex(spot=585.0, strikes=(570.0, 580.0, 590.0)):
    return SimpleNamespace(
        symbol="NVDA", spot=spot, strikes=list(strikes),
        net_gex=[10.0, -5.0, 20.0],
        call_gex={570.0: 4.0, 580.0: 2.0, 590.0: 20.0},
        put_gex={570.0: -14.0, 580.0: -3.0, 590.0: 0.0},
        call_wall=590.0, put_wall=570.0, gamma_flip=None,
        zero_gamma=582.0, atm_iv=40.0, put_skew=2.0, call_skew=-1.0,
        regime="mixed", max_pos_gex=20.0, max_neg_gex=-14.0, expiry_count=2,
    )


def flow_event(symbol, event_key):
    return FlowEvent(
        event_key=event_key, raw_message_id=None,
        contract_key=f"US.{symbol}|2026-09-18|200|C", symbol=symbol,
        expiry=date(2026, 9, 18), strike=200.0, option_type="C",
        premium=100000.0, average_price=2.0, dte=30,
        observed_at=datetime.utcnow() - timedelta(hours=3),
        session_date=date(2026, 8, 21),
    )


class GexSnapshotDbTests(unittest.TestCase):
    def test_save_upserts_and_summarizes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "radar.db")
            target = date(2026, 8, 21)
            rows = database.save_gex_snapshot(target, "nvda", fake_gex())
            self.assertEqual(rows, 3)
            summary = database.gex_snapshot_summary()
            self.assertEqual(summary["days"], 1)
            self.assertEqual(summary["symbols"], 1)
            self.assertEqual(summary["latest_date"], "2026-08-21")
            # Re-run same day refreshes rather than duplicates.
            updated = fake_gex()
            updated.net_gex = [1.0, 2.0, 3.0]
            self.assertEqual(database.save_gex_snapshot(target, "NVDA", updated), 3)
            self.assertEqual(database.gex_snapshot_summary()["days"], 1)
            with database.connect() as connection:
                net = connection.execute(
                    "SELECT net_gex FROM gex_snapshots WHERE symbol='NVDA' "
                    "AND strike=580.0"
                ).fetchone()[0]
            self.assertEqual(net, 2.0)

    def test_empty_gex_saves_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "radar.db")
            rows = database.save_gex_snapshot(
                date(2026, 8, 21), "NVDA", fake_gex(strikes=[]),
            )
            self.assertEqual(rows, 0)
            self.assertEqual(database.gex_snapshot_summary()["days"], 0)


class GexSnapshotWorkerTests(unittest.TestCase):
    def test_snapshot_gex_records_flow_symbols(self):
        class FakeDatabase:
            def __init__(self):
                self.saved = []

            def flow_events_for_date(self, trade_date):
                self.queried_date = trade_date
                return [
                    flow_event("NVDA", "EV1"),
                    flow_event("NVDA", "EV2"),
                    flow_event("AAPL", "EV3"),
                ]

            def save_gex_snapshot(self, snapshot_date, symbol, gex):
                self.saved.append((snapshot_date, symbol, gex))
                return 3

        service = object.__new__(OptionsRadarService)
        service.database = FakeDatabase()
        service._trade_date = lambda: date(2026, 8, 21)
        service.get_gex = lambda symbol: (
            None if symbol == "AAPL" else fake_gex()
        )
        result = service.snapshot_gex()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["symbols"], 2)
        self.assertEqual(result["rows"], 3)  # NVDA only; AAPL empty -> not saved
        self.assertIn("AAPL:empty", result["failed"])
        self.assertEqual(
            [item[1] for item in service.database.saved], ["NVDA"],
        )
        self.assertEqual(service.database.queried_date, date(2026, 8, 21))

    def test_snapshot_gex_limit_and_string_date(self):
        class FakeDatabase:
            def __init__(self):
                self.saved = []

            def flow_events_for_date(self, trade_date):
                return [flow_event("NVDA", "EV1"), flow_event("AAPL", "EV2")]

            def save_gex_snapshot(self, snapshot_date, symbol, gex):
                self.saved.append((snapshot_date, symbol))
                return 3

        service = object.__new__(OptionsRadarService)
        service.database = FakeDatabase()
        service._trade_date = lambda: date(2026, 8, 20)
        service.get_gex = lambda symbol: fake_gex()
        result = service.snapshot_gex(target_date="2026-08-19", limit=1)
        self.assertEqual(result["symbols"], 1)
        self.assertEqual([item[1] for item in service.database.saved], ["NVDA"])
        self.assertEqual(service.database.saved[0][0], date(2026, 8, 19))


if __name__ == "__main__":
    unittest.main()
