"""Analyst signal accuracy back-test.

Deterministic replay that answers three questions per TRADE signal:
  1. direction hit-rate  — did the underlying move the way the analyst said?
  2. stock pnl           — buy the underlying next open (BULL) / stay flat (BEAR).
  3. option-sell pnl     — sell an ATM PUT (BULL) or ATM CALL (BEAR) at the next
                           open, buy back at the horizon close, 25% margin basis.

Data comes from Massive daily bars (the underlying and the ATM option
contract). No AI is involved; every number is reproducible.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .db import Database
from .massive_client import MassiveClient
from .optimizer import OptionBar, simulate_short_option
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


def _atm_ticker(symbol: str, expiry: date, spot: float, option_type: str) -> str:
    strike_code = f"{int(round(spot * 1000)):08d}"
    return f"O:{symbol.replace('.', '')}{expiry:%y%m%d}{option_type.upper()}{strike_code}"


class AnalystBacktestCoordinator:
    """Replay TRADE signals against Massive daily bars and persist outcomes."""

    def __init__(
        self, database: Database, market: MassiveClient,
        horizons: Sequence[int] = (1, 3, 5),
        margin_pct: float = 0.25,
        take_profit_pct: Optional[float] = None,
        stop_loss_pct: float = 0.50,
    ):
        self.database = database
        self.market = market
        self.horizons = tuple(horizons)
        self.margin_pct = margin_pct
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

    def _prefetch_stocks(self, signals: List[Dict[str, Any]]) -> Dict[str, Dict[date, OptionBar]]:
        """One aggregate_bars call per symbol covering every session it appears in."""
        grouped: Dict[str, List[date]] = {}
        for signal in signals:
            session = date.fromisoformat(str(signal["session_date"]))
            grouped.setdefault(str(signal["symbol"]), []).append(session)
        cache: Dict[str, Dict[date, OptionBar]] = {}
        for symbol, sessions in grouped.items():
            start = min(sessions)
            end = add_business_days(max(sessions), max(self.horizons))
            bars = self._daily_bars(symbol, start, end)
            cache[symbol] = {bar.observed_at.date(): bar for bar in bars}
        return cache

    def _stock_bars(self, symbol: str, session: date, max_end: date,
                    stock_cache: Optional[Dict[str, Dict[date, OptionBar]]]) -> List[OptionBar]:
        if stock_cache is not None and symbol in stock_cache:
            by_day = stock_cache[symbol]
            return [by_day[day] for day in sorted(by_day) if session <= day <= max_end]
        return self._daily_bars(symbol, session, max_end)

    # -- per-signal settlement ----------------------------------------------

    def _settle(self, signal: Dict[str, Any],
                stock_cache: Optional[Dict[str, Dict[date, OptionBar]]] = None) -> List[Dict[str, object]]:
        session = date.fromisoformat(str(signal["session_date"]))
        symbol = str(signal["symbol"])
        contract = str(signal["contract_key"])
        option_type = str(signal["option_type"])
        direction = str(signal["direction"])
        try:
            expiry = date.fromisoformat(contract.split("|")[1])
        except (IndexError, ValueError):
            expiry = session

        # Fetch each series once (covering the widest horizon) and slice per
        # horizon to keep the request count at 2 per signal instead of 6.
        max_end = add_business_days(session, max(self.horizons))
        underlying_all = self._stock_bars(symbol, session, max_end, stock_cache)
        entry_day = add_business_days(session, 1)

        # ATM contract for the sell-side leg: BULL sells an ATM PUT, BEAR sells
        # an ATM CALL. Strike = signal-day close rounded; fall back to $5 grid
        # when the rounded strike has no data on Massive.
        spot = underlying_all[0].close if underlying_all else None
        sell_type = "P" if direction == "BULL" else "C"
        contract_all: List[OptionBar] = []
        atm_strike: Optional[float] = None
        if spot:
            for strike in (round(spot), round(spot / 5.0) * 5.0):
                candidate = self._daily_bars(_atm_ticker(symbol, expiry, strike, sell_type), entry_day, max_end)
                if candidate:
                    contract_all = candidate
                    atm_strike = float(strike)
                    break

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

            # Stock leg: buy next open for BULL, stay flat for BEAR.
            stock_pnl = None
            if direction == "BULL" and len(underlying_bars) >= 2:
                stock_entry = underlying_bars[1].open
                stock_exit = underlying_bars[-1].close
                if stock_entry and stock_exit:
                    stock_pnl = (stock_exit - stock_entry) / stock_entry
            elif direction == "BEAR" and len(underlying_bars) >= 1:
                stock_pnl = 0.0

            strategy = simulate_short_option(
                contract_bars, quantity=1,
                notional_per_contract=(atm_strike * 100.0) if atm_strike else None,
                margin_pct=self.margin_pct,
                take_profit_pct=self.take_profit_pct,
                stop_loss_pct=self.stop_loss_pct,
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
                "stock_pnl_pct": stock_pnl,
                "direction_correct": direction_correct,
                "underlying_change_pct": underlying_change,
                "observed_at": signal["observed_at"],
            })
        return outcomes

    # -- entry points --------------------------------------------------------

    def run(self) -> Dict[str, int]:
        """Settle unsettled TRADE signals; idempotent (ON CONFLICT updates)."""
        settled = self._settled_keys()
        pending = [signal for signal in self.trade_signals()
                   if (str(signal["analyst"]), str(signal["contract_key"])) not in settled]
        stock_cache = self._prefetch_stocks(pending) if pending else {}
        saved = 0
        no_fill = 0
        for signal in pending:
            for outcome in self._settle(signal, stock_cache=stock_cache):
                self.database.save_analyst_backtest_outcome(outcome)
                saved += 1
                no_fill += int(outcome["strategy_status"] == "no-fill")
        return {"saved": saved, "no_fill": no_fill, "signals": len(self.trade_signals())}

    def summary(self) -> List[Dict[str, object]]:
        return self.database.analyst_backtest_summary()

    def outcomes(self) -> List[Dict[str, object]]:
        return self.database.analyst_backtest_outcomes()
