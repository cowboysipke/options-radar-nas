from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List

from .db import Database
from .scoring import calibrated_weight


def update_analyst_weights(database: Database, lookback_days: int = 60) -> Dict[str, float]:
    samples = database.closed_trade_samples(datetime.utcnow() - timedelta(days=lookback_days))
    grouped: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for item in samples:
        grouped[str(item["analyst"])].append(item)
    updated: Dict[str, float] = {}
    for analyst, items in grouped.items():
        outcomes = [1.0 if float(item["pnl_pct"]) > 0 else 0.0 for item in items]
        returns = [float(item["pnl_pct"]) for item in items]
        calibration = [
            1.0 - (float(item["confidence"]) - outcome) ** 2
            for item, outcome in zip(items, outcomes)
        ]
        metrics = {
            "hit_rate": sum(outcomes) / len(outcomes),
            "median_return": statistics.median(returns),
            "calibration": sum(calibration) / len(calibration),
        }
        weight = calibrated_weight(
            metrics["hit_rate"], metrics["median_return"], metrics["calibration"], len(items)
        )
        database.save_weight(analyst, weight, len(items), metrics)
        updated[analyst] = weight
    return updated


def shrunk_hit_rate(hit_rate: float, samples: int, prior_strength: int = 20) -> float:
    """Bayesian shrinkage of a hit-rate toward 0.5 (no information)."""
    return (samples * hit_rate + prior_strength * 0.5) / (samples + prior_strength)


def family_alpha_from_hit_rate(hit_rate: float, samples: int, prior_strength: int = 20) -> float:
    """Map a direction hit-rate to a consensus weight, centered on 1.0 for "random".

    1.0 means no information (50% hit-rate); only statistically meaningful
    deviation from 50% moves the weight. A 50% hit-rate must NOT become 0.5.
    """
    shrunk = shrunk_hit_rate(hit_rate, samples, prior_strength)
    return 1.0 + 2.0 * (shrunk - 0.5)


def compute_family_alphas(database: Database, horizon_days: int = 0, prior_strength: int = 20) -> Dict[str, float]:
    """Derive per-family consensus weights from direction hit-rates, and persist them."""
    with database.connect() as connection:
        rows = connection.execute(
            """SELECT analyst_family, COUNT(*) AS n, SUM(CASE WHEN direction_correct=1 THEN 1 ELSE 0 END) AS hits
               FROM analyst_backtest_outcomes
               WHERE horizon_days=? AND direction_correct IS NOT NULL
               GROUP BY analyst_family""",
            (horizon_days,),
        ).fetchall()
    alphas: Dict[str, float] = {}
    for row in rows:
        family = str(row["analyst_family"])
        n = int(row["n"])
        if n <= 0:
            continue
        hit_rate = float(row["hits"]) / n
        alpha = family_alpha_from_hit_rate(hit_rate, n, prior_strength)
        alphas[family] = alpha
        database.save_family_alpha(family, alpha, hit_rate, n)
    return alphas


def update_analyst_weights_from_outcomes(
    database: Database, lookback_calendar_days: int = 90
) -> Dict[str, float]:
    """Calibrate against deterministic three-day option outcomes.

    Ninety calendar days approximates sixty US trading sessions; weights keep
    the same 20-sample shrinkage and 0.5..1.5 bounds as the desktop engine.
    """
    samples = database.analyst_outcome_samples(
        datetime.utcnow() - timedelta(days=lookback_calendar_days), horizon_days=3
    )
    grouped: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for item in samples:
        grouped[str(item["analyst"])].append(item)
    updated: Dict[str, float] = {}
    for analyst, items in grouped.items():
        outcomes = [1.0 if float(item["pnl_pct"]) > 0 else 0.0 for item in items]
        returns = [float(item["pnl_pct"]) for item in items]
        calibration = [
            1.0 - (float(item["confidence"]) - outcome) ** 2
            for item, outcome in zip(items, outcomes)
        ]
        metrics = {
            "hit_rate": sum(outcomes) / len(outcomes),
            "median_return": statistics.median(returns),
            "calibration": sum(calibration) / len(calibration),
        }
        weight = calibrated_weight(
            metrics["hit_rate"], metrics["median_return"], metrics["calibration"], len(items)
        )
        database.save_weight(analyst, weight, len(items), metrics)
        updated[analyst] = weight
    return updated


def update_analyst_weights_from_backtest(
    database: Database, horizon_days: int = 0
) -> Dict[str, float]:
    """Calibrate analyst weights from the signal back-test.

    Mixed metric: hit-rate uses the underlying direction (pure signal quality),
    median return uses the sell-side option pnl (the operator's real strategy),
    and calibration measures how well confidence matched the direction outcome.
    """
    samples = database.analyst_backtest_samples(horizon_days)
    grouped: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for item in samples:
        grouped[str(item["analyst"])].append(item)
    updated: Dict[str, float] = {}
    for analyst, items in grouped.items():
        rated = [
            item for item in items
            if item.get("direction_correct") is not None and item.get("confidence") is not None
        ]
        if not rated:
            continue
        hit_rate = sum(1 for item in rated if item["direction_correct"] == 1) / len(rated)
        returns = [float(item["strategy_pnl_pct"] or 0.0) for item in rated]
        calibration = [
            1.0 - (float(item["confidence"]) - float(item["direction_correct"])) ** 2
            for item in rated
        ]
        metrics = {
            "hit_rate": hit_rate,
            "median_return": statistics.median(returns),
            "calibration": sum(calibration) / len(calibration),
        }
        weight = calibrated_weight(
            metrics["hit_rate"], metrics["median_return"], metrics["calibration"], len(rated)
        )
        database.save_weight(analyst, weight, len(rated), metrics)
        updated[analyst] = weight
    return updated

