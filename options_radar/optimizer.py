from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .db import Database
from .models import StrategyVersion


@dataclass(frozen=True)
class OptionBar:
    observed_at: datetime
    open: float
    high: float
    low: float
    close: float
    complete: bool = True


@dataclass(frozen=True)
class BacktestResult:
    status: str
    entry_price: Optional[float]
    exit_price: Optional[float]
    exit_reason: str
    pnl: float
    pnl_pct: Optional[float]
    max_favorable: Optional[float]
    max_adverse: Optional[float]


def simulate_long_option(
    bars: Sequence[OptionBar],
    quantity: int = 1,
    max_entry_price: Optional[float] = None,
    take_profit_pct: float = 0.35,
    stop_loss_pct: float = 0.25,
    commission_per_contract_side: float = 0.65,
) -> BacktestResult:
    """Apply the fixed five-minute fill and conservative same-bar rules."""
    complete = [bar for bar in bars if bar.complete and bar.open > 0]
    if not complete or quantity < 1:
        return BacktestResult("no-fill", None, None, "no-complete-bar", 0.0, None, None, None)
    first = complete[0]
    slippage = max(0.03, first.open * 0.03)
    entry = first.open + slippage
    if max_entry_price is not None and entry > max_entry_price:
        return BacktestResult("no-fill", None, None, "limit-exceeded", 0.0, None, None, None)
    take_profit = entry * (1.0 + take_profit_pct)
    stop_loss = entry * (1.0 - stop_loss_pct)
    exit_price = complete[-1].close
    exit_reason = "holding-limit"
    maximum = entry
    minimum = entry
    for bar in complete:
        maximum = max(maximum, bar.high)
        minimum = min(minimum, bar.low)
        hit_stop = bar.low <= stop_loss
        hit_target = bar.high >= take_profit
        if hit_stop:  # conservative when both occur inside one bar
            exit_price = stop_loss - max(0.03, stop_loss * 0.03)
            exit_reason = "stop-loss"
            break
        if hit_target:
            exit_price = take_profit - max(0.03, take_profit * 0.03)
            exit_reason = "take-profit"
            break
    gross = (exit_price - entry) * 100.0 * quantity
    fees = 2.0 * commission_per_contract_side * quantity
    pnl = gross - fees
    basis = entry * 100.0 * quantity
    return BacktestResult(
        "filled", round(entry, 4), round(exit_price, 4), exit_reason, round(pnl, 2),
        pnl / basis if basis else None,
        maximum / entry - 1.0, minimum / entry - 1.0,
    )


def simulate_short_option(
    bars: Sequence[OptionBar],
    quantity: int = 1,
    notional_per_contract: Optional[float] = None,
    margin_pct: float = 0.25,
    take_profit_pct: Optional[float] = None,
    stop_loss_pct: float = 0.50,
    commission_per_contract_side: float = 0.65,
) -> BacktestResult:
    """Sell an option short: collect premium at the first open, buy back later.

    Conservative same-bar rules (stop checked before target). Return is quoted on
    margin: pnl / (notional * margin_pct). ``notional_per_contract`` defaults to the
    exit premium times 100 when unknown (strike x 100 preferred).
    """
    complete = [bar for bar in bars if bar.complete and bar.open > 0]
    if not complete or quantity < 1:
        return BacktestResult("no-fill", None, None, "no-complete-bar", 0.0, None, None, None)
    first = complete[0]
    slippage = max(0.03, first.open * 0.03)
    entry_credit = first.open - slippage
    exit_debit = complete[-1].close
    exit_reason = "holding-limit"
    maximum = entry_credit
    minimum = entry_credit
    for bar in complete:
        maximum = max(maximum, bar.high)
        minimum = min(minimum, bar.low)
        if stop_loss_pct:
            stop_price = entry_credit * (1.0 + stop_loss_pct)
            if bar.high >= stop_price:
                exit_debit = stop_price + max(0.03, stop_price * 0.03)
                exit_reason = "stop-loss"
                break
        if take_profit_pct:
            target_price = entry_credit * (1.0 - take_profit_pct)
            if bar.low <= target_price:
                exit_debit = target_price + max(0.03, target_price * 0.03)
                exit_reason = "take-profit"
                break
    gross = (entry_credit - exit_debit) * 100.0 * quantity
    fees = 2.0 * commission_per_contract_side * quantity
    pnl = gross - fees
    basis = (notional_per_contract if notional_per_contract else exit_debit * 100.0) * margin_pct * quantity
    return BacktestResult(
        "filled", round(entry_credit, 4), round(exit_debit, 4), exit_reason, round(pnl, 2),
        pnl / basis if basis else None,
        (entry_credit - minimum) / entry_credit if entry_credit else None,
        (maximum - entry_credit) / entry_credit if entry_credit else None,
    )


