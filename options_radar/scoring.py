from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from .models import (
    AnalystVote,
    ConsensusEvaluation,
    MarketSnapshot,
    ParsedSignal,
    PortfolioContext,
)


DIRECTION_VALUE = {"BULL": 1.0, "BEAR": -1.0, "NEUTRAL": 0.0, "UNKNOWN": 0.0}


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def dedupe_analyst_votes(signals: Iterable[ParsedSignal]) -> List[ParsedSignal]:
    """One analyst gets one vote per flow event; prefer the latest corrected card."""
    selected: Dict[str, ParsedSignal] = {}
    for signal in signals:
        current = selected.get(signal.analyst)
        if current is None or (signal.observed_at, signal.completeness) > (
            current.observed_at,
            current.completeness,
        ):
            selected[signal.analyst] = signal
    return list(selected.values())


def _family_trade_votes(signals: List[ParsedSignal], weights: Dict[str, float]) -> Dict[str, List[Tuple[ParsedSignal, float]]]:
    grouped: Dict[str, List[Tuple[ParsedSignal, float]]] = defaultdict(list)
    for signal in signals:
        if signal.decision != "TRADE" or signal.direction not in {"BULL", "BEAR"}:
            continue
        confidence = signal.confidence if signal.confidence is not None else 0.5
        grouped[signal.analyst_family].append((signal, weights.get(signal.analyst, 1.0) * confidence))
    return grouped


def _market_quality(market: Optional[MarketSnapshot], risk_flags: List[str]) -> float:
    if market is None or market.data_status not in {"ok", "eod"}:
        if market is not None and market.data_status == "stale":
            risk_flags.append("行情时间戳已过期（休市或数据源延迟），需等开市后刷新")
        else:
            risk_flags.append("暂未取到实时行情，等待数据源恢复")
        # Liquidity-first: a sell-side strategy holds toward expiry, so stale
        # timestamps matter less than spread/OI. If liquidity data is present,
        # award a base score instead of a flat 30.
        score = 30.0
        if market is not None:
            spread = market.spread_pct
            if spread is not None:
                score += 25.0
                if spread > 0.20:
                    score -= 20.0
                    risk_flags.append("流动性风险：价差超过20%")
                elif spread > 0.12:
                    score -= 12.0
                    risk_flags.append("流动性风险：价差超过12%")
            if market.open_interest is not None:
                score += 10.0
                if market.open_interest < 100:
                    score -= 8.0
                    risk_flags.append("流动性风险：Open Interest低于100")
        return clamp(score)
    is_eod = market.data_status == "eod"
    score = 70.0 if is_eod else 100.0
    if is_eod:
        risk_flags.append("行情为盘后或延迟数据，执行前需用富途实时买价复核")
    spread = market.spread_pct
    if spread is None:
        score -= 15
        risk_flags.append("盘口价差未知")
    elif spread > 0.20:
        score -= 50
        risk_flags.append("流动性风险：价差超过20%")
    elif spread > 0.12:
        score -= 30
        risk_flags.append("流动性风险：价差超过12%")
    elif spread > 0.08:
        score -= 12
    if market.open_interest is None:
        score -= 10
        risk_flags.append("Open Interest缺失")
    elif market.open_interest < 100:
        score -= 25
        risk_flags.append("流动性风险：Open Interest低于100")
    if market.volume is not None and market.volume < 20:
        score -= 10
    if market.implied_volatility is None:
        score -= 5
    if is_eod:
        if market.volume is not None and market.volume >= 50:
            score += 8
        if market.recent_active_days is not None and market.recent_active_days >= 3:
            score += 7
    return clamp(score)


