"""Read-only Interactive Brokers TWS / IB Gateway provider.

``ib_insync`` is deliberately an optional dependency.  Importing this module is
therefore safe on machines that only use Futu or the Flex adapter.  Tests and
other runtimes can inject an object implementing the small backend surface used
below; no order, exercise, or account-mutation operation is exposed.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


from .provider_types import (
    AccountSnapshot,
    ProviderCapability,
    ProviderHealth,
    ProviderHistoryBar,
    ProviderMarketSnapshot,
    ProviderOptionContract,
    SourcedValue,
)


DEFAULT_PORTS: Tuple[int, ...] = (7497, 7496, 4002, 4001)
PORT_LABELS = {
    7497: "tws-paper",
    7496: "tws-live",
    4002: "gateway-paper",
    4001: "gateway-live",
}


@dataclass(frozen=True)
class IBKREndpoint:
    host: str
    port: int
    kind: str


@dataclass(frozen=True)
class IBKRPosition:
    symbol: str
    security_type: str
    contract_key: str
    quantity: SourcedValue
    average_cost: SourcedValue
    market_price: SourcedValue
    market_value: SourcedValue
    currency: str = "USD"


@dataclass(frozen=True)
class IBKROptionContract(ProviderOptionContract):
    exchange: str = "SMART"
    currency: str = "USD"
    multiplier: int = 100
    contract_id: Optional[int] = None
    local_symbol: Optional[str] = None
    trading_class: Optional[str] = None


@dataclass
class IBKRMarketSnapshot(ProviderMarketSnapshot):
    @property
    def bid(self) -> SourcedValue:
        return self.fields["bid"]

    @property
    def ask(self) -> SourcedValue:
        return self.fields["ask"]

    @property
    def last(self) -> SourcedValue:
        return self.fields["last"]

    @property
    def midpoint(self) -> Optional[float]:
        bid, ask = self.bid.value, self.ask.value
        if bid is not None and ask is not None and ask >= bid:
            return (bid + ask) / 2.0
        return self.last.value


def _number(value: Any) -> Optional[float]:
    if value in (None, "", "nan", "NaN"):
        return None
    try:
        parsed = float(value)
        return parsed if parsed == parsed and abs(parsed) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _attr(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _masked_account(value: str) -> str:
    return "acct-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def _expiry(value: Any) -> Optional[date]:
    text = str(value or "").replace("-", "")
    try:
        return datetime.strptime(text[:8], "%Y%m%d").date()
    except ValueError:
        return None


class IBKRProvider:
    """TWS/Gateway market and portfolio access restricted to read operations."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: Optional[int] = None,
        client_id: int = 87,
        *,
        backend: Any = None,
        backend_factory: Optional[Callable[[], Any]] = None,
        socket_factory: Callable[..., Any] = socket.create_connection,
        now: Callable[[], datetime] = _utcnow,
        connect_timeout: float = 1.5,
        market_data_type: int = 3,
    ) -> None:
        self.host = host
        self.port = int(port) if port is not None else None
        self.client_id = int(client_id)
        self._backend = backend
        self._backend_factory = backend_factory
        self._socket_factory = socket_factory
        self._now = now
        self.connect_timeout = float(connect_timeout)
        self._request_count = 0
        self._last_error = ""
        self._market_data_type = int(market_data_type) if int(market_data_type) in (1, 2, 3, 4) else 3
        self._contract_cache: Dict[str, IBKROptionContract] = {}

    @staticmethod
    def capabilities() -> ProviderCapability:
        return ProviderCapability(
            provider="ibkr", broker=True, market=True, accounts=True,
            positions=True, watchlists=False,
            option_chain=True, snapshots=True, streaming=True, history=True,
            realtime=True, delayed=True,
        )

    def discover(self, timeout: float = 0.15) -> List[IBKREndpoint]:
        found: List[IBKREndpoint] = []
        for port in DEFAULT_PORTS:
            connection = None
            try:
                connection = self._socket_factory((self.host, port), timeout=timeout)
                found.append(IBKREndpoint(self.host, port, PORT_LABELS[port]))
            except (OSError, TimeoutError):
                continue
            finally:
                close = getattr(connection, "close", None)
                if callable(close):
                    close()
        return found

    def _load_backend(self) -> Any:
        if self._backend is not None:
            return self._backend
        if self._backend_factory is not None:
            self._backend = self._backend_factory()
            return self._backend
        try:
            from ib_insync import IB  # type: ignore
        except ImportError as exc:
            raise RuntimeError("optional dependency ib_insync is not installed") from exc
        self._backend = IB()
        return self._backend

    def _connected(self, backend: Optional[Any] = None) -> bool:
        backend = backend or self._backend
        if backend is None:
            return False
        check = getattr(backend, "isConnected", None)
        return bool(check()) if callable(check) else bool(getattr(backend, "connected", False))

    def connect(self) -> bool:
        # ib_insync expects an event loop in every calling thread.  Dashboard
        # handlers run in ThreadingHTTPServer workers, where Python does not
        # create one automatically.
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        backend = self._load_backend()
        if self._connected(backend):
            return True
        port = self.port
        if port is None:
            endpoints = self.discover()
            if not endpoints:
                raise ConnectionError("no TWS or IB Gateway endpoint discovered")
            port = endpoints[0].port
            self.port = port
        # ib_insync passes this through as the IB API readOnly flag.
        backend.connect(
            self.host, port, clientId=self.client_id,
            timeout=self.connect_timeout, readonly=True,
        )
        if not self._connected(backend):
            raise ConnectionError("IBKR backend did not enter connected state")
        return True

    def close(self) -> None:
        if self._backend is not None:
            disconnect = getattr(self._backend, "disconnect", None)
            if callable(disconnect):
                disconnect()

    def health(self) -> ProviderHealth:
        checked_at = self._now()
        try:
            self.connect()
            accounts = list(self._backend.managedAccounts()) if hasattr(self._backend, "managedAccounts") else []
            return ProviderHealth(
                provider="ibkr", configured=True, connected=True, status="ready",
                quality=self._quality(), endpoint=f"{self.host}:{self.port}",
                requests_used=self._request_count, last_success=checked_at,
                last_error=self._last_error or None,
                details={"account_access": bool(accounts), "readonly": True},
            )
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
            missing = isinstance(exc, RuntimeError) and "ib_insync" in str(exc)
            return ProviderHealth(
                provider="ibkr", configured=not missing, connected=False,
                status="missing_dependency" if missing else "error",
                endpoint=f"{self.host}:{self.port}" if self.port else "",
                message=self._last_error, last_error=self._last_error,
                details={"readonly": True},
            )

    def _ensure(self) -> Any:
        self.connect()
        backend = self._backend
        if backend is not None and self._market_data_type != 1:
            try:
                backend.reqMarketDataType(self._market_data_type)
            except Exception:
                pass
        return backend

    def _quality(self) -> str:
        # API values: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen.
        return {1: "realtime", 2: "frozen", 3: "delayed", 4: "delayed"}[self._market_data_type]

    def set_market_data_type(self, value: int) -> str:
        if int(value) not in (1, 2, 3, 4):
            raise ValueError("market data type must be 1, 2, 3, or 4")
        backend = self._ensure()
        backend.reqMarketDataType(int(value))
        self._market_data_type = int(value)
        return self._quality()

    def _sourced(self, value: Any, quality: str, market_timestamp: Optional[datetime] = None) -> SourcedValue:
        received = self._now()
        delay = None
        if market_timestamp is not None:
            delay = max(0.0, (received - market_timestamp.astimezone(timezone.utc)).total_seconds())
        return SourcedValue(value, "ibkr", quality if value is not None else "missing", market_timestamp, received, delay)

    def sync_accounts(self) -> List[AccountSnapshot]:
        backend = self._ensure()
        self._request_count += 1
        values = list(backend.accountValues())
        positions = list(backend.positions())
        portfolio_method = getattr(backend, "portfolio", None)
        portfolio = list(portfolio_method()) if callable(portfolio_method) else []
        accounts = sorted({
            str(_attr(item, "account", ""))
            for item in values + positions + portfolio if _attr(item, "account", "")
        })
        return [self._account_snapshot(account, values, positions, portfolio) for account in accounts]

    def _account_snapshot(
        self, account: str, values: Sequence[Any], positions: Sequence[Any],
        portfolio: Sequence[Any],
    ) -> AccountSnapshot:
        tags: Dict[str, Optional[float]] = {}
        currency = "USD"
        for item in values:
            if str(_attr(item, "account", "")) != account:
                continue
            tag = str(_attr(item, "tag", ""))
            if tag in {"NetLiquidation", "TotalCashValue"}:
                tags[tag] = _number(_attr(item, "value"))
                currency = str(_attr(item, "currency", currency) or currency)
        now = self._now()
        portfolio_items = [item for item in portfolio if str(_attr(item, "account", "")) == account]
        source_positions = portfolio_items or [
            item for item in positions if str(_attr(item, "account", "")) == account
        ]
        normalised = [self._position(item) for item in source_positions]
        nav = tags.get("NetLiquidation")
        cash = tags.get("TotalCashValue")
        quality = "realtime" if nav is not None or normalised else "missing"
        # Account IDs never leave this adapter; only a stable one-way reference does.
        position_map = {
            item.contract_key: {
                "quantity": item.quantity.value or 0.0,
                "average_cost": item.average_cost.value or 0.0,
                "market_price": item.market_price.value or 0.0,
                "market_value": item.market_value.value or 0.0,
                "asset_is_option": 1.0 if item.security_type == "OPT" else 0.0,
            }
            for item in normalised
        }
        return AccountSnapshot("ibkr", now, nav, cash, position_map, quality, _masked_account(account))

    def _position(self, item: Any) -> IBKRPosition:
        contract = _attr(item, "contract", {})
        symbol = str(_attr(contract, "symbol", "")).upper()
        sec_type = str(_attr(contract, "secType", "STK")).upper()
        qty = _number(_attr(item, "position"))
        cost = _number(_attr(item, "averageCost", _attr(item, "avgCost")))
        market_price = _number(_attr(item, "marketPrice"))
        market_value = _number(_attr(item, "marketValue"))
        key = self._contract_key(contract)
        now = self._now()
        return IBKRPosition(
            symbol, sec_type, key, self._sourced(qty, "realtime", now),
            self._sourced(cost, "realtime", now), self._sourced(market_price, "realtime", now),
            self._sourced(market_value, "realtime", now), str(_attr(contract, "currency", "USD")),
        )

    def sync_positions(self) -> AccountSnapshot:
        snapshots = self.sync_accounts()
        now = self._now()
        if not snapshots:
            return AccountSnapshot("ibkr", now, None, None, {}, "missing", "aggregate")
        positions = {key: value for snapshot in snapshots for key, value in snapshot.positions.items()}
        nav_values = [snapshot.nav for snapshot in snapshots if snapshot.nav is not None]
        cash_values = [snapshot.cash for snapshot in snapshots if snapshot.cash is not None]
        return AccountSnapshot(
            "ibkr", now, sum(nav_values) if nav_values else None,
            sum(cash_values) if cash_values else None, positions,
            "realtime" if positions or nav_values else "missing", "aggregate",
        )

    def get_nav(self) -> Optional[float]:
        return self.sync_positions().nav

    def sync_watchlists(self) -> List[str]:
        """TWS API has no portable user-watchlist read endpoint."""
        return []

    @staticmethod
    def _plain_symbol(symbol: str) -> str:
        """Strip the internal market prefix (US.QQQ -> QQQ) for TWS/IB Gateway.

        TWS expects bare tickers; BRK.B must stay intact (no known market
        prefix), while US.BRK.B is normalized to BRK.B.
        """
        text = str(symbol or "").strip().upper()
        if "." in text:
            head, _, tail = text.partition(".")
            if head in {"US", "HK", "CN", "SG", "JP", "AU", "CA"} and tail:
                return tail
        return text

    def _make_stock(self, symbol: str) -> Any:
        backend = self._ensure()
        maker = getattr(backend, "makeStock", None)
        plain = self._plain_symbol(symbol)
        if callable(maker):
            return maker(plain, "SMART", "USD")
        try:
            from ib_insync import Stock  # type: ignore
            return Stock(plain, "SMART", "USD")
        except ImportError:
            return {"symbol": plain, "secType": "STK", "exchange": "SMART", "currency": "USD"}

    def _make_option(self, item: IBKROptionContract) -> Any:
        backend = self._ensure()
        maker = getattr(backend, "makeOption", None)
        expiry = item.expiry.strftime("%Y%m%d")
        if callable(maker):
            return maker(item.symbol, expiry, item.strike, item.option_type, item.exchange, item.currency, item.multiplier)
        try:
            from ib_insync import Option  # type: ignore
            return Option(item.symbol, expiry, item.strike, item.option_type, item.exchange,
                          multiplier=str(item.multiplier), currency=item.currency,
                          tradingClass=item.trading_class or "")
        except ImportError:
            return {"symbol": item.symbol, "lastTradeDateOrContractMonth": expiry,
                    "strike": item.strike, "right": item.option_type, "secType": "OPT",
                    "exchange": item.exchange, "currency": item.currency,
                    "multiplier": str(item.multiplier), "tradingClass": item.trading_class or ""}

    def search_contracts(
        self, symbol: str, expiry_from: Optional[date] = None,
        expiry_to: Optional[date] = None, option_type: Optional[str] = None,
    ) -> List[ProviderOptionContract]:
        """Resolve an underlying into the provider-neutral option catalogue."""
        return list(self.get_option_chain(symbol, expiry_from, expiry_to, option_type))

    def get_option_chain(
        self, symbol: str, expiry_from: Optional[date] = None,
        expiry_to: Optional[date] = None, option_type: Optional[str] = None,
    ) -> List[IBKROptionContract]:
        backend = self._ensure()
        plain_symbol = self._plain_symbol(symbol)
        stock = self._make_stock(plain_symbol)
        qualified = list(backend.qualifyContracts(stock)) if hasattr(backend, "qualifyContracts") else [stock]
        underlying = qualified[0] if qualified else stock
        con_id = int(_attr(underlying, "conId", 0) or 0)
        self._request_count += 1
        chains = list(backend.reqSecDefOptParams(plain_symbol, "", "STK", con_id))
        # SMART routing often exposes a single near expiration while the real
        # listings (AMEX/NASDAQOM/...) carry the full chain.  Pick the richest
        # chain instead of blindly preferring SMART.
        def _chain_score(chain: Any) -> int:
            expirations = len(_attr(chain, "expirations", []) or [])
            strikes = len(_attr(chain, "strikes", []) or [])
            return expirations * 1000 + strikes

        selected = max(chains, key=_chain_score) if chains else None
        if selected is None:
            return []
        right_filter = str(option_type or "").upper()[:1]
        rights = [right_filter] if right_filter in {"C", "P"} else ["C", "P"]
        result: List[IBKROptionContract] = []
        for raw_expiry in sorted(_attr(selected, "expirations", []) or []):
            expiry = _expiry(raw_expiry)
            if expiry is None or (expiry_from and expiry < expiry_from) or (expiry_to and expiry > expiry_to):
                continue
            for strike in sorted(float(v) for v in (_attr(selected, "strikes", []) or [])):
                for right in rights:
                    key = f"US.{plain_symbol}|{expiry.isoformat()}|{strike:g}|{right}"
                    item = IBKROptionContract(
                        code=key, contract_key=key, symbol=plain_symbol, expiry=expiry,
                        strike=strike, option_type=right, provider="ibkr",
                        exchange=str(_attr(selected, "exchange", "SMART") or "SMART"),
                        multiplier=int(_number(_attr(selected, "multiplier", 100)) or 100),
                        trading_class=str(_attr(selected, "tradingClass", "") or "") or None,
                    )
                    self._contract_cache[key] = item
                    result.append(item)
        return result

    def _contract_key(self, contract: Any) -> str:
        symbol = str(_attr(contract, "symbol", "")).upper()
        if str(_attr(contract, "secType", "")).upper() != "OPT":
            return f"US.{symbol}"
        expiry = _expiry(_attr(contract, "lastTradeDateOrContractMonth", ""))
        strike = _number(_attr(contract, "strike")) or 0.0
        right = str(_attr(contract, "right", "")).upper()[:1]
        return f"US.{symbol}|{expiry.isoformat() if expiry else 'unknown'}|{strike:g}|{right}"

    def _native_contract(self, contract: Any) -> Any:
        if isinstance(contract, str):
            cached = self._contract_cache.get(contract)
            if cached is not None:
                return self._make_option(cached)
            parts = contract.split("|")
            if len(parts) == 4:
                symbol = parts[0].split(".", 1)[-1]
                expiry = date.fromisoformat(parts[1])
                return self._make_option(IBKROptionContract(
                    code=contract, contract_key=contract, symbol=symbol,
                    expiry=expiry, strike=float(parts[2]), option_type=parts[3].upper(),
                    provider="ibkr",
                ))
            return self._make_stock(contract.split(".", 1)[-1])
        return self._make_option(contract) if isinstance(contract, IBKROptionContract) else contract

    def get_snapshots(self, contracts: Iterable[Any]) -> Dict[str, IBKRMarketSnapshot]:
        backend = self._ensure()
        native = [self._native_contract(item) for item in contracts]
        if hasattr(backend, "qualifyContracts") and native:
            qualified = list(backend.qualifyContracts(*native))
            if qualified:
                native = qualified
        self._request_count += len(native)
        tickers = list(backend.reqTickers(*native)) if native else []
        return {self._contract_key(_attr(ticker, "contract", native[index] if index < len(native) else {})):
                self._snapshot(ticker, native[index] if index < len(native) else {})
                for index, ticker in enumerate(tickers)}

    def _snapshot(self, ticker: Any, contract: Any) -> IBKRMarketSnapshot:
        contract = _attr(ticker, "contract", contract)
        key = self._contract_key(contract)
        market_time = _timestamp(_attr(ticker, "time"))
        ticker_type = _attr(ticker, "marketDataType", None)
        try:
            ticker_type = int(ticker_type) if ticker_type is not None else self._market_data_type
        except (TypeError, ValueError):
            ticker_type = self._market_data_type
        quality = {1: "realtime", 2: "frozen", 3: "delayed", 4: "delayed"}.get(ticker_type, self._quality())
        if _attr(ticker, "bid", None) in (None, -1) and _attr(ticker, "ask", None) in (None, -1):
            quality = "missing"
        greeks = _attr(ticker, "modelGreeks") or _attr(ticker, "bidGreeks") or {}
        value = lambda raw: self._sourced(_number(raw), quality, market_time)
        fields = {
            "bid": value(_attr(ticker, "bid")), "ask": value(_attr(ticker, "ask")),
            "last": value(_attr(ticker, "last")), "volume": value(_attr(ticker, "volume")),
            "open_interest": value(_attr(ticker, "callOpenInterest") if str(_attr(contract, "right", "C")).upper().startswith("C") else _attr(ticker, "putOpenInterest")),
            "implied_volatility": value(_attr(greeks, "impliedVol")),
            "delta": value(_attr(greeks, "delta")), "gamma": value(_attr(greeks, "gamma")),
            "vega": value(_attr(greeks, "vega")), "theta": value(_attr(greeks, "theta")),
            "underlying_price": value(_attr(greeks, "undPrice")),
        }
        return IBKRMarketSnapshot(key, "ibkr", fields,
                                  instrument_code=str(_attr(contract, "localSymbol", "") or key),
                                  observed_at=self._now(), status=quality,
                                  metadata={"market_data_type": self._market_data_type})

    def subscribe(self, contracts: Iterable[Any]) -> List[Any]:
        backend = self._ensure()
        native = [self._native_contract(item) for item in contracts]
        self._request_count += len(native)
        return [backend.reqMktData(contract, "", False, False) for contract in native]

    def unsubscribe(self, contracts: Iterable[Any]) -> None:
        backend = self._ensure()
        for contract in contracts:
            backend.cancelMktData(self._native_contract(contract))

    def get_history(
        self, contract: Any, start: date, end: date, bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
    ) -> List[ProviderHistoryBar]:
        backend = self._ensure()
        native = self._native_contract(contract)
        end_at = datetime.combine(end, datetime.max.time()).replace(tzinfo=timezone.utc)
        days = max(1, (end - start).days + 1)
        self._request_count += 1
        bars = backend.reqHistoricalData(
            native, endDateTime=end_at, durationStr=f"{days} D", barSizeSetting=bar_size,
            whatToShow=what_to_show, useRTH=False, formatDate=2, keepUpToDate=False,
        )
        key = self._contract_key(native)
        quality = self._quality()
        result: List[ProviderHistoryBar] = []
        for bar in bars:
            timestamp = _timestamp(_attr(bar, "date")) or self._now()
            result.append(ProviderHistoryBar(
                key, timestamp, _number(_attr(bar, "open")), _number(_attr(bar, "high")),
                _number(_attr(bar, "low")), _number(_attr(bar, "close")),
                _number(_attr(bar, "volume")), "ibkr", quality, bar_size,
            ))
        return result

    def get_underlying_bars(self, symbol: str, start: date, end: date, bar_size: str = "5 mins") -> List[ProviderHistoryBar]:
        return self.get_history(self._make_stock(symbol), start, end, bar_size)

    def __enter__(self) -> "IBKRProvider":
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
