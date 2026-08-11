from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .models import MarketSnapshot


JsonGet = Callable[[str, float], Dict[str, object]]


def _read_api_key() -> str:
    key = os.getenv("MASSIVE_API_KEY", "").strip()
    secret_path = os.getenv("MASSIVE_API_KEY_FILE", "").strip()
    if not key and secret_path and Path(secret_path).is_file():
        key = Path(secret_path).read_text(encoding="utf-8").strip()
    return key


def _default_get(url: str, timeout: float) -> Dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "options-radar/0.2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _number(value: object) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class MassiveClient:
    """Massive aggregate-bar adapter with explicit per-field provenance.

    EOD aggregates supply close/volume. They do not pretend to supply native
    bid, ask, open interest, IV or Greeks.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.massive.com",
        timeout: float = 30.0,
        requests_per_minute: int = 5,
        cache_seconds: int = 900,
        http_get: Optional[JsonGet] = None,
    ):
        self.api_key = api_key if api_key is not None else _read_api_key()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.requests_per_minute = max(1, requests_per_minute)
        self.cache_seconds = cache_seconds
        self.http_get = http_get or _default_get
        self._calls: List[float] = []
        self._cache: Dict[str, Tuple[float, Dict[str, object]]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def split_contract_key(contract_key: str) -> Tuple[str, date, float, str]:
        symbol, expiry_text, strike_text, option_type = contract_key.split("|")
        return symbol.split(".", 1)[-1].upper(), date.fromisoformat(expiry_text), float(strike_text), option_type

    @staticmethod
    def occ_ticker(contract_key: str) -> str:
        symbol, expiry, strike, option_type = MassiveClient.split_contract_key(contract_key)
        strike_code = f"{int(round(strike * 1000)):08d}"
        return f"O:{symbol.replace('.', '')}{expiry:%y%m%d}{option_type.upper()}{strike_code}"

    def _rate_limit(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._calls = [stamp for stamp in self._calls if now - stamp < 60.0]
            if len(self._calls) >= self.requests_per_minute:
                wait = 60.0 - (now - self._calls[0])
            else:
                wait = 0.0
        if wait > 0:
            time.sleep(wait)
        with self._lock:
            self._calls.append(time.monotonic())

    def _get(self, path: str, params: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        params = dict(params or {})
        if self.api_key:
            params["apiKey"] = self.api_key
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        cached = self._cache.get(url)
        now = time.monotonic()
        if cached and now - cached[0] <= self.cache_seconds:
            return cached[1]
        self._rate_limit()
        payload = self.http_get(url, self.timeout)
        self._cache[url] = (now, payload)
        return payload

    def aggregate_bars(
        self, ticker: str, start: date, end: date, multiplier: int = 1, timespan: str = "day"
    ) -> List[Dict[str, object]]:
        encoded = urllib.parse.quote(ticker, safe=":")
        payload = self._get(
            f"/v2/aggs/ticker/{encoded}/range/{multiplier}/{timespan}/{start.isoformat()}/{end.isoformat()}",
            {"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        results = payload.get("results", [])
        return [dict(item) for item in results] if isinstance(results, list) else []

    def exact_snapshot(self, contract_key: str, as_of: Optional[date] = None) -> MarketSnapshot:
        observed = datetime.now(timezone.utc).replace(tzinfo=None)
        if not self.api_key:
            return MarketSnapshot(
                contract_key=contract_key, observed_at=observed, provider="massive",
                data_status="missing", field_quality={"last": "missing", "volume": "missing"},
            )
        end = as_of or date.today()
        try:
            bars = self.aggregate_bars(self.occ_ticker(contract_key), end - timedelta(days=14), end)
            active = [bar for bar in bars if (_number(bar.get("v")) or 0) > 0]
            if not bars:
                return MarketSnapshot(
                    contract_key=contract_key, observed_at=observed, provider="massive",
                    data_status="missing", field_quality={"last": "missing", "volume": "missing"},
                )
            latest = bars[-1]
            return MarketSnapshot(
                contract_key=contract_key,
                observed_at=datetime.fromtimestamp(float(latest.get("t", 0)) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
                if latest.get("t") else observed,
                last=_number(latest.get("c")), volume=_number(latest.get("v")),
                provider="massive", data_status="eod", recent_active_days=len(active),
                field_quality={
                    "last": "native", "volume": "native", "bid": "missing", "ask": "missing",
                    "open_interest": "missing", "implied_volatility": "missing", "delta": "missing",
                },
            )
        except Exception as exc:
            return MarketSnapshot(
                contract_key=contract_key, observed_at=observed, provider="massive",
                data_status=f"error:{type(exc).__name__}",
                field_quality={"last": "missing", "volume": "missing"},
            )

    def enrich_underlying(self, snapshot: MarketSnapshot, as_of: Optional[date] = None) -> MarketSnapshot:
        symbol, _, _, _ = self.split_contract_key(snapshot.contract_key)
        end = as_of or date.today()
        if not self.api_key:
            return snapshot
        try:
            bars = self.aggregate_bars(symbol, end - timedelta(days=45), end)
            if not bars:
                return snapshot
            latest = bars[-1]
            ranges: List[float] = []
            previous_close: Optional[float] = None
            for bar in bars:
                high, low, close = _number(bar.get("h")), _number(bar.get("l")), _number(bar.get("c"))
                if high is None or low is None or close is None:
                    continue
                ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)) if previous_close else high - low)
                previous_close = close
            snapshot.underlying_price = _number(latest.get("c"))
            snapshot.underlying_previous_high = _number(latest.get("h"))
            snapshot.underlying_previous_low = _number(latest.get("l"))
            snapshot.underlying_atr14 = sum(ranges[-14:]) / len(ranges[-14:]) if ranges else None
            snapshot.field_quality.update({
                "underlying_price": "native", "underlying_previous_high": "native",
                "underlying_previous_low": "native", "underlying_atr14": "estimated",
            })
        except Exception:
            snapshot.field_quality.setdefault("underlying_price", "missing")
        return snapshot
