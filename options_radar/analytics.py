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
