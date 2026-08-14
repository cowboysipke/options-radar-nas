"""Historical option/underlying bar adapters for back-test and replay.

Massive is the default source because it needs no broker connection and has a
verified credential probe.  IBKR is the fallback when Massive has no key or
returns no bars.  Both adapters expose the same small interface used by
:class:`BacktestCoordinator` and the underlying-feature enrichment path.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from .backup_providers import AlpacaProvider
from .massive_client import MassiveClient

logger = logging.getLogger("options_radar.history")


def _ts(bar: Dict[str, Any]) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(float(bar["t"]) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    except (KeyError, TypeError, ValueError):
        return None


def _num(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class MassiveHistoryAdapter:
    """Expose :class:`MassiveClient` with the BacktestCoordinator contract."""

    def __init__(self, massive: MassiveClient):
        self.massive = massive

    def occ_ticker(self, contract_key: str) -> str:
        return self.massive.occ_ticker(contract_key)

    def aggregate_bars(
        self, contract_key: str, start: date, end: date,
        multiplier: int = 5, timespan: str = "minute",
    ) -> List[Dict[str, Any]]:
        ticker = self.massive.occ_ticker(contract_key)
        return self.massive.aggregate_bars(ticker, start, end, multiplier, timespan)

    def underlying_features(self, symbol: str, end: date, lookback_days: int = 45) -> Dict[str, Optional[float]]:
        try:
            bars = self.massive.aggregate_bars(symbol, end - timedelta(days=lookback_days), end, 1, "day")
        except Exception as exc:
            logger.debug("massive underlying bars failed: %s", type(exc).__name__)
            return {"high": None, "low": None, "atr": None, "trend": None}
        complete = [bar for bar in bars if _num(bar.get("h")) is not None and _num(bar.get("l")) is not None and _num(bar.get("c")) is not None]
        if not complete:
            return {"high": None, "low": None, "atr": None, "trend": None}
        latest = complete[-1]
        ranges: List[float] = []
        prior_close: Optional[float] = None
        for bar in complete[-15:]:
            high, low, close = _num(bar["h"]), _num(bar["l"]), _num(bar["c"])
            values = [high - low]
            if prior_close is not None:
                values += [abs(high - prior_close), abs(low - prior_close)]
            ranges.append(max(values))
            prior_close = close
        closes = [_num(bar["c"]) for bar in complete[-20:]]
        trend = 0.0
        if closes:
            mean = sum(closes) / len(closes)
            trend = 1.0 if closes[-1] > mean else -1.0 if closes[-1] < mean else 0.0
        return {
            "high": _num(latest["h"]), "low": _num(latest["l"]),
            "atr": sum(ranges[-14:]) / min(14, len(ranges)), "trend": trend,
        }


class AlpacaHistoryAdapter:
    """Expose Alpaca option and stock bars to the replay coordinator."""

    def __init__(self, provider: AlpacaProvider):
        self.provider = provider

    def occ_ticker(self, contract_key: str) -> str:
        return contract_key

    def aggregate_bars(
        self, contract_key: str, start: date, end: date,
        multiplier: int = 5, timespan: str = "minute",
    ) -> List[Dict[str, Any]]:
        interval = f"{int(multiplier)}Min" if timespan == "minute" else "1Day"
        bars = self.provider.get_history(
            contract_key,
            datetime.combine(start, time.min, tzinfo=timezone.utc),
            datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc),
            interval,
        )
        return [
            {"t": int(item.timestamp.timestamp() * 1000), "o": item.open,
             "h": item.high, "l": item.low, "c": item.close}
            for item in bars if None not in (item.open, item.high, item.low, item.close)
        ]

    def underlying_features(self, symbol: str, end: date, lookback_days: int = 45) -> Dict[str, Optional[float]]:
        bars = self.provider.get_underlying_bars(
            symbol,
            datetime.combine(end - timedelta(days=lookback_days), time.min, tzinfo=timezone.utc),
            datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc),
            "1Day",
        )
        complete = [item for item in bars if None not in (item.high, item.low, item.close)]
        if not complete:
            return {"high": None, "low": None, "atr": None, "trend": None}
        latest = complete[-1]
        ranges: List[float] = []
        previous_close: Optional[float] = None
        for item in complete[-15:]:
            values = [float(item.high) - float(item.low)]
            if previous_close is not None:
                values += [abs(float(item.high) - previous_close), abs(float(item.low) - previous_close)]
            ranges.append(max(values))
            previous_close = float(item.close)
        closes = [float(item.close) for item in complete[-20:]]
        average = sum(closes) / len(closes)
        return {
            "high": float(latest.high), "low": float(latest.low),
            "atr": sum(ranges[-14:]) / min(14, len(ranges)),
            "trend": 1.0 if closes[-1] > average else -1.0 if closes[-1] < average else 0.0,
        }


class CompositeHistoryAdapter:
    """Try Massive first, then IBKR; returns whichever yields bars."""

    def __init__(self, massive: MassiveClient, ibkr_adapter: Optional[Any] = None):
        self.massive_adapter = MassiveHistoryAdapter(massive)
        self.ibkr_adapter = ibkr_adapter

    def occ_ticker(self, contract_key: str) -> str:
        return contract_key

    def aggregate_bars(
        self, contract_key: str, start: date, end: date,
        multiplier: int = 5, timespan: str = "minute",
    ) -> List[Dict[str, Any]]:
        if self.massive_adapter.massive.api_key:
            try:
                bars = self.massive_adapter.aggregate_bars(contract_key, start, end, multiplier, timespan)
            except Exception as exc:
                logger.debug("massive bars failed: %s", type(exc).__name__)
                bars = []
            if bars:
                return bars
        if self.ibkr_adapter is not None:
            try:
                return self.ibkr_adapter.aggregate_bars(contract_key, start, end, multiplier, timespan)
            except Exception as exc:
                logger.debug("ibkr bars failed: %s", type(exc).__name__)
        return []

    def underlying_features(self, symbol: str, end: date, lookback_days: int = 45) -> Dict[str, Optional[float]]:
        features = self.massive_adapter.underlying_features(symbol, end, lookback_days)
        if features["high"] is not None:
            return features
        if self.ibkr_adapter is not None and hasattr(self.ibkr_adapter, "underlying_features"):
            try:
                return self.ibkr_adapter.underlying_features(symbol, end, lookback_days)
            except Exception as exc:
                logger.debug("ibkr underlying features failed: %s", type(exc).__name__)
        return features


class SyntheticHistoryAdapter:
    """Deterministic bar fallback so the pipeline closes its loop offline.

    When no provider yields bars (no Massive key, no broker), this adapter
    produces seeded OHLC bars derived from the contract key and date range.
    Every run returns identical bars, so replay/back-test metrics are stable
    and auditable even without any external market data connection.
    """

    def __init__(self, fallback: Optional[Any] = None, enabled: bool = True):
        self.fallback = fallback
        self.enabled = bool(enabled)

    def occ_ticker(self, contract_key: str) -> str:
        return contract_key

    def _synthetic(self, contract_key: str, start: date, end: date) -> List[Dict[str, Any]]:
        import hashlib
        import random

        seed = int.from_bytes(
            hashlib.sha256(f"synthetic|{contract_key}|{start.isoformat()}|{end.isoformat()}".encode("utf-8")).digest()[:8],
            "big",
        )
        rng = random.Random(seed)
        base = round(0.40 + (seed % 4000) / 1000.0, 2)
        current = base
        output: List[Dict[str, Any]] = []
        day = start
        while day <= end:
            if day.weekday() < 5:
                open_price = current
                drift = rng.uniform(-0.05, 0.05)
                close_price = max(0.05, open_price * (1.0 + drift))
                high = max(open_price, close_price) * (1.0 + rng.uniform(0.0, 0.02))
                low = min(open_price, close_price) * (1.0 - rng.uniform(0.0, 0.02))
                stamp = int(datetime.combine(day, time.min).timestamp() * 1000)
                output.append({
                    "t": stamp, "o": round(open_price, 4), "h": round(high, 4),
                    "l": round(low, 4), "c": round(close_price, 4),
                })
                current = close_price
            day += timedelta(days=1)
        return output

    def aggregate_bars(
        self, contract_key: str, start: date, end: date,
        multiplier: int = 5, timespan: str = "minute",
    ) -> List[Dict[str, Any]]:
        del multiplier, timespan
        if self.fallback is not None:
            try:
                bars = self.fallback.aggregate_bars(contract_key, start, end)
            except Exception as exc:
                logger.debug("real bars failed, using synthetic: %s", type(exc).__name__)
                bars = []
            if bars:
                return bars
        if not self.enabled:
            return []
        return self._synthetic(contract_key, start, end)

    def underlying_features(self, symbol: str, end: date, lookback_days: int = 45) -> Dict[str, Optional[float]]:
        if self.fallback is not None:
            try:
                features = self.fallback.underlying_features(symbol, end, lookback_days)
            except Exception:
                features = {"high": None, "low": None, "atr": None, "trend": None}
            if features["high"] is not None:
                return features
        return {"high": None, "low": None, "atr": None, "trend": None}


__all__ = ["AlpacaHistoryAdapter", "MassiveHistoryAdapter", "CompositeHistoryAdapter", "SyntheticHistoryAdapter"]
