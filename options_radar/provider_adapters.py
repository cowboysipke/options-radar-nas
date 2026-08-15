"""Adapters for the project's existing Futu and Massive clients."""

from __future__ import annotations

from datetime import date, datetime, timezone
import socket
from typing import Any, Dict, Iterable, List, Optional

from .futu_provider import FutuProvider
from .massive_client import MassiveClient
from .provider_types import (
    AccountSnapshot, ProviderCapability, ProviderHealth, ProviderHistoryBar,
    ProviderMarketSnapshot, ProviderOptionContract, SourcedValue,
)


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class FutuUnifiedProvider:
    name = "futu"

    def __init__(self, client: FutuProvider, max_quote_age_seconds: int = 60):
        self.client = client
        self.max_quote_age_seconds = max_quote_age_seconds

    def capabilities(self) -> ProviderCapability:
        return ProviderCapability(
            self.name, broker=True, market=True, accounts=True, positions=True,
            watchlists=True, option_chain=True, snapshots=True, streaming=True,
            history=True, realtime=True, delayed=True,
        )

    def health(self) -> ProviderHealth:
        try:
            connection = socket.create_connection((self.client.host, self.client.port), timeout=0.25)
            connection.close()
        except OSError as exc:
            return ProviderHealth(
                self.name, True, False, "offline", message=str(exc)[:160], quality="missing",
                endpoint=f"{self.client.host}:{self.client.port}",
            )
        value = self.client.health()
        connected = bool(getattr(value, "ready", False))
        return ProviderHealth(
            self.name, True, connected, "ready" if connected else "offline",
            message=str(getattr(value, "message", "")),
            quality="realtime" if connected else "missing",
            endpoint=f"{self.client.host}:{self.client.port}",
            details=value.__dict__ if hasattr(value, "__dict__") else {},
        )

    def test_connection(self) -> None:
        value = self.health()
        if not value.connected:
            raise ConnectionError(value.message or "Futu OpenD offline")

    def sync_accounts(self) -> List[AccountSnapshot]:
        return [self.sync_positions()]

    def sync_positions(self) -> AccountSnapshot:
        value = self.client.sync_positions()
        positions = {
            item.symbol: {
                "quantity": item.quantity,
                "market_value": float(item.market_value or 0.0),
                "cost_basis": float(item.cost_price or 0.0),
                "asset_is_option": 1.0 if "OPT" in str(item.security_type or "").upper() else 0.0,
            }
            for item in value.positions
        }
        return AccountSnapshot(
            self.name, _naive(value.as_of) or datetime.utcnow(), value.nav, value.cash,
            positions, "realtime" if value.quality == "native" else value.quality,
        )

    def sync_watchlists(self) -> Dict[str, Iterable[str]]:
        return self.client.sync_watchlists().groups

    def get_nav(self) -> Optional[float]:
        return self.sync_positions().nav

    def get_option_chain(self, symbol: str, start: Optional[date] = None, end: Optional[date] = None,
                         option_type: Optional[str] = None) -> List[ProviderOptionContract]:
        start = start or date.today()
        end = end or date(start.year + 1, start.month, min(start.day, 28))
        return [ProviderOptionContract(
            item.code, item.contract_key, item.symbol, item.expiry, item.strike,
            item.option_type, self.name,
        ) for item in self.client.get_option_chain(symbol, start, end, option_type)]

    def _resolve(self, contract_key: str) -> Optional[ProviderOptionContract]:
        try:
            root, expiry_text, strike_text, option_type = contract_key.split("|")
            symbol = root.split(".", 1)[-1]
            expiry = date.fromisoformat(expiry_text)
            strike = float(strike_text)
        except (ValueError, IndexError):
            return None
        for item in self.get_option_chain(symbol, expiry, expiry, option_type):
            if abs(item.strike - strike) < 0.0001:
                return item
        return None

    def get_snapshots(self, contract_keys: Iterable[str]) -> Dict[str, ProviderMarketSnapshot]:
        resolved = {key: self._resolve(key) for key in contract_keys}
        codes = [item.code for item in resolved.values() if item]
        if codes:
            self.client.subscribe_candidates(codes)
        raw = self.client.get_snapshots(codes) if codes else {}
        now = datetime.utcnow()
        output: Dict[str, ProviderMarketSnapshot] = {}
        for key, contract in resolved.items():
            item = raw.get(contract.code) if contract else None
            if item is None:
                output[key] = ProviderMarketSnapshot(key, self.name, {}, status="missing")
                continue
            market_time = _naive(item.market_timestamp)
            delay = max(0.0, (now - market_time).total_seconds()) if market_time else None
            delayed = "DELAY" in str(item.data_type or "").upper()
            quality = "delayed" if delayed else "realtime"
            fields = {}
            mapping = {
                "bid": item.bid, "ask": item.ask, "last": item.last, "volume": item.volume,
                "open_interest": item.open_interest, "implied_volatility": item.implied_volatility,
                "delta": item.delta, "gamma": item.gamma, "vega": item.vega,
                "theta": item.theta, "rho": item.rho, "underlying_price": item.underlying_price,
            }
            received = _naive(item.observed_at) or now
            for name, number in mapping.items():
                fields[name] = SourcedValue(number, self.name, quality if number is not None else "missing",
                                            market_time, received, delay)
            output[key] = ProviderMarketSnapshot(
                key, self.name, fields, instrument_code=contract.code if contract else None,
                observed_at=received, status=quality,
                metadata={"data_type": item.data_type, "field_quality": item.field_quality},
            )
        return output

    def get_snapshot(self, contract_key: str) -> ProviderMarketSnapshot:
        return self.get_snapshots([contract_key])[contract_key]

    def get_history(self, instrument_key: str, start: date, end: date, interval: str) -> List[ProviderHistoryBar]:
        code = instrument_key
        if "|" in instrument_key:
            resolved = self._resolve(instrument_key)
            if not resolved:
                return []
            code = resolved.code
        return [ProviderHistoryBar(
            instrument_key, _naive(item.timestamp) or item.timestamp, item.open, item.high,
            item.low, item.close, item.volume, self.name, "realtime", item.interval,
        ) for item in self.client.get_history(code, start, end, interval)]


