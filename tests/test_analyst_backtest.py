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

    def _rising_bars(self):
        # Underlying closes 100 -> 102 -> ... rising every day from Aug 3.
        days = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5),
                date(2026, 8, 6), date(2026, 8, 7), date(2026, 8, 10)]
        underlying = []
        for i, day in enumerate(days):
            close = 100.0 + i * 2.0
            underlying.append(_bar(day, close - 1.0, close + 1.0, close - 2.0, close))
        # Option bars start the day AFTER the signal (entry uses next-day open).
        contract = [
            _bar(date(2026, 8, 4), 5.0, 6.0, 4.9, 5.5),
            _bar(date(2026, 8, 5), 5.5, 6.5, 5.4, 6.0),
            _bar(date(2026, 8, 6), 6.0, 7.0, 5.9, 6.5),
            _bar(date(2026, 8, 7), 6.5, 7.5, 6.4, 7.0),
            _bar(date(2026, 8, 10), 7.0, 8.0, 6.9, 7.5),
        ]
        return {"TEST": underlying, "US.TEST|2026-10-16|100|C": contract}

    def test_trade_signals_joins_flow_and_dedupes(self):
        self._seed()
        # A second signal from the same analyst on the same contract dedupes.
        market = FakeMarket({})
        coordinator = AnalystBacktestCoordinator(self.db, market)
        self.assertEqual(len(coordinator.trade_signals()), 1)

    def test_bull_direction_correct_on_rising_underlying(self):
        self._seed(direction="BULL", option_type="C")
        market = FakeMarket(self._rising_bars())
        coordinator = AnalystBacktestCoordinator(self.db, market)
        outcomes = coordinator._settle(coordinator.trade_signals()[0])
        by_horizon = {o["horizon_days"]: o for o in outcomes}
        self.assertEqual(by_horizon[1]["direction_correct"], 1)
        self.assertEqual(by_horizon[1]["strategy_status"], "filled")
        self.assertGreater(by_horizon[1]["strategy_pnl_pct"], 0)

    def test_bear_direction_correct_on_falling_underlying(self):
        self._seed(direction="BEAR", option_type="P")
        # Underlying falls; a BEAR (long put) should be direction-correct.
        days = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]
        underlying = [_bar(days[0], 101, 102, 99, 100), _bar(days[1], 99, 100, 98, 99),
                      _bar(days[2], 98, 99, 97, 98)]
        contract = [_bar(date(2026, 8, 4), 5.0, 5.1, 4.9, 5.0)]
        market = FakeMarket({"TEST": underlying, "US.TEST|2026-10-16|100|P": contract})
        coordinator = AnalystBacktestCoordinator(self.db, market)
        outcomes = coordinator._settle(coordinator.trade_signals()[0])
        self.assertEqual(outcomes[0]["direction_correct"], 1)

    def test_strategy_entry_uses_next_day_open_not_signal_day(self):
        # Guard against look-ahead: the option position must open the day AFTER
        # the signal. If it wrongly opened on the signal day it would include
        # pre-signal movement.
        self._seed(direction="BULL", option_type="C")
        # No option bar on the signal day (Aug 3); only Aug 4 onward exists.
        market = FakeMarket(self._rising_bars())
        coordinator = AnalystBacktestCoordinator(self.db, market)
        outcomes = coordinator._settle(coordinator.trade_signals()[0])
        # horizon=1 uses a single next-day bar -> filled, not no-fill.
        by_horizon = {o["horizon_days"]: o for o in outcomes}
        self.assertEqual(by_horizon[1]["strategy_status"], "filled")

    def test_run_persists_and_is_idempotent(self):
        self._seed(direction="BULL", option_type="C")
        market = FakeMarket(self._rising_bars())
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


if __name__ == "__main__":
    unittest.main()
