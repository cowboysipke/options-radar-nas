import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from options_radar.analyst_backtest import (
    EXPIRY_HORIZON,
    AnalystBacktestCoordinator,
)
from options_radar.db import Database
from options_radar.models import FlowEvent, ParsedSignal
from options_radar.optimizer import OptionBar


def _bar(day, open_, high, low, close):
    ts = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    return {"t": ts, "o": open_, "h": high, "l": low, "c": close, "v": 1000}


class FakeMarket:
    """Deterministic bars keyed by ticker; returns [start, end] inclusive."""

    def __init__(self, bars_by_ticker):
        self.bars = bars_by_ticker

    def aggregate_bars(self, ticker, start, end, multiplier=1, timespan="day"):
        result = []
        for bar in self.bars.get(ticker, []):
            day = datetime.utcfromtimestamp(bar["t"] / 1000).date()
            if start <= day <= end:
                result.append(dict(bar))
        return result

    def occ_ticker(self, contract_key):
        return contract_key


class AnalystBacktestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "radar.db")

    def _seed(self, symbol="TEST", contract="US.TEST|2026-10-16|100|C",
              direction="BULL", option_type="C", analyst="mr", family="mean_reversion"):
        flow = FlowEvent(
            event_key=f"evt-{contract}", contract_key=contract, symbol=symbol,
            expiry=date(2026, 10, 16), strike=100.0, option_type=option_type,
            premium=5.0, average_price=None, dte=None,
            observed_at=datetime(2026, 8, 3, 18, 0), session_date=date(2026, 8, 3),
        )
        self.db.insert_flow_event(flow)
        signal = ParsedSignal(
            flow_event_key=flow.event_key, contract_key=contract, symbol=symbol,
            expiry=date(2026, 10, 16), strike=100.0, option_type=option_type,
            decision="TRADE", direction=direction, direction_source="flow",
            confidence=0.8, confidence_raw="high", analyst_family=family,
            analyst=analyst, channel="test", observed_at=datetime(2026, 8, 3, 18, 0),
        )
        self.db.insert_signal(signal)

    def _rising_underlying(self):
        days = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5),
                date(2026, 8, 6), date(2026, 8, 7), date(2026, 8, 10)]
        underlying = []
        for i, day in enumerate(days):
            close = 100.0 + i * 2.0
            underlying.append(_bar(day, close - 1.0, close + 1.0, close - 2.0, close))
        return underlying

    def _settle_one(self, direction, option_type, underlying, option_bars, ticker):
        self._seed(direction=direction, option_type=option_type)
        market = FakeMarket({"TEST": underlying, ticker: option_bars})
        coordinator = AnalystBacktestCoordinator(self.db, market)
        coordinator.build_series()
        coordinator.settle_horizon(1)
        rows = self.db.analyst_backtest_outcomes_for_analyst("mr", 1)
        return coordinator, rows

    def test_trade_signals_joins_flow_and_dedupes(self):
        self._seed()
        coordinator = AnalystBacktestCoordinator(self.db, FakeMarket({}))
        self.assertEqual(len(coordinator.trade_signals()), 1)

    def test_bull_sells_atm_put_and_stock_leg(self):
        underlying = self._rising_underlying()
        put_bars = [
            _bar(date(2026, 8, 4), 5.0, 5.5, 4.8, 4.5),
            _bar(date(2026, 8, 5), 4.5, 4.8, 4.2, 4.0),
            _bar(date(2026, 8, 6), 4.0, 4.4, 3.8, 3.6),
        ]
        _, rows = self._settle_one("BULL", "C", underlying, put_bars, "O:TEST261016P00100000")
        self.assertEqual(len(rows), 1)
        o = rows[0]
        self.assertEqual(o["direction_correct"], 1)
        self.assertGreater(o["stock_pnl_pct"], 0)
        self.assertEqual(o["strategy_status"], "filled")
        self.assertGreater(o["strategy_pnl_pct"], 0)
        self.assertGreater(o["strategy_premium_pct"], 0)
        self.assertEqual(o["atm_ticker"], "O:TEST261016P00100000")

    def test_bear_stock_flat_and_sells_atm_call(self):
        underlying = [
            _bar(date(2026, 8, 3), 101, 102, 99, 100),
            _bar(date(2026, 8, 4), 99, 100, 98, 99),
            _bar(date(2026, 8, 5), 98, 99, 97, 98),
            _bar(date(2026, 8, 6), 97, 98, 96, 97),
        ]
        call_bars = [
            _bar(date(2026, 8, 4), 5.0, 5.2, 4.8, 4.6),
            _bar(date(2026, 8, 5), 4.6, 4.8, 4.3, 4.1),
        ]
        _, rows = self._settle_one("BEAR", "P", underlying, call_bars, "O:TEST261016C00100000")
        o = rows[0]
        self.assertEqual(o["direction_correct"], 1)
        self.assertEqual(o["stock_pnl_pct"], 0.0)
        self.assertGreater(o["strategy_pnl_pct"], 0)

    def test_short_option_stops_out_when_premium_spikes(self):
        underlying = [
            _bar(date(2026, 8, 3), 101, 102, 99, 100),
            _bar(date(2026, 8, 4), 98, 99, 95, 96),
            _bar(date(2026, 8, 5), 96, 97, 94, 95),
        ]
        put_bars = [
            _bar(date(2026, 8, 4), 5.0, 9.0, 4.9, 8.5),
            _bar(date(2026, 8, 5), 8.5, 9.5, 8.0, 9.0),
        ]
        _, rows = self._settle_one("BULL", "C", underlying, put_bars, "O:TEST261016P00100000")
        o = rows[0]
        self.assertLess(o["strategy_pnl_pct"], 0)
        self.assertEqual(o["strategy_exit_reason"], "stop-loss")
        self.assertAlmostEqual(o["strategy_premium_pct"], -0.5, delta=0.05)

    def test_run_builds_series_and_settles_default_horizons(self):
        self._seed(direction="BULL", option_type="C")
        underlying = self._rising_underlying()
        put_bars = [
            _bar(date(2026, 8, 4), 5.0, 5.5, 4.8, 4.5),
            _bar(date(2026, 8, 5), 4.5, 4.8, 4.2, 4.0),
            _bar(date(2026, 8, 6), 4.0, 4.4, 3.8, 3.6),
        ]
        market = FakeMarket({"TEST": underlying, "O:TEST261016P00100000": put_bars})
        coordinator = AnalystBacktestCoordinator(self.db, market)
        result = coordinator.run()
        self.assertEqual(result["built"], 1)
        # default horizons 1/3/5 + expiry(0) all settled
        for horizon in (1, 3, 5, EXPIRY_HORIZON):
            self.assertEqual(len(self.db.analyst_backtest_outcomes_for_analyst("mr", horizon)), 1)
        # idempotent on second run
        result2 = coordinator.run()
        self.assertEqual(result2["built"], 0)

    def test_horizon_bars_expiry_and_n_day(self):
        coordinator = AnalystBacktestCoordinator(self.db, FakeMarket({}))
        session = date(2026, 8, 3)
        entry = date(2026, 8, 4)
        expiry = date(2026, 8, 14)
        bars = [OptionBar(datetime(2026, 8, day, 12), 5, 6, 4, 5) for day in range(4, 15)]
        # N-day slice on option series -> first N bars
        self.assertEqual(len(coordinator._horizon_bars(bars, session, entry, expiry, 3, is_option=True)), 3)
        # expiry slice -> up to expiry - 3 days (8/11)
        exp_bars = coordinator._horizon_bars(bars, session, entry, expiry, EXPIRY_HORIZON, is_option=True)
        self.assertEqual(exp_bars[-1].observed_at.date(), date(2026, 8, 11))


if __name__ == "__main__":
    unittest.main()
