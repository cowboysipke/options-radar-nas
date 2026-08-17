"""Analyst signal accuracy back-test.

Deterministic replay that answers three questions per TRADE signal:
  1. direction hit-rate  — did the underlying move the way the analyst said?
  2. stock pnl           — buy the underlying next open (BULL) / stay flat (BEAR).
  3. option-sell pnl     — sell an ATM PUT (BULL) or ATM CALL (BEAR) at the next
                           open, buy back later, 25% margin basis.

Each signal stores a full daily-bar series (entry -> expiry), so any holding
period (1..N trading days, or "to expiry") can be settled instantly from the
cached series without refetching. Data comes from Massive; no AI involved.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .db import Database
from .massive_client import MassiveClient
from .optimizer import OptionBar, simulate_short_option
from .paper import add_business_days

EXPIRY_HORIZON = 0  # horizon_days==0 means "hold to expiry - 3 days"


def _bar(item: Dict[str, object]) -> Optional[OptionBar]:
    try:
        stamp = datetime.utcfromtimestamp(float(item["t"]) / 1000.0)
        return OptionBar(
            observed_at=stamp, open=float(item["o"]), high=float(item["h"]),
            low=float(item["l"]), close=float(item["c"]), complete=True,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _bars_to_json(bars: Sequence[OptionBar]) -> str:
    return json.dumps([{
        "d": bar.observed_at.date().isoformat(), "o": bar.open,
        "h": bar.high, "l": bar.low, "c": bar.close,
    } for bar in bars])


def _bars_from_json(payload: str) -> List[OptionBar]:
    bars: List[OptionBar] = []
    for item in json.loads(payload):
        bars.append(OptionBar(
            observed_at=datetime.fromisoformat(item["d"]),
            open=float(item["o"]), high=float(item["h"]),
            low=float(item["l"]), close=float(item["c"]), complete=True,
        ))
    return bars


def _atm_ticker(symbol: str, expiry: date, spot: float, option_type: str) -> str:
    strike_code = f"{int(round(spot * 1000)):08d}"
    return f"O:{symbol.replace('.', '')}{expiry:%y%m%d}{option_type.upper()}{strike_code}"


class CompositeBarsSource:
    """Try the primary bars source, fall back to a secondary one."""

    def __init__(self, primary: Any, fallback: Any = None):
        self.primary = primary
        self.fallback = fallback

    def aggregate_bars(self, ticker: str, start: date, end: date,
                       multiplier: int = 1, timespan: str = "day") -> List[Dict[str, object]]:
        try:
            bars = self.primary.aggregate_bars(ticker, start, end, multiplier, timespan)
        except Exception:
            bars = []
        if bars:
            return bars
        if self.fallback is not None:
            try:
                return self.fallback.aggregate_bars(ticker, start, end, multiplier, timespan)
            except Exception:
                return []
        return []


class AnalystBacktestCoordinator:
    """Replay TRADE signals against Massive daily bars and persist outcomes."""

    def __init__(
        self, database: Database, market: MassiveClient,
        horizons: Sequence[int] = (1, 3, 5),
        margin_pct: float = 0.25,
        take_profit_pct: Optional[float] = None,
        stop_loss_pct: float = 0.50,
        exit_before_expiry_days: int = 3,
        min_dte: int = 7,
    ):
        self.database = database
        self.market = market
        self.horizons = tuple(horizons)
        self.margin_pct = margin_pct
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.exit_before_expiry_days = exit_before_expiry_days
        self.min_dte = min_dte

    # -- data access ---------------------------------------------------------

    def trade_signals(self) -> List[Dict[str, Any]]:
        """Distinct (analyst, contract) TRADE signals with the flow contract joined in.

        Excludes signals whose contract is fewer than ``min_dte`` calendar days
        from expiry: near-expiry options carry explosive gamma, so a sell-side
        back-test on them is noise rather than signal.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT p.analyst, p.analyst_family, p.direction,
                          f.contract_key, f.symbol, f.option_type, f.session_date,
                          MAX(p.observed_at) AS observed_at
                   FROM parsed_signals p
                   JOIN flow_events f ON p.flow_event_key = f.event_key
                   WHERE p.decision='TRADE' AND p.direction IN ('BULL','BEAR')
                     AND f.session_date IS NOT NULL
                     AND julianday(f.expiry) - julianday(f.session_date) >= ?
                   GROUP BY p.analyst, f.contract_key
                   ORDER BY f.session_date, p.analyst""",
                (self.min_dte,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _settled_keys(self, horizon_days: Optional[int] = None) -> Set[Tuple[str, str]]:
        query = "SELECT DISTINCT analyst, contract_key FROM analyst_backtest_outcomes"
        params: Tuple[object, ...] = ()
        if horizon_days is not None:
            query += " WHERE horizon_days=?"
            params = (horizon_days,)
        with self.database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return {(str(row["analyst"]), str(row["contract_key"])) for row in rows}

    def _series_keys(self) -> Set[Tuple[str, str]]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT analyst, contract_key FROM analyst_backtest_series").fetchall()
        return {(str(row["analyst"]), str(row["contract_key"])) for row in rows}

    # -- bars ----------------------------------------------------------------

    def _daily_bars(self, ticker: str, start: date, end: date) -> List[OptionBar]:
        try:
            raw = self.market.aggregate_bars(ticker, start, end, 1, "day")
        except Exception:
            return []
        bars = [value for value in (_bar(item) for item in raw) if value is not None]
        return [bar for bar in bars if bar.observed_at.date() <= end]

    def _prefetch_stocks(self, signals: List[Dict[str, Any]]) -> Dict[str, Dict[date, OptionBar]]:
        """One aggregate_bars call per symbol covering every session and expiry it touches."""
        grouped: Dict[str, Tuple[List[date], List[date]]] = {}
        for signal in signals:
            session = date.fromisoformat(str(signal["session_date"]))
            try:
                expiry = date.fromisoformat(str(signal["contract_key"]).split("|")[1])
            except (IndexError, ValueError):
                expiry = session
            sym = str(signal["symbol"])
            entry = grouped.setdefault(sym, ([], []))
            entry[0].append(session)
            entry[1].append(expiry)
        cache: Dict[str, Dict[date, OptionBar]] = {}
        for symbol, (sessions, expiries) in grouped.items():
            start = min(sessions)
            end = add_business_days(max(expiries), 5)
            bars = self._daily_bars(symbol, start, end)
            cache[symbol] = {bar.observed_at.date(): bar for bar in bars}
        return cache

    def _stock_bars(self, symbol: str, session: date, end: date,
                    stock_cache: Optional[Dict[str, Dict[date, OptionBar]]]) -> List[OptionBar]:
        if stock_cache is not None and symbol in stock_cache:
            by_day = stock_cache[symbol]
            return [by_day[day] for day in sorted(by_day) if session <= day <= end]
        return self._daily_bars(symbol, session, end)

    # -- series building -----------------------------------------------------

    def _build_series(self, signal: Dict[str, Any],
                      stock_cache: Optional[Dict[str, Dict[date, OptionBar]]]) -> Optional[Dict[str, Any]]:
        session = date.fromisoformat(str(signal["session_date"]))
        symbol = str(signal["symbol"])
        contract = str(signal["contract_key"])
        direction = str(signal["direction"])
        try:
            expiry = date.fromisoformat(contract.split("|")[1])
        except (IndexError, ValueError):
            expiry = session

        underlying_end = add_business_days(expiry, 5)
        underlying_all = self._stock_bars(symbol, session, underlying_end, stock_cache)
        entry_day = add_business_days(session, 1)

        spot = underlying_all[0].close if underlying_all else None
        sell_type = "P" if direction == "BULL" else "C"
        option_all: List[OptionBar] = []
        atm_strike: Optional[float] = None
        atm_ticker: Optional[str] = None
        if spot:
            for strike in (round(spot), round(spot / 5.0) * 5.0):
                ticker = _atm_ticker(symbol, expiry, strike, sell_type)
                candidate = self._daily_bars(ticker, entry_day, underlying_end)
                if candidate:
                    option_all = candidate
                    atm_strike = float(strike)
                    atm_ticker = ticker
                    break
        if atm_strike is None:
            return None
        return {
            "analyst": str(signal["analyst"]),
            "analyst_family": str(signal["analyst_family"]),
            "contract_key": contract,
            "symbol": symbol,
            "session_date": signal["session_date"],
            "direction": direction,
            "option_type": str(signal["option_type"]),
            "expiry": expiry.isoformat(),
            "entry_day": entry_day.isoformat(),
            "atm_strike": atm_strike,
            "atm_ticker": atm_ticker or "",
            "underlying_bars_json": _bars_to_json(underlying_all),
            "option_bars_json": _bars_to_json(option_all),
            "observed_at": signal["observed_at"],
        }

    # -- settlement ----------------------------------------------------------

    def _horizon_bars(self, bars: List[OptionBar], session: date, entry_day: date,
                      expiry: date, horizon_days: int, is_option: bool) -> List[OptionBar]:
        """Slice a daily series to a holding period.

        horizon_days==0 -> hold to expiry - exit_before_expiry_days (calendar days).
        Otherwise hold horizon_days trading days from entry (option) or session (stock).
        """
        if horizon_days == EXPIRY_HORIZON:
            limit = expiry - timedelta(days=self.exit_before_expiry_days)
            return [bar for bar in bars if bar.observed_at.date() <= limit]
        if is_option:
            return bars[:horizon_days]
        # Stock series starts on the signal day; N trading days later is index N.
        return bars[:horizon_days + 1]

    def _settle_from_series(self, series: Dict[str, Any], horizon_days: int) -> Dict[str, object]:
        session = date.fromisoformat(str(series["session_date"]))
        expiry = date.fromisoformat(str(series["expiry"]))
        entry_day = date.fromisoformat(str(series["entry_day"]))
        direction = str(series["direction"])
        underlying_all = _bars_from_json(str(series["underlying_bars_json"]))
        option_all = _bars_from_json(str(series["option_bars_json"]))

        underlying_bars = self._horizon_bars(underlying_all, session, entry_day, expiry, horizon_days, is_option=False)
        option_bars = self._horizon_bars(option_all, session, entry_day, expiry, horizon_days, is_option=True)

        underlying_change = None
        direction_correct = None
        if len(underlying_bars) >= 2:
            base = underlying_bars[0].close
            last = underlying_bars[-1].close
            if base and last:
                underlying_change = (last - base) / base
                direction_correct = 1 if (underlying_change > 0) == (direction == "BULL") else 0

        stock_pnl = None
        if direction == "BULL" and len(underlying_bars) >= 2:
            stock_entry = underlying_bars[1].open
            stock_exit = underlying_bars[-1].close
            if stock_entry and stock_exit:
                stock_pnl = (stock_exit - stock_entry) / stock_entry
        elif direction == "BEAR" and len(underlying_bars) >= 1:
            stock_pnl = 0.0

        strategy = simulate_short_option(
            option_bars, quantity=1,
            notional_per_contract=(float(series["atm_strike"]) * 100.0),
            margin_pct=self.margin_pct,
            take_profit_pct=self.take_profit_pct,
            stop_loss_pct=self.stop_loss_pct,
        )
        premium_pct = None
        if strategy.entry_price and strategy.entry_price > 0:
            premium_pct = (strategy.entry_price - strategy.exit_price) / strategy.entry_price

        return {
            "analyst": series["analyst"],
            "analyst_family": series["analyst_family"],
            "contract_key": series["contract_key"],
            "symbol": series["symbol"],
            "session_date": series["session_date"],
            "direction": direction,
            "option_type": series["option_type"],
            "horizon_days": horizon_days,
            "strategy_status": strategy.status,
            "strategy_pnl_pct": strategy.pnl_pct,
            "strategy_premium_pct": premium_pct,
            "stock_pnl_pct": stock_pnl,
            "direction_correct": direction_correct,
            "underlying_change_pct": underlying_change,
            "atm_ticker": series["atm_ticker"],
            "strategy_exit_reason": strategy.exit_reason if strategy.status == "filled" else None,
            "observed_at": series["observed_at"],
        }

    # -- entry points --------------------------------------------------------

    def build_series(self) -> Dict[str, int]:
        """Fetch and persist full bar series for every TRADE signal (idempotent)."""
        existing = self._series_keys()
        pending = [s for s in self.trade_signals()
                   if (str(s["analyst"]), str(s["contract_key"])) not in existing]
        stock_cache = self._prefetch_stocks(pending) if pending else {}
        built = 0
        skipped = 0
        for signal in pending:
            series = self._build_series(signal, stock_cache)
            if series is None:
                skipped += 1
                continue
            self.database.save_analyst_backtest_series(series)
            built += 1
        return {"built": built, "skipped": skipped}

    def settle_horizon(self, horizon_days: int) -> Dict[str, int]:
        """Settle every signal at one holding period; idempotent (ON CONFLICT)."""
        settled = self._settled_keys(horizon_days)
        saved = 0
        no_fill = 0
        for series in self.database.analyst_backtest_series_all():
            key = (str(series["analyst"]), str(series["contract_key"]))
            if key in settled:
                continue
            outcome = self._settle_from_series(series, horizon_days)
            self.database.save_analyst_backtest_outcome(outcome)
            saved += 1
            no_fill += int(outcome["strategy_status"] == "no-fill")
        return {"saved": saved, "no_fill": no_fill, "horizon_days": horizon_days}

    def settle_analyst_horizon(self, analyst: str, horizon_days: int) -> int:
        """Settle one analyst at one holding period (used for on-demand detail)."""
        settled = self._settled_keys(horizon_days)
        saved = 0
        for series in self.database.analyst_backtest_series_all():
            if str(series["analyst"]) != analyst:
                continue
            key = (str(series["analyst"]), str(series["contract_key"]))
            if key in settled:
                continue
            outcome = self._settle_from_series(series, horizon_days)
            self.database.save_analyst_backtest_outcome(outcome)
            saved += 1
        return saved

    def run(self) -> Dict[str, int]:
        """Build series then settle the default horizons plus the expiry horizon."""
        built = self.build_series()
        totals: Dict[str, int] = {"built": built["built"], "skipped_series": built["skipped"]}
        for horizon in (*self.horizons, EXPIRY_HORIZON):
            result = self.settle_horizon(horizon)
            totals[f"h{horizon}_saved"] = result["saved"]
        return totals

    def summary_for_horizon(self, horizon_days: int) -> List[Dict[str, object]]:
        return self.database.analyst_backtest_summary_for_horizon(horizon_days)

    def daily_summary(self, horizon_days: int) -> List[Dict[str, object]]:
        return self.database.analyst_backtest_daily_summary(horizon_days)

    def outcomes(self) -> List[Dict[str, object]]:
        return self.database.analyst_backtest_outcomes()
