"""Analyst signal accuracy back-test.

Deterministic replay that answers two questions per TRADE signal:
  1. direction hit-rate  — did the underlying move the way the analyst said?
  2. strategy pnl        — would a long CALL (BULL) / long PUT (BEAR) have paid?

Data comes from Massive daily bars (both the underlying and the option
contract). No AI is involved; every number is reproducible.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .db import Database
from .massive_client import MassiveClient
from .optimizer import OptionBar, simulate_long_option
from .paper import add_business_days


def _bar(item: Dict[str, object]) -> Optional[OptionBar]:
    try:
        stamp = datetime.utcfromtimestamp(float(item["t"]) / 1000.0)
        return OptionBar(
            observed_at=stamp, open=float(item["o"]), high=float(item["h"]),
            low=float(item["l"]), close=float(item["c"]), complete=True,
        )
    except (KeyError, TypeError, ValueError):
        return None


class AnalystBacktestCoordinator:
    """Replay TRADE signals against Massive daily bars and persist outcomes."""

    def __init__(
        self, database: Database, market: MassiveClient,
        horizons: Sequence[int] = (1, 3, 5),
        take_profit_pct: float = 0.35, stop_loss_pct: float = 0.25,
    ):
        self.database = database
        self.market = market
        self.horizons = tuple(horizons)
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct

    # -- data access ---------------------------------------------------------

    def trade_signals(self) -> List[Dict[str, Any]]:
        """Distinct (analyst, contract) TRADE signals with the flow contract joined in."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT p.analyst, p.analyst_family, p.direction,
                          f.contract_key, f.symbol, f.option_type, f.session_date,
                          MAX(p.observed_at) AS observed_at
                   FROM parsed_signals p
                   JOIN flow_events f ON p.flow_event_key = f.event_key
                   WHERE p.decision='TRADE' AND p.direction IN ('BULL','BEAR')
                     AND f.session_date IS NOT NULL
                   GROUP BY p.analyst, f.contract_key
                   ORDER BY f.session_date, p.analyst"""
            ).fetchall()
        return [dict(row) for row in rows]

    def _settled_keys(self) -> Set[Tuple[str, str]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT analyst, contract_key FROM analyst_backtest_outcomes"
            ).fetchall()
        return {(str(row["analyst"]), str(row["contract_key"])) for row in rows}

    # -- bars ----------------------------------------------------------------

    def _daily_bars(self, ticker: str, start: date, end: date) -> List[OptionBar]:
        try:
            raw = self.market.aggregate_bars(ticker, start, end, 1, "day")
        except Exception:
            return []
        bars = [value for value in (_bar(item) for item in raw) if value is not None]
        # Clip to the requested window so a longer fetch never leaks a bar past
        # the horizon (look-ahead bias).
        return [bar for bar in bars if bar.observed_at.date() <= end]

    # -- per-signal settlement ----------------------------------------------

    def _settle(self, signal: Dict[str, Any]) -> List[Dict[str, object]]:
        session = date.fromisoformat(str(signal["session_date"]))
        symbol = str(signal["symbol"])
        contract = str(signal["contract_key"])
        option_type = str(signal["option_type"])
        direction = str(signal["direction"])

        # Fetch each series once (covering the widest horizon) and slice per
        # horizon to keep the request count at 2 per signal instead of 6.
        max_end = add_business_days(session, max(self.horizons))
        underlying_all = self._daily_bars(symbol, session, max_end)
        entry_day = add_business_days(session, 1)
        contract_all = self._daily_bars(self.market.occ_ticker(contract), entry_day, max_end)

        outcomes: List[Dict[str, object]] = []
        for horizon in self.horizons:
            end_day = add_business_days(session, horizon)
            underlying_bars = [b for b in underlying_all if b.observed_at.date() <= end_day]
            contract_bars = [b for b in contract_all if b.observed_at.date() <= end_day]

            underlying_change = None
            direction_correct = None
            if len(underlying_bars) >= 2:
                base = underlying_bars[0].close
                last = underlying_bars[-1].close
                if base and last:
                    underlying_change = (last - base) / base
                    direction_correct = 1 if (underlying_change > 0) == (direction == "BULL") else 0

            strategy = simulate_long_option(
                contract_bars, quantity=1, max_entry_price=None,
                take_profit_pct=self.take_profit_pct, stop_loss_pct=self.stop_loss_pct,
            )
            outcomes.append({
                "analyst": signal["analyst"],
                "analyst_family": signal["analyst_family"],
                "contract_key": contract,
                "symbol": symbol,
                "session_date": signal["session_date"],
                "direction": direction,
                "option_type": option_type,
                "horizon_days": horizon,
                "strategy_status": strategy.status,
                "strategy_pnl_pct": strategy.pnl_pct,
                "direction_correct": direction_correct,
                "underlying_change_pct": underlying_change,
                "observed_at": signal["observed_at"],
            })
        return outcomes

    # -- entry points --------------------------------------------------------

    def run(self) -> Dict[str, int]:
        """Settle unsettled TRADE signals; idempotent (ON CONFLICT updates)."""
        settled = self._settled_keys()
        saved = 0
        no_fill = 0
        for signal in self.trade_signals():
            if (str(signal["analyst"]), str(signal["contract_key"])) in settled:
                continue
            for outcome in self._settle(signal):
                self.database.save_analyst_backtest_outcome(outcome)
                saved += 1
                no_fill += int(outcome["strategy_status"] == "no-fill")
        return {"saved": saved, "no_fill": no_fill, "signals": len(self.trade_signals())}

    def summary(self) -> List[Dict[str, object]]:
        return self.database.analyst_backtest_summary()

    def outcomes(self) -> List[Dict[str, object]]:
        return self.database.analyst_backtest_outcomes()