def _premium_quality(market: Optional[MarketSnapshot], risk_flags: List[str]) -> float:
    """Premium quality for a short-premium (sell-side) strategy.

    Scores IV premium (VRP), spread, open interest, and volume. This is the
    primary alpha source for premium collection, independent of direction.
    """
    if market is None:
        risk_flags.append("暂未取到实时行情，等待数据源恢复")
        return 50.0

    score = 0.0
    # IV premium: prefer Futu's historical IV rank (0-100 percentile); fall back
    # to VRP = ATM IV - HV when rank is unavailable.
    iv_rank = market.iv_rank
    if iv_rank is not None:
        if iv_rank >= 80.0:
            score += 40.0
        elif iv_rank >= 50.0:
            score += 20.0 + (iv_rank - 50.0) * (20.0 / 30.0)
        elif iv_rank >= 20.0:
            score += 10.0 + (iv_rank - 20.0) * (10.0 / 30.0)
        else:
            score += iv_rank * 0.5
    elif market.atm_iv is not None and market.underlying_hv is not None and market.underlying_hv > 0:
        vrp = market.atm_iv / 100.0 - market.underlying_hv
        if vrp > 0.20:
            score += 40.0
        elif vrp >= 0:
            score += 20.0 + vrp * 100.0
        else:
            score += max(0.0, 20.0 + vrp * 100.0)
    else:
        score += 20.0
        risk_flags.append("IV溢价未知")

    spread = market.spread_pct
    if spread is None:
        score += 15.0
        risk_flags.append("盘口价差未知")
    elif spread <= 0.08:
        score += 30.0
    elif spread <= 0.12:
        score += 20.0
        risk_flags.append("流动性风险：价差超过8%")
    elif spread <= 0.20:
        score += 10.0
        risk_flags.append("流动性风险：价差超过12%")
    else:
        risk_flags.append("流动性风险：价差超过20%")

    if market.open_interest is None:
        score += 10.0
    elif market.open_interest >= 1000:
        score += 20.0
    elif market.open_interest >= 100:
        score += 12.0
    else:
        risk_flags.append("流动性风险：Open Interest低于100")

    if market.volume is None:
        score += 5.0
    elif market.volume >= 50:
        score += 10.0

    return clamp(score)


def _portfolio_quality(portfolio: PortfolioContext, risk_flags: List[str]) -> float:
    if not portfolio.eligible:
        return 20.0
    score = 100.0
    if portfolio.concentration >= 0.30:
        score -= 35
        risk_flags.append("持仓集中度超过30%")
    elif portfolio.concentration >= 0.20:
        score -= 20
    if portfolio.open_paper_positions >= 2:
        score -= 25
        risk_flags.append("同一标的已有2个模拟仓位")
    return clamp(score)