class MassiveUnifiedProvider:
    name = "massive"

    def __init__(self, client: MassiveClient):
        self.client = client

    def capabilities(self) -> ProviderCapability:
        return ProviderCapability(self.name, market=True, option_chain=True, snapshots=True, history=True)

    def health(self) -> ProviderHealth:
        configured = bool(self.client.api_key)
        return ProviderHealth(
            self.name, configured, configured, "ready" if configured else "not_configured",
            quality="eod" if configured else "missing", endpoint=self.client.base_url,
            requests_used=len(self.client._calls), requests_limit=self.client.requests_per_minute,
        )

    def get_option_chain(self, symbol: str, *args: Any, **kwargs: Any) -> List[ProviderOptionContract]:
        # Existing free client is contract-key oriented; reference-chain support
        # is supplied by the dedicated backup adapter when configured.
        return []

    def get_snapshot(self, contract_key: str) -> ProviderMarketSnapshot:
        raw = self.client.exact_snapshot(contract_key)
        mapping = {
            "last": raw.last, "volume": raw.volume, "bid": raw.bid, "ask": raw.ask,
            "open_interest": raw.open_interest, "implied_volatility": raw.implied_volatility,
            "delta": raw.delta, "underlying_price": raw.underlying_price,
        }
        fields = {
            name: SourcedValue(number, self.name, "eod" if number is not None else "missing",
                               raw.observed_at, datetime.utcnow())
            for name, number in mapping.items()
        }
        return ProviderMarketSnapshot(contract_key, self.name, fields, observed_at=raw.observed_at, status="eod")

    def get_snapshots(self, contract_keys: Iterable[str]) -> Dict[str, ProviderMarketSnapshot]:
        return {key: self.get_snapshot(key) for key in contract_keys}

    def get_history(self, instrument_key: str, start: date, end: date, interval: str) -> List[ProviderHistoryBar]:
        multiplier = 1
        timespan = "day"
        normalized = interval.upper()
        if "M" in normalized:
            digits = "".join(ch for ch in normalized if ch.isdigit())
            multiplier, timespan = int(digits or 1), "minute"
        ticker = self.client.occ_ticker(instrument_key) if "|" in instrument_key else instrument_key
        output = []
        for item in self.client.aggregate_bars(ticker, start, end, multiplier, timespan):
            stamp = datetime.fromtimestamp(float(item.get("t", 0)) / 1000, tz=timezone.utc).replace(tzinfo=None)
            output.append(ProviderHistoryBar(
                instrument_key, stamp, item.get("o"), item.get("h"), item.get("l"),
                item.get("c"), item.get("v"), self.name, "eod", interval,
            ))
        return output
