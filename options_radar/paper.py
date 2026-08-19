from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from math import floor
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .db import Database
from .models import ConsensusEvaluation, MarketSnapshot, OptionCandidate, PortfolioContext


def business_days_between(start: date, end: date) -> int:
    if end <= start:
        return 0
    count = 0
    cursor = start
    while cursor < end:
        cursor = date.fromordinal(cursor.toordinal() + 1)
        if cursor.weekday() < 5:
            count += 1
    return count


def add_business_days(start: date, days: int) -> date:
    cursor = start
    remaining = max(0, days)
    while remaining:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor


def _weighted_median(items: Iterable[Tuple[float, float]]) -> Optional[float]:
    values = sorted((float(value), max(0.0, float(weight))) for value, weight in items)
    if not values:
        return None
    total = sum(weight for _, weight in values)
    if total <= 0:
        return values[len(values) // 2][0]
    cursor = 0.0
    for value, weight in values:
        cursor += weight
        if cursor >= total / 2.0:
            return value
    return values[-1][0]


def _family_plan(evaluation: ConsensusEvaluation, attribute: str) -> Optional[float]:
    by_family: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for vote in evaluation.votes:
        if vote.decision != "TRADE" or vote.direction != evaluation.final_direction:
            continue
        value = getattr(vote, attribute)
        if value is not None:
            by_family[vote.family].append((float(value), vote.weight * vote.confidence))
    family_values = [
        value for value in (_weighted_median(items) for items in by_family.values()) if value is not None
    ]
    return _weighted_median((value, 1.0) for value in family_values)


def build_candidate(
    evaluation: ConsensusEvaluation,
    market: MarketSnapshot,
    portfolio: PortfolioContext,
    settings: Dict[str, float],
) -> OptionCandidate:
    entry = market.ask if market.ask is not None else market.last
    max_entry_price = market.last * 1.05 if market.last is not None else entry
    now = evaluation.evaluated_at
    portfolio_fresh = portfolio.snapshot_at is None or business_days_between(portfolio.snapshot_at.date(), now.date()) <= 2
    if portfolio.snapshot_at is not None:
        capital = float(portfolio.nav or 0.0)
    else:
        capital = float(settings.get("starting_cash", 100000))
    risk_budget = capital * float(settings.get("risk_per_trade", 0.01))
    sizing_price = max_entry_price or entry
    quantity = floor(risk_budget / (sizing_price * 100.0)) if sizing_price and sizing_price > 0 else 0
    quantity_status = "available"
    if not portfolio_fresh:
        quantity = 0
        quantity_status = "portfolio_stale"
    elif portfolio.snapshot_at is not None and (portfolio.nav is None or portfolio.nav <= 0):
        quantity = 0
        quantity_status = "nav_missing"

    underlying_entry = _family_plan(evaluation, "underlying_entry")
    underlying_target = _family_plan(evaluation, "underlying_target")
    underlying_stop = _family_plan(evaluation, "underlying_stop")
    if underlying_entry is None and market.underlying_atr14 is not None:
        if evaluation.final_direction == "BULL" and market.underlying_previous_high is not None:
            underlying_entry = market.underlying_previous_high + 0.05 * market.underlying_atr14
        elif evaluation.final_direction == "BEAR" and market.underlying_previous_low is not None:
            underlying_entry = market.underlying_previous_low - 0.05 * market.underlying_atr14

    native_execution = bool(
        market.data_status == "ok" and market.bid is not None and market.ask is not None
    )
    if not evaluation.eligible or evaluation.score < 65 or evaluation.final_direction not in {"BULL", "BEAR"}:
        strategy = "WATCH_ONLY"
        quantity = 0
    elif not native_execution:
        strategy = "PENDING_SHORT_PUT" if evaluation.final_direction == "BULL" else "PENDING_SHORT_CALL"
    else:
        strategy = "SHORT_PUT" if evaluation.final_direction == "BULL" else "SHORT_CALL"
    if native_execution and quantity < 1 and entry and quantity_status == "available":
        strategy = "CREDIT_SPREAD_REQUIRED"
    if native_execution and market.implied_volatility is not None and market.implied_volatility >= float(settings.get("high_iv_threshold", 0.80)):
        strategy = "CREDIT_SPREAD_REQUIRED"
    take_profit_pct = float(settings.get("take_profit_pct", 0.50))
    stop_loss_pct = float(settings.get("stop_loss_pct", 0.50))
    risk_per_contract = sizing_price * 100.0 if sizing_price else None
    valid_until = add_business_days(now.date(), int(settings.get("max_holding_business_days", 5)))
    try:
        expiry = date.fromisoformat(market.contract_key.split("|")[1])
        valid_until = min(valid_until, expiry - timedelta(days=int(settings.get("exit_before_expiry_days", 3))))
    except (IndexError, ValueError):
        pass
    quality = "native" if native_execution and market.open_interest is not None else (
        "estimated" if market.data_status == "eod" and market.last is not None else "missing"
    )
    invalidation_parts = ["标的突破/跌破分析师结构失效位"]
    if underlying_stop is not None:
        invalidation_parts[0] += f" {underlying_stop:g}"
    invalidation_parts.append("超过最晚退出日或行情时间戳过期")
    return OptionCandidate(
        evaluation=evaluation,
        market=market,
        portfolio=portfolio,
        strategy=strategy,
        entry_debit=entry,
        quantity=max(0, quantity),
        max_loss=(sizing_price * 100.0 * quantity) if sizing_price and quantity else None,
        take_profit=(entry * (1.0 - take_profit_pct)) if entry else None,
        stop_loss=(entry * (1.0 + stop_loss_pct)) if entry else None,
        invalidation="；".join(invalidation_parts),
        underlying_entry=underlying_entry,
        underlying_target=underlying_target,
        underlying_stop=underlying_stop,
        max_entry_price=max_entry_price,
        valid_until=valid_until,
        data_quality=quality,
        risk_per_contract=risk_per_contract,
        quantity_status=quantity_status,
    )


def select_top_candidates(candidates: Sequence[OptionCandidate], limit: int = 3) -> List[OptionCandidate]:
    """Return up to three qualified contracts, at most one per underlying."""
    selected: List[OptionCandidate] = []
    symbols = set()
    for candidate in sorted(candidates, key=lambda item: item.evaluation.score, reverse=True):
        symbol = candidate.market.contract_key.split("|", 1)[0]
        if symbol in symbols or not candidate.evaluation.eligible or candidate.evaluation.score < 65:
            continue
        selected.append(candidate)
        symbols.add(symbol)
        if len(selected) >= max(0, limit):
            break
    return selected


class PaperEngine:
    def __init__(self, database: Database, settings: Dict[str, float]):
        self.database = database
        self.settings = settings

    def maybe_open(self, candidate: OptionCandidate, recommendation_id: int, expiry: date) -> Optional[int]:
        if not candidate.evaluation.eligible or candidate.strategy not in {"SHORT_PUT", "SHORT_CALL"}:
            return None
        if candidate.quantity < 1 or candidate.entry_debit is None:
            return None
        if len(self.database.open_trades()) >= int(self.settings.get("max_open_positions", 5)):
            return None
        for trade in self.database.open_trades():
            if trade["contract_key"] == candidate.market.contract_key:
                return None
        return self.database.open_trade({
            "recommendation_id": recommendation_id,
            "contract_key": candidate.market.contract_key,
            "strategy": candidate.strategy,
            "direction": candidate.evaluation.final_direction,
            "opened_at": datetime.utcnow().isoformat(),
            "entry_price": candidate.entry_debit,
            "quantity": candidate.quantity,
            "max_loss": candidate.max_loss or 0.0,
            "take_profit_price": candidate.take_profit or 0.0,
            "stop_loss_price": candidate.stop_loss or 0.0,
            "expiry": expiry.isoformat(),
        })

    def mark(self, prices: Dict[str, float], now: Optional[datetime] = None) -> None:
        now = now or datetime.utcnow()
        max_days = int(self.settings.get("max_holding_business_days", 5))
        exit_before = int(self.settings.get("exit_before_expiry_days", 3))
        for trade in self.database.open_trades():
            price = prices.get(str(trade["contract_key"]))
            if price is None:
                continue
            reason = None
            if price <= float(trade["take_profit_price"]):
                reason = "take_profit"
            elif price >= float(trade["stop_loss_price"]):
                reason = "stop_loss"
            elif business_days_between(date.fromisoformat(str(trade["opened_at"])[:10]), now.date()) >= max_days:
                reason = "max_holding_days"
            elif (date.fromisoformat(str(trade["expiry"])) - now.date()).days <= exit_before:
                reason = "expiry_guard"
            if reason:
                self.database.close_trade(int(trade["id"]), now, price, reason)