def parameter_grid(champion: Dict[str, float]) -> List[Dict[str, float]]:
    """Small bounded grid; every value remains auditable and deterministic."""
    thresholds = sorted({60.0, 65.0, 70.0, float(champion.get("threshold", 65.0))})
    dte_max = sorted({45.0, 60.0, 75.0, float(champion.get("max_dte", 60.0))})
    targets = sorted({0.30, 0.35, 0.40, float(champion.get("take_profit_pct", 0.35))})
    stops = sorted({0.20, 0.25, 0.30, float(champion.get("stop_loss_pct", 0.25))})
    holding = sorted({3.0, 5.0, 7.0, float(champion.get("holding_days", 5.0))})
    output = []
    for threshold, max_dte, target, stop, days in itertools.product(
        thresholds, dte_max, targets, stops, holding
    ):
        output.append({
            "threshold": threshold, "min_dte": float(champion.get("min_dte", 14.0)),
            "max_dte": max_dte, "min_abs_delta": float(champion.get("min_abs_delta", 0.30)),
            "max_abs_delta": float(champion.get("max_abs_delta", 0.65)),
            "take_profit_pct": target, "stop_loss_pct": stop, "holding_days": days,
        })
    return output


def select_challenger(
    champion_parameters: Dict[str, float],
    evaluator: Callable[[Dict[str, float], int, int], Dict[str, float]],
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Search on 120 trading days and rank only by 40-day validation metrics."""
    ranked: List[Tuple[float, float, Dict[str, float], Dict[str, float]]] = []
    for parameters in parameter_grid(champion_parameters):
        train = evaluator(parameters, 0, 120)
        validation = evaluator(parameters, 120, 160)
        metrics = {
            "train_avg_net_return": float(train.get("avg_net_return", 0.0)),
            "validation_avg_net_return": float(validation.get("avg_net_return", 0.0)),
            "validation_max_drawdown": float(validation.get("max_drawdown", 1.0)),
            "validation_coverage": float(validation.get("coverage", 0.0)),
            "validation_samples": float(validation.get("samples", 0.0)),
        }
        ranked.append((
            metrics["validation_avg_net_return"], -abs(metrics["validation_max_drawdown"]),
            parameters, metrics,
        ))
    if not ranked:
        return champion_parameters, {}
    _, _, parameters, metrics = max(ranked, key=lambda item: (item[0], item[1]))
    return parameters, metrics


def promotion_gate(
    champion_metrics: Dict[str, float],
    challenger_metrics: Dict[str, float],
    total_samples: int,
    family_samples: Dict[str, int],
    shadow_samples: int = 0,
) -> Tuple[bool, str]:
    if total_samples < 100:
        return False, "total_samples_below_100"
    if not family_samples or min(family_samples.values()) < 20:
        return False, "family_samples_below_20"
    if challenger_metrics.get("validation_samples", 0) < 20:
        return False, "validation_samples_too_small"
    if challenger_metrics.get("validation_coverage", 0) < 0.80:
        return False, "coverage_below_80pct"
    improvement = challenger_metrics.get("validation_avg_net_return", 0) - champion_metrics.get(
        "validation_avg_net_return", champion_metrics.get("avg_net_return", 0)
    )
    if improvement < 0.005:
        return False, "net_return_improvement_below_0_5pct"
    drawdown_delta = abs(challenger_metrics.get("validation_max_drawdown", 0)) - abs(
        champion_metrics.get("validation_max_drawdown", champion_metrics.get("max_drawdown", 0))
    )
    if drawdown_delta > 0.02:
        return False, "drawdown_worse_by_over_2pct"
    if shadow_samples < 20:
        return False, "shadow_samples_below_20"
    return True, "promote"


class StrategyManager:
    def __init__(self, database: Database):
        self.database = database

    def champion(self) -> Optional[StrategyVersion]:
        return next((item for item in self.database.strategy_versions() if item.status == "champion"), None)

    def register_challenger(
        self, version: str, parameters: Dict[str, float], metrics: Dict[str, float]
    ) -> StrategyVersion:
        item = StrategyVersion(version=version, status="shadow", parameters=parameters, metrics=metrics)
        self.database.save_strategy_version(item)
        return item

    def promote(
        self,
        challenger: StrategyVersion,
        total_samples: int,
        family_samples: Dict[str, int],
        shadow_samples: int,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        now = now or datetime.utcnow()
        champion = self.champion()
        champion_metrics = champion.metrics if champion else {
            "validation_avg_net_return": 0.0, "validation_max_drawdown": 0.0,
        }
        promoted = [item for item in self.database.strategy_versions() if item.promoted_at]
        if promoted and max(item.promoted_at for item in promoted if item.promoted_at) > now - timedelta(days=30):
            return False, "monthly_promotion_limit"
        passed, reason = promotion_gate(
            champion_metrics, challenger.metrics, total_samples, family_samples, shadow_samples
        )
        if not passed:
            return False, reason
        if champion:
            champion.status = "retired"
            self.database.save_strategy_version(champion)
        challenger.status = "champion"
        challenger.promoted_at = now
        self.database.save_strategy_version(challenger)
        retired = [item for item in self.database.strategy_versions() if item.status == "retired"]
        for item in retired[2:]:
            item.status = "archived"
            self.database.save_strategy_version(item)
        return True, "promoted"