def evaluate_consensus(
    signals: Iterable[ParsedSignal],
    analyst_weights: Optional[Dict[str, float]] = None,
    family_alphas: Optional[Dict[str, float]] = None,
    market: Optional[MarketSnapshot] = None,
    portfolio: Optional[PortfolioContext] = None,
    now: Optional[datetime] = None,
    recommendation_threshold: float = 65.0,
    disagreement_threshold: float = 0.25,
) -> ConsensusEvaluation:
    votes_source = dedupe_analyst_votes(signals)
    if not votes_source:
        raise ValueError("at least one signal is required")
    weights = analyst_weights or {}
    portfolio = portfolio or PortfolioContext(symbol=votes_source[0].symbol)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    risk_flags: List[str] = []

    family_votes = _family_trade_votes(votes_source, weights)
    family_values: List[float] = []
    family_strengths: List[float] = []
    family_weights: List[float] = []
    for family, items in family_votes.items():
        denominator = sum(weight for _, weight in items) or 1.0
        value = sum(DIRECTION_VALUE[signal.direction] * weight for signal, weight in items) / denominator
        family_values.append(value)
        family_strengths.append(abs(value))
        family_weights.append(1.0)
        if len({signal.direction for signal, _ in items}) > 1:
            risk_flags.append(f"{family}家族内部方向冲突")

    active_families = len(family_votes)
    if family_values:
        # Family-level consensus: every active family counts equally.
        total_alpha = sum(family_weights) or 1.0
        family_direction = sum(v * w for v, w in zip(family_values, family_weights)) / total_alpha
        consensus_strength = abs(family_direction)
        final_direction = "BULL" if family_direction > 0.05 else "BEAR" if family_direction < -0.05 else "NEUTRAL"
    else:
        consensus_strength = 0.0
        final_direction = "NEUTRAL"

    disagreement = bool(
        family_values
        and (
            consensus_strength < disagreement_threshold
            or (min(family_values) < -0.05 and max(family_values) > 0.05)
        )
    )
    if disagreement:
        risk_flags.append("独立分析家族方向冲突")

    trade_count = sum(1 for signal in votes_source if signal.decision == "TRADE")
    no_trade_count = sum(1 for signal in votes_source if signal.decision == "NO_TRADE")
    decision_density = trade_count / max(1, len(votes_source))
    consensus_score = 100.0 * consensus_strength * (0.65 + 0.35 * decision_density)

    historical_values = []
    for signal in votes_source:
        # Stored analyst weights are bounded 0.5..1.5; 1.0 is neutral history = 50.
        historical_values.append((weights.get(signal.analyst, 1.0) - 0.5) * 100.0)
    historical_score = sum(historical_values) / len(historical_values)

    completeness = sum(signal.completeness for signal in votes_source) / len(votes_source)
    confidence = sum((signal.confidence or 0.5) for signal in votes_source) / len(votes_source)
    age_hours = max(0.0, (now - max(signal.observed_at for signal in votes_source)).total_seconds() / 3600.0)
    freshness = clamp(100.0 - 4.0 * age_hours)
    signal_quality = 100.0 * (0.45 * completeness + 0.35 * confidence) + 0.20 * freshness

    market_quality = _premium_quality(market, risk_flags)
    portfolio_quality = _portfolio_quality(portfolio, risk_flags)
    components = {
        "consensus": round(consensus_score, 2),
        "analyst_history": round(historical_score, 2),
        "signal_quality": round(signal_quality, 2),
        "market_quality": round(market_quality, 2),
        "portfolio_fit": round(portfolio_quality, 2),
    }
    score = (
        0.40 * market_quality      # 权利金质量（卖方 alpha 核心）
        + 0.20 * signal_quality    # 信号质量
        + 0.15 * portfolio_quality # 组合适配
        + 0.15 * consensus_score   # 方向共识（弱化）
        + 0.10 * historical_score  # 历史胜率（弱化）
    )

    dte = next((signal.dte for signal in votes_source if signal.dte is not None), None)
    if dte is not None and dte <= 7:
        score -= 15
        risk_flags.append("期权归零风险：DTE不高于7天")
    if no_trade_count > trade_count:
        score -= 8
        risk_flags.append("多数分析师选择观望")
    if active_families == 0:
        score = min(score, 49.0)
    if disagreement:
        score = min(score, 64.0)

    native_execution_fields = bool(
        market is not None
        and market.bid is not None and market.ask is not None
        and market.open_interest is not None
        and market.data_status == "ok"
    )
    if not native_execution_fields:
        if market is not None and market.data_status == "stale":
            risk_flags.append("盘口与OI为过期数据（休市），开市后自动恢复可执行行情")
        elif market is None or market.data_status != "ok":
            risk_flags.append("盘口或OI暂不完整，等待实时数据恢复")

    score = round(clamp(score), 2)
    if score >= 80 and active_families >= 1 and trade_count >= 1 and not disagreement:
        grade = "A"
    elif score >= 65:
        grade = "B"
    elif score >= 50:
        grade = "C"
    else:
        grade = "D"

    votes = [
        AnalystVote(
            analyst=signal.analyst,
            family=signal.analyst_family,
            decision=signal.decision,
            direction=signal.direction,
            confidence=signal.confidence if signal.confidence is not None else 0.5,
            weight=weights.get(signal.analyst, 1.0),
            rationale=signal.rationale,
            underlying_entry=signal.underlying_entry,
            underlying_target=signal.underlying_target,
            underlying_stop=signal.underlying_stop,
            win_rate=signal.win_rate,
            risk_score=signal.risk_score,
        )
        for signal in sorted(votes_source, key=lambda item: item.analyst)
    ]
    eligible = bool(
        portfolio.eligible
        and score >= recommendation_threshold
        and active_families >= 1
        and final_direction in {"BULL", "BEAR"}
    )
    return ConsensusEvaluation(
        contract_key=votes_source[0].contract_key,
        evaluated_at=now,
        final_direction=final_direction,
        score=score,
        grade=grade,
        disagreement=disagreement,
        consensus_strength=round(consensus_strength, 4),
        components=components,
        votes=votes,
        risk_flags=list(dict.fromkeys(risk_flags)),
        market_status=market.data_status if market else "missing",
        eligible=eligible,
    )


