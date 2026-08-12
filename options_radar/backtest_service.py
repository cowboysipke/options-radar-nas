from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .db import Database
from .massive_client import MassiveClient
from .models import SignalOutcome, StrategyVersion
from .optimizer import (
    OptionBar, StrategyManager, promotion_gate, select_challenger, simulate_long_option,
)
from .paper import add_business_days, business_days_between


def _bar(item: Dict[str, object]) -> Optional[OptionBar]:
    try:
        stamp = datetime.fromtimestamp(float(item["t"]) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
        return OptionBar(
            observed_at=stamp, open=float(item["o"]), high=float(item["h"]),
            low=float(item["l"]), close=float(item["c"]), complete=True,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _drawdown(returns: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    maximum = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        maximum = min(maximum, equity / peak - 1.0)
    return maximum


class BacktestCoordinator:
    def __init__(self, database: Database, market: MassiveClient, paper_settings: Dict[str, Any]):
        self.database = database
        self.market = market
        self.paper = dict(paper_settings)
        self.strategies = StrategyManager(database)

    def _bars_for(self, contract_key: str, start: date, end: date) -> List[OptionBar]:
        start_dt = datetime.combine(start, time.min)
        end_dt = datetime.combine(end + timedelta(days=1), time.min)
        stored = self.database.backtest_bars(contract_key, start_dt, end_dt)
        if not stored:
            raw = self.market.aggregate_bars(
                self.market.occ_ticker(contract_key), start, end,
                multiplier=5, timespan="minute",
            )
            parsed = [value for value in (_bar(item) for item in raw) if value is not None]
            self.database.save_backtest_bars(contract_key, [
                {"observed_at": item.observed_at.isoformat(), "open": item.open,
                 "high": item.high, "low": item.low, "close": item.close, "complete": item.complete}
                for item in parsed
            ])
            return parsed
        return [OptionBar(
            observed_at=datetime.fromisoformat(str(item["observed_at"])),
            open=float(item["open"]), high=float(item["high"]),
            low=float(item["low"]), close=float(item["close"]), complete=bool(item["complete"]),
        ) for item in stored]

    def record_daily(self, today: Optional[date] = None) -> Dict[str, int]:
        today = today or date.today()
        existing = {(item.recommendation_id, item.horizon_days) for item in self.database.signal_outcomes()}
        saved = 0
        no_fill = 0
        for row in self.database.recommendations_since(today - timedelta(days=30)):
            if not row.get("session_date"):
                continue
            session = date.fromisoformat(str(row["session_date"]))
            payload = json.loads(str(row["payload_json"]))
            execution = payload.get("execution", {}) if isinstance(payload.get("execution"), dict) else {}
            for horizon in (1, 3, 5):
                key = (int(row["id"]), horizon)
                if key in existing or business_days_between(session, today) < horizon:
                    continue
                start = add_business_days(session, 1)
                end = add_business_days(session, horizon)
                try:
                    bars = self._bars_for(str(row["contract_key"]), start, end)
                except Exception:
                    bars = []
                result = simulate_long_option(
                    bars, quantity=1, max_entry_price=execution.get("max_entry_price"),
                    take_profit_pct=float(self.paper.get("take_profit_pct", 0.35)),
                    stop_loss_pct=float(self.paper.get("stop_loss_pct", 0.25)),
                )
                self.database.save_signal_outcome(SignalOutcome(
                    recommendation_id=int(row["id"]), horizon_days=horizon,
                    status=result.status, entry_price=result.entry_price,
                    exit_price=result.exit_price, pnl_pct=result.pnl_pct,
                    max_favorable=result.max_favorable, max_adverse=result.max_adverse,
                ))
                saved += 1
                no_fill += int(result.status == "no-fill")
        return {"saved": saved, "no_fill": no_fill}

    def _samples(self) -> List[Dict[str, Any]]:
        outcomes = {
            item.recommendation_id: item for item in self.database.signal_outcomes()
            if item.horizon_days == 5
        }
        output: List[Dict[str, Any]] = []
        for row in self.database.recommendations_since(date.today() - timedelta(days=300)):
            outcome = outcomes.get(int(row["id"]))
            if outcome is None:
                continue
            payload = json.loads(str(row["payload_json"]))
            execution = payload.get("execution", {}) if isinstance(payload.get("execution"), dict) else {}
            session = date.fromisoformat(str(row["session_date"]))
            try:
                expiry = date.fromisoformat(str(row["contract_key"]).split("|")[1])
                dte = (expiry - session).days
            except (ValueError, IndexError):
                dte = -1
            output.append({
                "row": row, "payload": payload, "execution": execution, "outcome": outcome,
                "date": session, "dte": dte, "delta": execution.get("delta"),
            })
        return sorted(output, key=lambda item: item["date"])

    def _evaluator(self, samples: List[Dict[str, Any]]):
        dates = sorted({item["date"] for item in samples})[-160:]

        def evaluate(parameters: Dict[str, float], start: int, end: int) -> Dict[str, float]:
            selected_dates = set(dates[start:end])
            returns: List[float] = []
            total = 0
            for sample in samples:
                if sample["date"] not in selected_dates:
                    continue
                if float(sample["payload"].get("score", 0)) < parameters["threshold"]:
                    continue
                if not parameters["min_dte"] <= sample["dte"] <= parameters["max_dte"]:
                    continue
                delta = sample.get("delta")
                if delta is not None and not parameters["min_abs_delta"] <= abs(float(delta)) <= parameters["max_abs_delta"]:
                    continue
                total += 1
                start_day = add_business_days(sample["date"], 1)
                end_day = add_business_days(sample["date"], int(parameters["holding_days"]))
                bars = self.database.backtest_bars(
                    str(sample["row"]["contract_key"]),
                    datetime.combine(start_day, time.min),
                    datetime.combine(end_day + timedelta(days=1), time.min),
                )
                option_bars = [OptionBar(
                    observed_at=datetime.fromisoformat(str(item["observed_at"])),
                    open=float(item["open"]), high=float(item["high"]), low=float(item["low"]),
                    close=float(item["close"]), complete=bool(item["complete"]),
                ) for item in bars]
                result = simulate_long_option(
                    option_bars, max_entry_price=sample["execution"].get("max_entry_price"),
                    take_profit_pct=parameters["take_profit_pct"],
                    stop_loss_pct=parameters["stop_loss_pct"],
                )
                if result.pnl_pct is not None:
                    returns.append(result.pnl_pct)
            return {
                "avg_net_return": sum(returns) / len(returns) if returns else 0.0,
                "max_drawdown": _drawdown(returns),
                "coverage": len(returns) / total if total else 0.0,
                "samples": float(len(returns)),
            }
        return evaluate

    def optimize_weekly(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.utcnow()
        samples = self._samples()
        completed = [item for item in samples if item["outcome"].status == "filled"]
        family_samples = self.database.analyst_family_outcome_counts(horizon_days=5)
        if len(completed) < 100 or not family_samples or min(family_samples.values()) < 20:
            return {"status": "collecting", "samples": len(completed), "families": family_samples}
        trading_dates = {item["date"] for item in samples}
        if len(trading_dates) < 160:
            return {"status": "collecting_window", "trading_days": len(trading_dates), "required": 160}
        evaluator = self._evaluator(samples)
        champion = self.strategies.champion()
        defaults = {
            "threshold": 65.0, "min_dte": 14.0, "max_dte": 60.0,
            "min_abs_delta": 0.30, "max_abs_delta": 0.65,
            "take_profit_pct": 0.35, "stop_loss_pct": 0.25, "holding_days": 5.0,
        }
        if champion is None:
            metrics = evaluator(defaults, 120, 160)
            champion = StrategyVersion(
                version="baseline-" + now.strftime("%Y%m%d"), status="champion",
                parameters=defaults, metrics={
                    "validation_avg_net_return": metrics["avg_net_return"],
                    "validation_max_drawdown": metrics["max_drawdown"],
                    "validation_coverage": metrics["coverage"],
                    "validation_samples": metrics["samples"],
                },
            )
            self.database.save_strategy_version(champion)
            return {"status": "baseline_created", "version": champion.version}
        shadows = [item for item in self.database.strategy_versions() if item.status == "shadow"]
        if shadows:
            challenger = shadows[0]
            fresh_metrics = evaluator(challenger.parameters, 120, 160)
            challenger.metrics.update({
                "validation_avg_net_return": fresh_metrics["avg_net_return"],
                "validation_max_drawdown": fresh_metrics["max_drawdown"],
                "validation_coverage": fresh_metrics["coverage"],
                "validation_samples": fresh_metrics["samples"],
            })
            started = int(challenger.metrics.get("shadow_started_count", len(completed)))
            promoted, reason = self.strategies.promote(
                challenger, len(completed), family_samples, max(0, len(completed) - started), now,
            )
            self.database.save_strategy_version(challenger)
            return {"status": "promoted" if promoted else "shadow", "reason": reason, "version": challenger.version}
        parameters, metrics = select_challenger(champion.parameters, evaluator)
        eligible, reason = promotion_gate(champion.metrics, metrics, len(completed), family_samples, shadow_samples=20)
        if not eligible:
            return {"status": "champion_kept", "reason": reason}
        metrics["shadow_started_count"] = float(len(completed))
        challenger = self.strategies.register_challenger(
            "challenger-" + now.strftime("%Y%m%d%H%M"), parameters, metrics
        )
        return {"status": "shadow_started", "version": challenger.version}
