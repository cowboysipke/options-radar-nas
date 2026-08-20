from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional


VALID_DIRECTIONS = {"BULL", "BEAR", "NEUTRAL", "UNKNOWN"}


@dataclass
class RawMessage:
    channel: str
    analyst: str
    observed_at: datetime
    content: str
    source_timestamp: Optional[datetime] = None
    screenshot_path: Optional[str] = None
    content_hash: str = ""
    id: Optional[int] = None


@dataclass
class SourceMessage:
    """Provider-neutral Discord message consumed by the pipeline."""

    channel_id: str
    message_id: str
    analyst: str
    created_at: datetime
    raw_text: str
    edited_at: Optional[datetime] = None
    embeds: List[Dict[str, Any]] = field(default_factory=list)
    attachments: List[str] = field(default_factory=list)
    evidence_uri: Optional[str] = None
    content_hash: str = ""


@dataclass
class SourceCursor:
    channel_id: str
    last_message_id: str
    last_timestamp: datetime


@dataclass
class FlowEvent:
    event_key: str
    contract_key: str
    symbol: str
    expiry: date
    strike: float
    option_type: str
    premium: float
    average_price: Optional[float]
    dte: Optional[int]
    observed_at: datetime
    session_date: Optional[date] = None
    raw_message_id: Optional[int] = None
    id: Optional[int] = None


@dataclass
class ParsedSignal:
    flow_event_key: str
    contract_key: str
    symbol: str
    expiry: date
    strike: float
    option_type: str
    decision: str
    direction: str
    direction_source: str
    confidence: Optional[float]
    confidence_raw: Optional[str]
    analyst_family: str
    analyst: str
    channel: str
    observed_at: datetime
    rationale: List[str] = field(default_factory=list)
    underlying_entry: Optional[float] = None
    underlying_target: Optional[float] = None
    underlying_stop: Optional[float] = None
    premium: Optional[float] = None
    average_price: Optional[float] = None
    dte: Optional[int] = None
    win_rate: Optional[float] = None
    risk_score: Optional[int] = None
    risk_notes: List[str] = field(default_factory=list)
    completeness: float = 0.0
    raw_message_id: Optional[int] = None
    id: Optional[int] = None

    def __post_init__(self) -> None:
        self.symbol = self.symbol.upper().strip()
        self.option_type = self.option_type.upper().strip()
        self.direction = self.direction.upper().strip()
        self.decision = self.decision.upper().strip()
        if self.option_type not in {"C", "P"}:
            raise ValueError("option_type must be C or P")
        if self.direction not in VALID_DIRECTIONS:
            raise ValueError("invalid direction")
        if self.decision not in {"TRADE", "NO_TRADE", "WATCH"}:
            raise ValueError("invalid decision")
        if self.confidence is not None:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))


@dataclass
class MarketSnapshot:
    contract_key: str
    observed_at: datetime
    futu_code: Optional[str] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    volume: Optional[float] = None
    open_interest: Optional[float] = None
    implied_volatility: Optional[float] = None
    delta: Optional[float] = None
    underlying_price: Optional[float] = None
    trend_alignment: Optional[float] = None
    data_status: str = "missing"
    provider: str = "unknown"
    field_quality: Dict[str, str] = field(default_factory=dict)
    recent_active_days: Optional[int] = None
    underlying_previous_high: Optional[float] = None
    underlying_previous_low: Optional[float] = None
    underlying_atr14: Optional[float] = None
    underlying_hv: Optional[float] = None
    atm_iv: Optional[float] = None
    iv_rank: Optional[float] = None
    hv_30d: Optional[float] = None
    provenance: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    data_conflicts: List[str] = field(default_factory=list)

    @property
    def midpoint(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None and self.ask >= self.bid:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def spread_pct(self) -> Optional[float]:
        mid = self.midpoint
        if mid and self.bid is not None and self.ask is not None:
            return max(0.0, self.ask - self.bid) / mid
        return None


@dataclass
class PortfolioContext:
    symbol: str
    in_watchlist: bool = False
    held_quantity: float = 0.0
    concentration: float = 0.0
    open_paper_positions: int = 0
    nav: Optional[float] = None
    snapshot_at: Optional[datetime] = None

    @property
    def eligible(self) -> bool:
        return self.in_watchlist or self.held_quantity != 0


@dataclass
class AnalystVote:
    analyst: str
    family: str
    decision: str
    direction: str
    confidence: float
    weight: float
    rationale: List[str]
    underlying_entry: Optional[float] = None
    underlying_target: Optional[float] = None
    underlying_stop: Optional[float] = None
    win_rate: Optional[float] = None
    risk_score: Optional[int] = None


@dataclass
class ConsensusEvaluation:
    contract_key: str
    evaluated_at: datetime
    final_direction: str
    score: float
    grade: str
    disagreement: bool
    consensus_strength: float
    components: Dict[str, float]
    votes: List[AnalystVote]
    risk_flags: List[str] = field(default_factory=list)
    market_status: str = "missing"
    eligible: bool = False
    id: Optional[int] = None


@dataclass
class OptionCandidate:
    evaluation: ConsensusEvaluation
    market: MarketSnapshot
    portfolio: PortfolioContext
    strategy: str
    entry_debit: Optional[float]
    quantity: int
    max_loss: Optional[float]
    take_profit: Optional[float]
    stop_loss: Optional[float]
    invalidation: str
    underlying_entry: Optional[float] = None
    underlying_target: Optional[float] = None
    underlying_stop: Optional[float] = None
    max_entry_price: Optional[float] = None
    valid_until: Optional[date] = None
    data_quality: str = "missing"
    risk_per_contract: Optional[float] = None
    quantity_status: str = "available"


@dataclass
class PaperTrade:
    contract_key: str
    strategy: str
    direction: str
    opened_at: datetime
    entry_price: float
    quantity: int
    max_loss: float
    take_profit_price: float
    stop_loss_price: float
    expiry: date
    status: str = "OPEN"
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    recommendation_id: Optional[int] = None
    id: Optional[int] = None


@dataclass
class WatchlistItem:
    symbol: str
    source: str = "manual"
    group_name: str = "默认"
    enabled: bool = True
    added_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class BrokerSnapshot:
    as_of: datetime
    nav: Optional[float]
    cash: Optional[float]
    positions: Dict[str, Dict[str, float]]
    source: str = "futu_opend"
    quality: str = "native"


@dataclass
class SignalOutcome:
    recommendation_id: int
    horizon_days: int
    status: str
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    pnl_pct: Optional[float] = None
    max_favorable: Optional[float] = None
    max_adverse: Optional[float] = None
    exit_reason: Optional[str] = None
    observed_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class StrategyVersion:
    version: str
    status: str
    parameters: Dict[str, float]
    metrics: Dict[str, float]
    created_at: datetime = field(default_factory=datetime.utcnow)
    promoted_at: Optional[datetime] = None


def dataclass_dict(value: Any) -> Dict[str, Any]:
    result = asdict(value)
    for key, item in list(result.items()):
        if isinstance(item, (date, datetime)):
            result[key] = item.isoformat()
    return result