def evaluate_buy_side(
    signals: Iterable[ParsedSignal],
    market: Optional[MarketSnapshot] = None,
    now: Optional[datetime] = None,
) -> ConsensusEvaluation:
    """Score a short-term buy-side signal (fpd only) by its own plan quality.

    Weighting: 40% plan reward:risk, 30% liquidity, 30% analyst confidence.
    Requires an fpd TRADE signal with entry/target/stop on the underlying.
    """
    votes_source = dedupe_analyst_votes(signals)
    if not votes_source:
        raise ValueError("at least one signal is required")
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    risk_flags: List[str] = []

    fpd_trade = [
        signal for signal in votes_source
        if signal.analyst == "fpd" and signal.decision == "TRADE"
        and signal.direction in {"BULL", "BEAR"}
        and signal.underlying_entry is not None
        and signal.underlying_target is not None
        and signal.underlying_stop is not None
    ]
    if not fpd_trade:
        signal = votes_source[0]
        return ConsensusEvaluation(
            contract_key=signal.contract_key, evaluated_at=now,
            final_direction=signal.direction, score=0.0, grade="D",
            disagreement=False, consensus_strength=0.0,
            components={"plan_rr": 0.0, "liquidity": 0.0, "confidence": 0.0},
            votes=[], risk_flags=["无 fpd 交易计划，不进入买方短线流"],
            market_status=market.data_status if market else "missing", eligible=False,
        )

    signal = fpd_trade[0]
    direction = signal.direction
    entry = float(signal.underlying_entry)
    target = float(signal.underlying_target)
    stop = float(signal.underlying_stop)
    if direction == "BULL":
        reward = target - entry
        risk = entry - stop
    else:
        reward = entry - target
        risk = stop - entry
    rr = reward / risk if risk and risk > 0 else 0.0
    rr_score = clamp(rr / 2.0, 0.0, 1.0)  # R:R >= 2 is full marks

    liquidity = _market_quality(market, risk_flags) / 100.0
    confidence = float(signal.confidence if signal.confidence is not None else 0.5)

    score = round(clamp(100.0 * (0.40 * rr_score + 0.30 * liquidity + 0.30 * confidence)), 2)
    grade = "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D"

    votes = [
        AnalystVote(
            analyst=signal.analyst, family=signal.analyst_family, decision=signal.decision,
            direction=signal.direction, confidence=signal.confidence if signal.confidence is not None else 0.5,
            weight=1.0, rationale=signal.rationale,
            underlying_entry=signal.underlying_entry, underlying_target=signal.underlying_target,
            underlying_stop=signal.underlying_stop, win_rate=signal.win_rate, risk_score=signal.risk_score,
        )
    ]
    return ConsensusEvaluation(
        contract_key=votes_source[0].contract_key, evaluated_at=now,
        final_direction=direction, score=score, grade=grade,
        disagreement=False, consensus_strength=1.0,
        components={"plan_rr": round(rr_score * 100.0, 2), "liquidity": round(liquidity * 100.0, 2), "confidence": round(confidence * 100.0, 2)},
        votes=votes, risk_flags=list(dict.fromkeys(risk_flags)),
        market_status=market.data_status if market else "missing",
        eligible=False,
    )


def calibrated_weight(hit_rate: float, median_return: float, calibration_score: float, samples: int) -> float:
    """60-day analyst weight with shrinkage until 20 closed samples.

    Bounded 0.2..2.0 so a family with real alpha (e.g. fpd) can pull clearly
    above 1.0 while a coin-flip stays near 1.0 rather than collapsing to 0.5.
    """
    return_score = clamp((median_return + 0.25) / 0.60, 0.0, 1.0)
    raw_quality = 0.50 * clamp(hit_rate, 0.0, 1.0) + 0.30 * return_score + 0.20 * clamp(calibration_score, 0.0, 1.0)
    raw_weight = 0.2 + raw_quality * 1.8
    shrinkage = min(1.0, max(0, samples) / 20.0)
    return round(clamp(1.0 + shrinkage * (raw_weight - 1.0), 0.2, 2.0), 4)
