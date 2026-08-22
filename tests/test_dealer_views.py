# -*- coding: utf-8 -*-
"""Dealer chart / spark line / symbol-filtered signals views."""
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from options_radar.db import Database
from options_radar.models import FlowEvent
from options_radar.service import OptionsRadarService


class FakeGex:
    def __init__(self, spot=585.0):
        self.spot = spot
        self.strikes = [570.0, 580.0, 590.0]
        self.net_gex = [10.0, -5.0, 20.0]
        self.call_wall = 600.0
        self.put_wall = 570.0
        self.gamma_flip = 582.0
        self.zero_gamma = 582.0
        self.term_structure = {}
        self.heatmap = []
        self.regime = "mixed"
        self.max_pos_gex = 20.0
        self.max_neg_gex = -5.0
        self.expiry_count = 2


class FakeBarsSource:
    def __init__(self, bars=None):
        self.bars = bars or []
        self.calls = 0

    def aggregate_bars(self, ticker, start, end, multiplier=1, timespan="day"):
        self.calls += 1
        return list(self.bars)


def make_bars(n=10):
    base = datetime(2026, 8, 10, 13, 0)
    bars = []
    for i in range(n):
        stamp = base + timedelta(days=i)
        bars.append({
            "t": int(stamp.timestamp() * 1000),
            "o": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i,
            "c": 100.5 + i, "v": 1000,
        })
    return bars


def make_flow_event(event_key, observed_at):
    return FlowEvent(
        event_key=event_key, raw_message_id=None,
        contract_key="US.AAPL|2026-09-18|200|C", symbol="AAPL",
        expiry=date(2026, 9, 18), strike=200.0, option_type="C",
        premium=100000.0, average_price=2.0, dte=30,
        observed_at=observed_at, session_date=observed_at.date(),
    )


class DealerViewTests(unittest.TestCase):
    def test_dealer_view_combines_gex_bars_and_flow_events(self):
        class FakeDatabase:
            def flow_events_recent_for_symbol(self, symbol, days=30):
                self.called_with = (symbol, days)
                return [make_flow_event("EV1", datetime.utcnow() - timedelta(days=2))]

        service = object.__new__(OptionsRadarService)
        service.get_gex = lambda symbol: FakeGex()
        service.bars_source = FakeBarsSource(make_bars())
        service.database = FakeDatabase()
        result = service.dealer_view(symbol="nvda")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["symbol"], "NVDA")
        self.assertEqual(len(result["bars"]), 10)
        self.assertEqual(result["gex"]["call_wall"], 600.0)
        self.assertEqual(result["spot"], 585.0)
        self.assertEqual(len(result["flow_events"]), 1)
        self.assertEqual(result["flow_events"][0]["contract_key"], "US.AAPL|2026-09-18|200|C")
        self.assertEqual(service.database.called_with, ("NVDA", 30))

    def test_dealer_view_falls_back_to_bar_close_spot_without_gex(self):
        class FakeDatabase:
            def flow_events_recent_for_symbol(self, symbol, days=30):
                return []

        service = object.__new__(OptionsRadarService)
        service.get_gex = lambda symbol: None
        service.bars_source = FakeBarsSource(make_bars())
        service.database = FakeDatabase()
        result = service.dealer_view(symbol="aapl")
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["gex"])
        self.assertEqual(result["spot"], 100.5 + 9)
        self.assertEqual(result["flow_events"], [])

    def test_dealer_view_requires_symbol(self):
        service = object.__new__(OptionsRadarService)
        self.assertEqual(service.dealer_view()["status"], "error")

    def test_spark_view_uses_thirty_minute_cache(self):
        service = object.__new__(OptionsRadarService)
        service.bars_source = FakeBarsSource(make_bars())
        first = service.spark_view(symbol="AAPL")
        second = service.spark_view(symbol="AAPL")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(len(first["bars"]), 10)
        self.assertEqual(second["bars"], first["bars"])
        self.assertEqual(service.bars_source.calls, 1)

    def test_spark_view_filters_incomplete_bars(self):
        service = object.__new__(OptionsRadarService)
        bars = make_bars(5) + [{"t": 1234567890000, "o": None, "h": 1, "l": 1, "c": 1}]
        service.bars_source = FakeBarsSource(bars)
        result = service.spark_view(symbol="AAPL")
        self.assertEqual(len(result["bars"]), 5)

    def test_dashboard_signals_symbol_filter_uses_recent_events(self):
        class SigDatabase:
            def flow_events_recent_for_symbol(self, symbol, days=30):
                self.got = (symbol, days)
                return []

            def flow_events_for_date(self, trade_date):
                raise AssertionError("date path must not be used with symbol filter")

            def signals_for_event(self, event_key):
                return []

        service = object.__new__(OptionsRadarService)
        service.database = SigDatabase()
        rows = service.dashboard_signals({"symbol": " aapl "})
        self.assertEqual(rows, [])
        self.assertEqual(service.database.got, ("AAPL", 30))

    def test_flow_events_recent_for_symbol_query(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "radar.db")
            recent = datetime.utcnow() - timedelta(days=3)
            old = datetime.utcnow() - timedelta(days=60)
            database.insert_flow_event(make_flow_event("EV_RECENT", recent))
            database.insert_flow_event(make_flow_event("EV_OLD", old))
            rows = database.flow_events_recent_for_symbol("aapl", 30)
            self.assertEqual([row.event_key for row in rows], ["EV_RECENT"])
            self.assertEqual(database.flow_events_recent_for_symbol("MSFT", 30), [])


if __name__ == "__main__":
    unittest.main()
