"""Provider-neutral contracts for brokers and market-data adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional


QUALITY_ORDER = {
    "missing": 0,
    "estimated": 1,
    "eod": 2,
    "indicative": 3,
    "delayed": 4,
    "frozen": 5,
    "realtime": 6,
}


@dataclass(frozen=True)
class ProviderCapability:
    provider: str
    broker: bool = False
    market: bool = False
    accounts: bool = False
    positions: bool = False
    watchlists: bool = False
    option_chain: bool = False
    snapshots: bool = False
    streaming: bool = False
    history: bool = False
    realtime: bool = False
    delayed: bool = False
    indicative: bool = False


@dataclass
class ProviderHealth:
    provider: str
    configured: bool
    connected: bool
    status: str
    message: str = ""
    quality: str = "missing"
    endpoint: str = ""
    requests_used: Optional[int] = None
    requests_limit: Optional[int] = None
    last_success: Optional[datetime] = None
    last_error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        if self.last_success:
            result["last_success"] = self.last_success.isoformat()
        return result


@dataclass(frozen=True)
class SourcedValue:
    value: Optional[float]
    provider: str
    quality: str
    market_timestamp: Optional[datetime]
    received_at: datetime
    delay_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if self.quality not in QUALITY_ORDER:
            raise ValueError(f"unsupported data quality: {self.quality}")

    @property
    def usable(self) -> bool:
        return self.value is not None and self.quality != "missing"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "provider": self.provider,
            "quality": self.quality,
            "market_timestamp": self.market_timestamp.isoformat() if self.market_timestamp else None,
            "received_at": self.received_at.isoformat(),
            "delay_seconds": self.delay_seconds,
        }


@dataclass(frozen=True)
class ProviderOptionContract:
    code: str
    contract_key: str
    symbol: str
    expiry: date
    strike: float
    option_type: str
    provider: str


@dataclass
class ProviderMarketSnapshot:
    contract_key: str
    provider: str
    fields: Dict[str, SourcedValue]
    instrument_code: Optional[str] = None
    observed_at: datetime = field(default_factory=datetime.utcnow)
    status: str = "missing"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderHistoryBar:
    instrument_key: str
    timestamp: datetime
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[float]
    provider: str
    quality: str
    interval: str


@dataclass
class AccountSnapshot:
    provider: str
    observed_at: datetime
    nav: Optional[float]
    cash: Optional[float]
    positions: Dict[str, Dict[str, float]]
    quality: str
    account_alias: str = ""


@dataclass(frozen=True)
class DataConflict:
    field: str
    providers: List[str]
    values: List[float]
    difference_pct: float
    threshold_pct: float
    message: str


@dataclass
class CompositeMarketSnapshot:
    contract_key: str
    fields: Dict[str, SourcedValue]
    observed_at: datetime
    quote_provider: Optional[str]
    data_status: str
    conflicts: List[DataConflict] = field(default_factory=list)
    candidates: Dict[str, ProviderMarketSnapshot] = field(default_factory=dict)

    @property
    def execution_allowed(self) -> bool:
        bid, ask = self.fields.get("bid"), self.fields.get("ask")
        return bool(
            bid and ask and bid.usable and ask.usable
            and bid.provider == ask.provider
            and bid.quality == ask.quality == "realtime"
            and self.data_status == "ok"
            and not any(item.field == "midpoint" for item in self.conflicts)
        )

    def provenance(self) -> Dict[str, Any]:
        return {
            "contract_key": self.contract_key,
            "data_status": self.data_status,
            "quote_provider": self.quote_provider,
            "execution_allowed": self.execution_allowed,
            "fields": {name: value.to_dict() for name, value in self.fields.items()},
            "conflicts": [asdict(item) for item in self.conflicts],
        }


class BrokerProvider(ABC):
    @abstractmethod
    def health(self) -> ProviderHealth: ...

    @abstractmethod
    def capabilities(self) -> ProviderCapability: ...

    @abstractmethod
    def sync_accounts(self) -> List[AccountSnapshot]: ...

    @abstractmethod
    def sync_positions(self) -> AccountSnapshot: ...

    def sync_watchlists(self) -> Mapping[str, Iterable[str]]:
        return {}

    def get_nav(self) -> Optional[float]:
        snapshots = self.sync_accounts()
        values = [item.nav for item in snapshots if item.nav is not None]
        return sum(values) if values else None


class MarketProvider(ABC):
    @abstractmethod
    def health(self) -> ProviderHealth: ...

    @abstractmethod
    def capabilities(self) -> ProviderCapability: ...

    def search_contracts(self, symbol: str) -> List[ProviderOptionContract]:
        return self.get_option_chain(symbol)

    @abstractmethod
    def get_option_chain(self, symbol: str, *args: Any, **kwargs: Any) -> List[ProviderOptionContract]: ...

    @abstractmethod
    def get_snapshots(self, contract_keys: Iterable[str]) -> Dict[str, ProviderMarketSnapshot]: ...

    def get_snapshot(self, contract_key: str) -> ProviderMarketSnapshot:
        return self.get_snapshots([contract_key]).get(
            contract_key,
            ProviderMarketSnapshot(contract_key, self.capabilities().provider, {}, status="missing"),
        )

    def subscribe(self, contract_keys: Iterable[str]) -> None:
        return None

    def unsubscribe(self, contract_keys: Iterable[str]) -> None:
        return None

    @abstractmethod
    def get_history(self, instrument_key: str, start: date, end: date, interval: str) -> List[ProviderHistoryBar]: ...

    def get_underlying_bars(self, symbol: str, start: date, end: date, interval: str) -> List[ProviderHistoryBar]:
        return self.get_history(symbol, start, end, interval)
