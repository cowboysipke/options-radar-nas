"""Read-only HTTP market-data fallbacks.

The adapters in this module intentionally expose market-data endpoints only.
Their transport is injectable so provider payload handling, caching, and quota
behaviour can be tested without network access or credentials.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .provider_types import (
    ProviderCapability,
    ProviderHealth,
    ProviderHistoryBar,
    ProviderMarketSnapshot,
    ProviderOptionContract,
    MarketProvider,
    SourcedValue,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _float(value: Any) -> Optional[float]:
    try:
        return None if value in (None, "", "N/A", "NaN") else float(value)
    except (TypeError, ValueError):
        return None


def _coalesce(*values: Any) -> Any:
    return next((value for value in values if value is not None and value != ""), None)


def _timestamp(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            number = float(value)
            if number > 10_000_000_000_000:
                number /= 1_000_000_000
            elif number > 10_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _quality(value: str) -> str:
    return value


def _contract_parts(contract_key: str) -> Tuple[str, date, float, str]:
    symbol, expiry, strike, kind = contract_key.split("|")
    symbol = symbol.split(".", 1)[-1].upper()
    return symbol, date.fromisoformat(expiry), float(strike), kind.upper()


def _occ_symbol(contract_key: str) -> str:
    symbol, expiry, strike, kind = _contract_parts(contract_key)
    return f"{symbol.replace('.', '')}{expiry:%y%m%d}{kind}{int(round(strike * 1000)):08d}"


def _contract_key(occ: str) -> str:
    raw = str(occ).upper().replace("O:", "").replace(" ", "")
    suffix = raw[-15:]
    root = raw[:-15]
    expiry = datetime.strptime(suffix[:6], "%y%m%d").date()
    kind = suffix[6]
    strike = int(suffix[7:]) / 1000
    strike_text = (f"{strike:.3f}").rstrip("0").rstrip(".")
    return f"US.{root}|{expiry.isoformat()}|{strike_text}|{kind}"


def _date_text(value: Any) -> str:
    return (value.date() if isinstance(value, datetime) else value).isoformat()


OptionContract = ProviderOptionContract
HistoryBar = ProviderHistoryBar


class ProviderRateLimitError(RuntimeError):
    pass


class UrlLibTransport:
    def request(
        self, method: str, url: str, *, headers: Mapping[str, str],
        params: Mapping[str, Any], timeout: float,
    ) -> Mapping[str, Any]:
        query = urllib.parse.urlencode(
            [(key, item) for key, value in params.items()
             for item in (value if isinstance(value, (list, tuple)) else [value])
             if item is not None]
        )
        target = url + (("&" if "?" in url else "?") + query if query else "")
        request = urllib.request.Request(target, method=method, headers=dict(headers))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, Mapping) else {"data": payload}


class _HttpMarketProvider(MarketProvider):
    name = "base"
    quality = "missing"
    default_quota = 1
    quota_window_seconds = 60

    def __init__(
        self, api_key: str = "", *, base_url: str, transport: Any = None,
        timeout: float = 20.0, cache_seconds: int = 60,
        quota: Optional[int] = None, now: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrlLibTransport()
        self.timeout = float(timeout)
        self.cache_seconds = max(0, int(cache_seconds))
        self.quota = max(1, int(quota or self.default_quota))
        self._now, self._monotonic = now, monotonic
        self._cache: Dict[str, Tuple[float, Mapping[str, Any]]] = {}
        self._calls: List[float] = []
        self._request_count = 0
        self._cache_hits = 0
        self._last_error = ""
        self._lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def quota_status(self) -> Dict[str, Any]:
        with self._lock:
            self._trim_calls()
            used = len(self._calls)
            return {
                "limit": self.quota, "used": used, "remaining": max(0, self.quota - used),
                "window_seconds": self.quota_window_seconds, "requests_total": self._request_count,
                "cache_hits": self._cache_hits,
            }

    def cache_status(self) -> Dict[str, Any]:
        with self._lock:
            now = self._monotonic()
            active = sum(1 for stored, _ in self._cache.values() if now - stored <= self.cache_seconds)
            return {"entries": active, "ttl_seconds": self.cache_seconds, "hits": self._cache_hits}

    def _trim_calls(self) -> None:
        now = self._monotonic()
        self._calls[:] = [stamp for stamp in self._calls if now - stamp < self.quota_window_seconds]

    def _invoke_transport(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> Mapping[str, Any]:
        target = getattr(self.transport, "request", self.transport)
        try:
            result = target(method, url, headers=headers, params=params, timeout=self.timeout)
        except TypeError:
            result = target(method, url, headers, params, self.timeout)
        if not isinstance(result, Mapping):
            raise TypeError("transport response must be a mapping")
        return result

    def _request(self, path: str, params: Optional[Mapping[str, Any]] = None, *, headers: Optional[Mapping[str, str]] = None) -> Mapping[str, Any]:
        params = dict(params or {})
        cache_key = json.dumps([path, sorted(params.items())], default=str, separators=(",", ":"))
        now = self._monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] <= self.cache_seconds:
                self._cache_hits += 1
                return cached[1]
            self._trim_calls()
            if len(self._calls) >= self.quota:
                raise ProviderRateLimitError(f"{self.name} quota exhausted")
            self._calls.append(now)
            self._request_count += 1
        try:
            target_url = path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"
            payload = self._invoke_transport("GET", target_url, headers or {}, params)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {str(exc)[:250]}"
            raise
        with self._lock:
            self._cache[cache_key] = (now, payload)
        self._last_error = ""
        return payload

    def _value(self, value: Any, timestamp: Optional[datetime], quality: Optional[str] = None) -> SourcedValue:
        received = self._now()
        market_time = timestamp or received
        try:
            delay = max(0.0, (received - market_time).total_seconds())
        except TypeError:
            delay = 0.0
        return SourcedValue(
            value=value, provider=self.name,
            quality=_quality(quality or self.quality) if value is not None else _quality("missing"),
            market_timestamp=market_time, received_at=received, delay_seconds=delay,
        )

    def _snapshot(self, contract_key: str, timestamp: Optional[datetime], fields: Mapping[str, Any], quality: Optional[str] = None) -> ProviderMarketSnapshot:
        sourced = {name: self._value(value, timestamp, quality) for name, value in fields.items()}
        present = [item for item in sourced.values() if item.value is not None]
        return ProviderMarketSnapshot(
            contract_key=contract_key, provider=self.name, fields=sourced,
            instrument_code=_occ_symbol(contract_key), observed_at=self._now(),
            status=(quality or self.quality) if present else "missing",
            metadata={"quota": self.quota_status()},
        )

    def capabilities(self) -> ProviderCapability:
        return ProviderCapability(
            provider=self.name, broker=False, market=True,
            option_chain=True, snapshots=True, streaming=False, history=True,
            realtime=self.quality == "realtime", delayed=self.quality == "delayed",
            indicative=self.quality == "indicative",
        )

    def _health_probe(self) -> None:
        raise NotImplementedError

    def health(self) -> ProviderHealth:
        checked_at = self._now()
        quota = self.quota_status()
        if not self.configured:
            return ProviderHealth(
                self.name, False, False, "missing", message="API key is not configured",
                quality="missing", endpoint=self.base_url,
                requests_used=quota["used"], requests_limit=quota["limit"],
                last_error="API key is not configured", details={"cache": self.cache_status()},
            )
        try:
            self._health_probe()
            quota = self.quota_status()
            return ProviderHealth(
                self.name, True, True, "ready", quality=self.quality, endpoint=self.base_url,
                requests_used=quota["used"], requests_limit=quota["limit"], last_success=checked_at,
                details={"cache": self.cache_status(), "quota_window_seconds": self.quota_window_seconds},
            )
        except Exception as exc:
            quota = self.quota_status()
            return ProviderHealth(
                self.name, True, False, "error", quality="missing", endpoint=self.base_url,
                requests_used=quota["used"], requests_limit=quota["limit"],
                last_error=f"{type(exc).__name__}: {str(exc)[:250]}", details={"cache": self.cache_status()},
            )

    def subscribe(self, _: Iterable[str]) -> List[str]:
        return []

    def unsubscribe(self, _: Iterable[str]) -> List[str]:
        return []


class AlpacaProvider(_HttpMarketProvider):
    """Alpaca Basic adapter; option values are always labelled indicative."""

    name, quality, default_quota = "alpaca", "indicative", 200

    def __init__(
        self, api_key: str = "", secret_key: str = "", *,
        base_url: str = "https://data.alpaca.markets",
        contracts_base_url: str = "https://paper-api.alpaca.markets",
        feed: str = "indicative", **kwargs: Any,
    ) -> None:
        super().__init__(api_key, base_url=base_url, **kwargs)
        self.secret_key = str(secret_key or "").strip()
        self.contracts_base_url = contracts_base_url.rstrip("/")
        self.feed = str(feed or "indicative").strip().lower()
        self.quality = "realtime" if self.feed == "opra" else "indicative"

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.secret_key)

    @property
    def _headers(self) -> Dict[str, str]:
        return {"APCA-API-KEY-ID": self.api_key, "APCA-API-SECRET-KEY": self.secret_key}

    def _get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        return self._request(path, params, headers=self._headers)

    def _health_probe(self) -> None:
        self._get("/v2/stocks/snapshots", {"symbols": "SPY", "feed": "iex"})

    def search_contracts(self, symbol: str, expiration_date: Optional[date] = None) -> List[OptionContract]:
        params: Dict[str, Any] = {"underlying_symbols": symbol.upper(), "status": "active", "limit": 10000}
        if expiration_date:
            params["expiration_date"] = expiration_date.isoformat()
        output = []
        for _ in range(20):
            payload = self._request(f"{self.contracts_base_url}/v2/options/contracts", params, headers=self._headers)
            rows = payload.get("option_contracts") or payload.get("contracts") or []
            for row in rows if isinstance(rows, list) else []:
                occ = str(row.get("symbol", ""))
                try:
                    key = _contract_key(occ)
                    underlying, expiry, strike, kind = _contract_parts(key)
                    output.append(OptionContract(occ, key, underlying, expiry, strike, kind, self.name))
                except (ValueError, IndexError):
                    continue
            token = payload.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return output

    def get_option_chain(self, symbol: str, expiration_date: Optional[date] = None) -> List[OptionContract]:
        return self.search_contracts(symbol, expiration_date)

    def get_snapshots(self, contract_keys: Iterable[str]) -> Dict[str, ProviderMarketSnapshot]:
        keys = list(dict.fromkeys(contract_keys))
        if not keys:
            return {}
        by_occ = {_occ_symbol(key): key for key in keys}
        rows: Dict[str, Any] = {}
        underlyings = sorted({_contract_parts(key)[0] for key in keys})
        for symbol in underlyings:
            params: Dict[str, Any] = {"feed": self.feed, "limit": 1000}
            for _ in range(20):
                payload = self._get(f"/v1beta1/options/snapshots/{symbol}", params)
                page = payload.get("snapshots") or payload.get("option_snapshots") or {}
                if isinstance(page, Mapping):
                    rows.update(page)
                token = payload.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
        output: Dict[str, ProviderMarketSnapshot] = {}
        for occ, row in rows.items():
            key = by_occ.get(str(occ).replace("O:", ""))
            if not key or not isinstance(row, Mapping):
                continue
            quote = row.get("latestQuote") or row.get("latest_quote") or {}
            trade = row.get("latestTrade") or row.get("latest_trade") or {}
            greek = row.get("greeks") or {}
            market_time = _timestamp(_coalesce(quote.get("t"), quote.get("timestamp"), trade.get("t")))
            output[key] = self._snapshot(key, market_time, {
                "bid": _float(_coalesce(quote.get("bp"), quote.get("bid_price"))),
                "ask": _float(_coalesce(quote.get("ap"), quote.get("ask_price"))),
                "last": _float(_coalesce(trade.get("p"), trade.get("price"))),
                "volume": _float(row.get("dailyBar", {}).get("v") if isinstance(row.get("dailyBar"), Mapping) else None),
                "implied_volatility": _float(_coalesce(row.get("impliedVolatility"), row.get("implied_volatility"))),
                "delta": _float(greek.get("delta")), "gamma": _float(greek.get("gamma")),
                "theta": _float(greek.get("theta")), "vega": _float(greek.get("vega")),
            })
        return output

    def get_history(self, contract_key: str, start: datetime, end: datetime, interval: str = "1Day") -> List[HistoryBar]:
        occ = _occ_symbol(contract_key)
        payload = self._get("/v1beta1/options/bars", {
            "symbols": occ, "timeframe": interval, "start": start.isoformat(), "end": end.isoformat(),
        })
        rows = payload.get("bars") or {}
        rows = rows.get(occ, []) if isinstance(rows, Mapping) else rows
        return self._bars(contract_key, rows, interval, "indicative")

    def get_underlying_bars(self, symbol: str, start: datetime, end: datetime, interval: str = "1Day") -> List[HistoryBar]:
        payload = self._get(f"/v2/stocks/{symbol.upper()}/bars", {"timeframe": interval, "start": start.isoformat(), "end": end.isoformat(), "feed": "iex"})
        return self._bars(symbol.upper(), payload.get("bars") or [], interval, "delayed")

    def aggregate_bars(
        self, ticker: str, start: date, end: date, multiplier: int = 1, timespan: str = "day"
    ) -> List[Dict[str, object]]:
        """Massive-compatible historical bars for the back-test coordinator.

        ``ticker`` is either a stock symbol ("NVDA") or an option OCC symbol
        prefixed with "O:" ("O:MU260814P00881000"). Historical option bars are
        available without an OPRA subscription; only real-time quotes need it.
        """
        timeframe = "1Day" if timespan == "day" else f"{multiplier}Min"
        # Alpaca option bars reject an ``end`` at or beyond "today" with a 403
        # (OPRA agreement); future bars do not exist yet, so clamp to yesterday.
        end = min(end, date.today() - timedelta(days=1))
        if end < start:
            return []
        start_dt = f"{start.isoformat()}T00:00:00Z"
        end_dt = f"{end.isoformat()}T23:59:59Z"
        if ticker.startswith("O:"):
            occ = ticker[2:]
            payload = self._get("/v1beta1/options/bars", {
                "symbols": occ, "timeframe": timeframe, "start": start_dt, "end": end_dt, "limit": 10000,
            })
            rows = payload.get("bars", {}) if isinstance(payload, dict) else {}
            rows = rows.get(occ, []) if isinstance(rows, Mapping) else rows
        else:
            payload = self._get(f"/v2/stocks/{ticker.upper()}/bars", {
                "timeframe": timeframe, "start": start_dt, "end": end_dt, "limit": 10000,
                "feed": "iex",
            })
            rows = payload.get("bars", []) if isinstance(payload, dict) else []
        output: List[Dict[str, object]] = []
        for row in rows:
            ts = _timestamp(_coalesce(row.get("t"), row.get("timestamp"))) if isinstance(row, Mapping) else None
            if ts is None:
                continue
            output.append({
                "t": int(ts.timestamp() * 1000),
                "o": _float(row.get("o")), "h": _float(row.get("h")),
                "l": _float(row.get("l")), "c": _float(row.get("c")),
                "v": _float(row.get("v") or 0),
            })
        return output

    def _bars(self, key: str, rows: Any, interval: str, quality: str) -> List[HistoryBar]:
        return [HistoryBar(key, ts, _float(row.get("o")), _float(row.get("h")), _float(row.get("l")), _float(row.get("c")), _float(row.get("v")), self.name, _quality(quality), interval)
                for row in rows if isinstance(row, Mapping) and (ts := _timestamp(_coalesce(row.get("t"), row.get("timestamp")))) is not None]


class MarketDataAppProvider(_HttpMarketProvider):
    """MarketData.app free-tier adapter; all returned values are delayed."""

    name, quality, default_quota, quota_window_seconds = "marketdata_app", "delayed", 100, 86_400

    def __init__(self, api_key: str = "", *, base_url: str = "https://api.marketdata.app/v1", **kwargs: Any) -> None:
        super().__init__(api_key, base_url=base_url, **kwargs)

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        return self._request(path, params, headers=self._headers)

    def _health_probe(self) -> None:
        self._get("/stocks/quotes/SPY/")

    def search_contracts(self, symbol: str, expiration_date: Optional[date] = None) -> List[OptionContract]:
        params = {"expiration": expiration_date.isoformat()} if expiration_date else {}
        payload = self._get(f"/options/chain/{symbol.upper()}/", params)
        symbols = payload.get("optionSymbol") or payload.get("optionSymbols") or payload.get("symbol") or []
        return _contracts_from_symbols(symbols, self.name)

    def get_option_chain(self, symbol: str, expiration_date: Optional[date] = None) -> List[OptionContract]:
        return self.search_contracts(symbol, expiration_date)

    def get_snapshots(self, contract_keys: Iterable[str]) -> Dict[str, ProviderMarketSnapshot]:
        output = {}
        for key in dict.fromkeys(contract_keys):
            occ = _occ_symbol(key)
            payload = self._get(f"/options/quotes/{occ}/")
            ts = _timestamp(_first(payload, "updated", "timestamp"))
            output[key] = self._snapshot(key, ts, {
                "bid": _float(_first(payload, "bid")), "ask": _float(_first(payload, "ask")),
                "last": _float(_first(payload, "last")), "volume": _float(_first(payload, "volume")),
                "open_interest": _float(_first(payload, "openInterest")),
                "implied_volatility": _float(_first(payload, "iv")), "delta": _float(_first(payload, "delta")),
                "gamma": _float(_first(payload, "gamma")), "theta": _float(_first(payload, "theta")),
                "vega": _float(_first(payload, "vega")),
            })
        return output

    def get_history(self, contract_key: str, start: datetime, end: datetime, interval: str = "daily") -> List[HistoryBar]:
        payload = self._get(f"/options/candles/{interval}/{_occ_symbol(contract_key)}/", {"from": _date_text(start), "to": _date_text(end)})
        return _columnar_bars(payload, contract_key, interval, self.name, "delayed")

    def get_underlying_bars(self, symbol: str, start: datetime, end: datetime, interval: str = "daily") -> List[HistoryBar]:
        payload = self._get(f"/stocks/candles/{interval}/{symbol.upper()}/", {"from": _date_text(start), "to": _date_text(end)})
        return _columnar_bars(payload, symbol.upper(), interval, self.name, "delayed")


class TradierProvider(_HttpMarketProvider):
    """Tradier market-data client for sandbox (delayed) or live (realtime)."""

    name, default_quota = "tradier", 120

    def __init__(self, api_key: str = "", *, sandbox: bool = True, base_url: Optional[str] = None, **kwargs: Any) -> None:
        self.sandbox = bool(sandbox)
        self.quality = "delayed" if self.sandbox else "realtime"
        super().__init__(api_key, base_url=base_url or ("https://sandbox.tradier.com/v1" if sandbox else "https://api.tradier.com/v1"), **kwargs)

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def _get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        return self._request(path, params, headers=self._headers)

    def _health_probe(self) -> None:
        self._get("/markets/quotes", {"symbols": "SPY", "greeks": "false"})

    def search_contracts(self, symbol: str, expiration_date: Optional[date] = None) -> List[OptionContract]:
        if expiration_date is None:
            expirations = self._get("/markets/options/expirations", {"symbol": symbol.upper(), "includeAllRoots": "true"})
            dates = expirations.get("expirations", {}).get("date", []) if isinstance(expirations.get("expirations"), Mapping) else []
            if isinstance(dates, str):
                dates = [dates]
        else:
            dates = [expiration_date.isoformat()]
        output: List[OptionContract] = []
        for expiry in dates:
            payload = self._get("/markets/options/chains", {"symbol": symbol.upper(), "expiration": expiry, "greeks": "true"})
            options = payload.get("options", {}).get("option", []) if isinstance(payload.get("options"), Mapping) else []
            if isinstance(options, Mapping):
                options = [options]
            output.extend(_contracts_from_symbols([row.get("symbol") for row in options if isinstance(row, Mapping)], self.name))
        return output

    def get_option_chain(self, symbol: str, expiration_date: Optional[date] = None) -> List[OptionContract]:
        return self.search_contracts(symbol, expiration_date)

    def get_snapshots(self, contract_keys: Iterable[str]) -> Dict[str, ProviderMarketSnapshot]:
        keys = list(dict.fromkeys(contract_keys))
        if not keys:
            return {}
        by_occ = {_occ_symbol(key): key for key in keys}
        payload = self._get("/markets/quotes", {"symbols": ",".join(by_occ), "greeks": "true"})
        rows = payload.get("quotes", {}).get("quote", []) if isinstance(payload.get("quotes"), Mapping) else []
        if isinstance(rows, Mapping):
            rows = [rows]
        output = {}
        for row in rows:
            occ = str(row.get("symbol", "")).replace("O:", "")
            key = by_occ.get(occ)
            if not key:
                continue
            greek = row.get("greeks") or {}
            ts = _timestamp(_coalesce(row.get("trade_date"), row.get("bid_date"), row.get("ask_date")))
            output[key] = self._snapshot(key, ts, {
                "bid": _float(row.get("bid")), "ask": _float(row.get("ask")),
                "last": _float(row.get("last")), "volume": _float(row.get("volume")),
                "open_interest": _float(row.get("open_interest")),
                "implied_volatility": _float(_coalesce(greek.get("mid_iv"), greek.get("smv_vol"))),
                "delta": _float(greek.get("delta")), "gamma": _float(greek.get("gamma")),
                "theta": _float(greek.get("theta")), "vega": _float(greek.get("vega")),
                "rho": _float(greek.get("rho")),
            })
        return output

    def get_history(self, contract_key: str, start: datetime, end: datetime, interval: str = "daily") -> List[HistoryBar]:
        return self._history(_occ_symbol(contract_key), contract_key, start, end, interval)

    def get_underlying_bars(self, symbol: str, start: datetime, end: datetime, interval: str = "daily") -> List[HistoryBar]:
        return self._history(symbol.upper(), symbol.upper(), start, end, interval)

    def _history(self, provider_symbol: str, key: str, start: datetime, end: datetime, interval: str) -> List[HistoryBar]:
        payload = self._get("/markets/history", {"symbol": provider_symbol, "interval": interval, "start": _date_text(start), "end": _date_text(end)})
        rows = payload.get("history", {}).get("day", []) if isinstance(payload.get("history"), Mapping) else []
        if isinstance(rows, Mapping):
            rows = [rows]
        return [HistoryBar(key, ts, _float(row.get("open")), _float(row.get("high")), _float(row.get("low")), _float(row.get("close")), _float(row.get("volume")), self.name, _quality(self.quality), interval)
                for row in rows if isinstance(row, Mapping) and (ts := _timestamp(_coalesce(row.get("date"), row.get("timestamp")))) is not None]


def _contracts_from_symbols(symbols: Any, provider: str) -> List[OptionContract]:
    if isinstance(symbols, str):
        symbols = [symbols]
    output = []
    for occ in symbols if isinstance(symbols, Sequence) else []:
        try:
            key = _contract_key(str(occ))
            symbol, expiry, strike, kind = _contract_parts(key)
            output.append(OptionContract(str(occ), key, symbol, expiry, strike, kind, provider))
        except (ValueError, IndexError):
            continue
    return output


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if value is not None:
            return value
    return None


def _columnar_bars(payload: Mapping[str, Any], key: str, interval: str, provider: str, quality: str) -> List[HistoryBar]:
    timestamps = payload.get("t") or payload.get("timestamp") or []
    output = []
    for index, raw_time in enumerate(timestamps if isinstance(timestamps, list) else [timestamps]):
        ts = _timestamp(raw_time)
        if ts is None:
            continue
        def item(name: str) -> Any:
            value = payload.get(name) or []
            return value[index] if isinstance(value, list) and index < len(value) else None
        output.append(HistoryBar(key, ts, _float(item("o")), _float(item("h")), _float(item("l")), _float(item("c")), _float(item("v")), provider, _quality(quality), interval))
    return output


# Descriptive aliases keep configuration/factory code readable while preserving
# the short public names used by the dashboard.
AlpacaBasicProvider = AlpacaProvider
MarketDataProvider = MarketDataAppProvider
TradierMarketProvider = TradierProvider


__all__ = [
    "AlpacaProvider", "AlpacaBasicProvider", "MarketDataAppProvider", "MarketDataProvider",
    "TradierProvider", "TradierMarketProvider", "OptionContract", "HistoryBar",
    "ProviderRateLimitError", "UrlLibTransport",
]
