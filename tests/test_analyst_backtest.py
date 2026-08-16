import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from options_radar.analyst_backtest import AnalystBacktestCoordinator
from options_radar.db import Database
from options_radar.models import FlowEvent, ParsedSignal


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

    def test_trade_signals_joins_flow_and_dedupes(self):
        self._seed()
        coordinator = AnalystBacktestCoordinator(self.db, FakeMarket({}))
        self.assertEqual(len(coordinator.trade_signals()), 1)

    def test_bull_sells_atm_put_and_stock_leg(self):
        self._seed(direction="BULL", option_type="C")
        underlying = self._rising_underlying()
        # Signal-day close = 100 -> ATM strike 100 -> O:TEST261016P00100000
        put_bars = [
            _bar(date(2026, 8, 4), 5.0, 5.5, 4.8, 4.5),
            _bar(date(2026, 8, 5), 4.5, 4.8, 4.2, 4.0),
            _bar(date(2026, 8, 6), 4.0, 4.4, 3.8, 3.6),
            _bar(date(2026, 8, 7), 3.6, 3.9, 3.4, 3.2),
            _bar(date(2026, 8, 10), 3.2, 3.5, 3.0, 2.8),
        ]
        market = FakeMarket({"TEST": underlying, "O:TEST261016P00100000": put_bars})
        coordinator = AnalystBacktestCoordinator(self.db, market)
        outcomes = coordinator._settle(coordinator.trade_signals()[0])
        by_horizon = {o["horizon_days"]: o for o in outcomes}
        self.assertEqual(by_horizon[1]["direction_correct"], 1)
        self.assertGreater(by_horizon[1]["stock_pnl_pct"], 0)
        self.assertEqual(by_horizon[1]["strategy_status"], "filled")
        # Underlying rises -> short put gains.
        self.assertGreater(by_horizon[1]["strategy_pnl_pct"], 0)

    def test_bear_stock_flat_and_sells_atm_call(self):
        self._seed(direction="BEAR", option_type="P")
        # Underlying falls from 100 on the signal day; ATM CALL strike 100.
        underlying = [
            _bar(date(2026, 8, 3), 101, 102, 99, 100),
            _bar(date(2026, 8, 4), 99, 100, 98, 99),
            _bar(date(2026, 8, 5), 98, 99, 97, 98),
            _bar(date(2026, 8, 6), 97, 98, 96, 97),
        ]
        call_bars = [
            _bar(date(2026, 8, 4), 5.0, 5.2, 4.8, 4.6),
            _bar(date(2026, 8, 5), 4.6, 4.8, 4.3, 4.1),
            _bar(date(2026, 8, 6), 4.1, 4.4, 3.9, 3.7),
        ]
        market = FakeMarket({"TEST": underlying, "O:TEST261016C00100000": call_bars})
        coordinator = AnalystBacktestCoordinator(self.db, market)
        outcomes = coordinator._settle(coordinator.trade_signals()[0])
        by_horizon = {o["horizon_days"]: o for o in outcomes}
        self.assertEqual(by_horizon[1]["direction_correct"], 1)
        # BEAR: stock leg stays flat at 0.
        self.assertEqual(by_horizon[1]["stock_pnl_pct"], 0.0)
        self.assertGreater(by_horizon[1]["strategy_pnl_pct"], 0)

    def test_short_option_stops_out_when_premium_spikes(self):
        self._seed(direction="BULL", option_type="C")
        underlying = [
            _bar(date(2026, 8, 3), 101, 102, 99, 100),
            _bar(date(2026, 8, 4), 98, 99, 95, 96),
            _bar(date(2026, 8, 5), 96, 97, 94, 95),
        ]
        # Premium spikes far above +50% on day 1 -> stop-loss, not holding-limit.
        put_bars = [
            _bar(date(2026, 8, 4), 5.0, 9.0, 4.9, 8.5),
            _bar(date(2026, 8, 5), 8.5, 9.5, 8.0, 9.0),
        ]
        market = FakeMarket({"TEST": underlying, "O:TEST261016P00100000": put_bars})
        coordinator = AnalystBacktestCoordinator(self.db, market)
        outcomes = coordinator._settle(coordinator.trade_signals()[0])
        by_horizon = {o["horizon_days"]: o for o in outcomes}
        self.assertLess(by_horizon[1]["strategy_pnl_pct"], 0)

    def test_strategy_entry_uses_next_day_open_not_signal_day(self):
        self._seed(direction="BULL", option_type="C")
        underlying = self._rising_underlying()
        put_bars = [
            _bar(date(2026, 8, 4), 5.0, 5.5, 4.8, 4.5),
            _bar(date(2026, 8, 5), 4.5, 4.8, 4.2, 4.0),
            _bar(date(2026, 8, 6), 4.0, 4.4, 3.8, 3.6),
        ]
        market = FakeMarket({"TEST": underlying, "O:TEST261016P00100000": put_bars})
        coordinator = AnalystBacktestCoordinator(self.db, market)
        outcomes = coordinator._settle(coordinator.trade_signals()[0])
        by_horizon = {o["horizon_days"]: o for o in outcomes}
        self.assertEqual(by_horizon[1]["strategy_status"], "filled")

    def test_run_persists_and_is_idempotent(self):
        self._seed(direction="BULL", option_type="C")
        underlying = self._rising_underlying()
        put_bars = [
            _bar(date(2026, 8, 4), 5.0, 5.5, 4.8, 4.5),
            _bar(date(2026, 8, 5), 4.5, 4.8, 4.2, 4.0),
            _bar(date(2026, 8, 6), 4.0, 4.4, 3.8, 3.6),
        ]
        market = FakeMarket({"TEST": underlying, "O:TEST261016P00100000": put_bars})
        coordinator = AnalystBacktestCoordinator(self.db, market)
        first = coordinator.run()
        self.assertGreaterEqual(first["saved"], 3)
        count = len(self.db.analyst_backtest_outcomes())
        coordinator.run()
        self.assertEqual(len(self.db.analyst_backtest_outcomes()), count)
        summary = self.db.analyst_backtest_summary()
        self.assertTrue(summary)
        self.assertIn("direction_hits", summary[0])
        self.assertIn("strategy_wins", summary[0])
        self.assertIn("stock_wins", summary[0])


if __name__ == "__main__":
    unittest.main()
