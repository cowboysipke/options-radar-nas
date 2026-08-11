from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from .db import Database
from .models import ConsensusEvaluation, OptionCandidate


def _money(value: Optional[float]) -> str:
    return "--" if value is None else f"${value:,.2f}"


def _direction(value: str) -> str:
    return {"BULL": "看多", "BEAR": "看空", "NEUTRAL": "中性"}.get(value, value)


def evaluation_markdown(
    evaluation: ConsensusEvaluation, candidate: Optional[OptionCandidate] = None
) -> str:
    action = "观察，等待更多独立分析家族确认"
    if evaluation.eligible and evaluation.final_direction == "BULL":
        action = "标的向上触发后，按限价考虑买入 Call"
    elif evaluation.eligible and evaluation.final_direction == "BEAR":
        action = "标的向下触发后，按限价考虑买入 Put"
    lines = [
        f"### {evaluation.grade}｜{evaluation.contract_key}｜{evaluation.score:.1f}/100",
        f"**一句话动作：** {action}",
        f"**最终方向：** {_direction(evaluation.final_direction)}｜**共识度：** {evaluation.consensus_strength:.0%}",
    ]
    if evaluation.disagreement:
        lines.append("**状态：** 高分歧，保留多空两方依据")
    analyst_text = "；".join(
        f"{vote.analyst.upper()}={vote.decision}/{_direction(vote.direction)}/{vote.confidence:.0%}"
        for vote in evaluation.votes
    )
    lines.append(f"**分析师：** {analyst_text}")
    reasons: List[str] = []
    for vote in evaluation.votes:
        for reason in vote.rationale:
            reason = str(reason).strip()
            if reason and reason not in reasons:
                reasons.append(reason)
    if reasons:
        lines.append("**核心理由：** " + "；".join(reasons[:3]))
    if evaluation.risk_flags:
        lines.append("**主要风险：** " + "；".join(evaluation.risk_flags[:3]))
    if candidate is not None:
        lines.extend([
            f"**数据：** {candidate.data_quality}｜{candidate.market.provider}｜{candidate.market.observed_at.isoformat(timespec='minutes')}",
            f"**标的触发：** {candidate.underlying_entry if candidate.underlying_entry is not None else '--'}"
            f"｜目标 {candidate.underlying_target if candidate.underlying_target is not None else '--'}"
            f"｜结构止损 {candidate.underlying_stop if candidate.underlying_stop is not None else '--'}",
            f"**期权执行：** 最高限价 {_money(candidate.max_entry_price)}｜策略 {candidate.strategy}",
        ])
        if candidate.quantity_status == "available":
            lines.append(
                f"**模拟仓位：** {candidate.quantity} 张｜最大风险 {_money(candidate.max_loss)}"
                f"｜每张风险 {_money(candidate.risk_per_contract)}"
            )
        else:
            lines.append(
                f"**模拟仓位：** 组合快照状态 {candidate.quantity_status}，暂不显示数量"
                f"｜每张风险 {_money(candidate.risk_per_contract)}"
            )
        lines.append(
            f"**退出：** 止盈 {_money(candidate.take_profit)}｜止损 {_money(candidate.stop_loss)}"
            f"｜最晚退出 {candidate.valid_until.isoformat() if candidate.valid_until else '--'}"
        )
        lines.append(f"**失效条件：** {candidate.invalidation}")
    return "\n".join(lines)


def _top_report_rows(database: Database, report_date: date, limit: int = 3) -> List[Dict[str, object]]:
    selected: List[Dict[str, object]] = []
    symbols = set()
    for row in database.recommendations_for_date(report_date):
        payload = json.loads(str(row["payload_json"]))
        if not bool(payload.get("eligible")) or float(payload.get("score", 0)) < 65:
            continue
        symbol = str(payload.get("contract_key", "")).split("|", 1)[0]
        if not symbol or symbol in symbols:
            continue
        item = dict(row)
        item["payload"] = payload
        selected.append(item)
        symbols.add(symbol)
        if len(selected) >= limit:
            break
    return selected


def daily_report(database: Database, report_date: date) -> str:
    all_rows = database.recommendations_for_date(report_date)
    top_rows = _top_report_rows(database, report_date)
    stats = database.paper_stats()
    lines = [
        f"# 异常期权日报｜{report_date.isoformat()}",
        f"当日处理 {len(all_rows)} 张合约｜合格推荐 {len(top_rows)} 张｜模拟已结算 {int(stats['closed'])}｜"
        f"胜率 {stats['win_rate']:.1%}｜累计 P/L ${stats['realized_pnl']:,.2f}｜最大回撤 ${stats['max_drawdown']:,.2f}",
        "",
    ]
    if not top_rows:
        lines.append("今天没有达到 65 分且满足组合约束的合约，不凑数。")
    for rank, row in enumerate(top_rows, 1):
        payload = row["payload"]
        execution = payload.get("execution", {}) if isinstance(payload.get("execution"), dict) else {}
        direction = _direction(str(payload.get("final_direction", "NEUTRAL")))
        state = "高分歧" if payload.get("disagreement") else "方向一致"
        lines.extend([
            f"## 第 {rank} 名｜{payload.get('grade')} {payload.get('contract_key')}｜{float(payload.get('score', 0)):.1f}",
            f"**动作：** {'等待向上触发后限价买入 Call' if direction == '看多' else '等待向下触发后限价买入 Put'}",
            f"**方向与共识：** {direction}｜{state}｜共识度 {float(payload.get('consensus_strength', 0)):.0%}",
            f"**标的触发：** {execution.get('underlying_entry', '--')}｜**期权最高限价：** {_money(execution.get('max_entry_price'))}",
            f"**模拟数量：** {execution.get('quantity', 0) if execution.get('quantity_status', 'available') == 'available' else '组合快照过期，暂不显示'}",
            f"**退出：** 止盈 {_money(execution.get('take_profit'))}｜止损 {_money(execution.get('stop_loss'))}｜最晚 {execution.get('valid_until', '--')}",
            f"**数据：** {execution.get('data_quality', payload.get('market_status', 'missing'))}｜{execution.get('market_observed_at', '--')}",
            f"**实时盘口：** bid {_money(execution.get('bid'))}｜ask {_money(execution.get('ask'))}｜"
            f"价差 {float(execution['spread_pct']):.1%}" if execution.get("spread_pct") is not None else "**实时盘口：** 价差数据待更新",
            f"**流动性/波动：** OI {execution.get('open_interest', '--')}｜成交量 {execution.get('volume', '--')}｜"
            f"IV {execution.get('iv', '--')}｜Delta {execution.get('delta', '--')}",
        ])
        votes = payload.get("votes", [])
        if votes:
            lines.append("**分析师：** " + "；".join(
                f"{str(vote.get('analyst', '')).upper()} {vote.get('decision', '--')}/"
                f"{_direction(str(vote.get('direction', 'UNKNOWN')))} {float(vote.get('confidence', .5)):.0%}"
                for vote in votes
            ))
            reasons = []
            for vote in votes:
                for reason in vote.get("rationale", []) or []:
                    if reason and reason not in reasons:
                        reasons.append(str(reason))
            if reasons:
                lines.append("**核心理由：** " + "；".join(reasons[:3]))
        if execution.get("invalidation"):
            lines.append(f"**失效条件：** {execution['invalidation']}")
        risks = payload.get("risk_flags", [])
        if risks:
            lines.append("**风险：** " + "；".join(str(value) for value in risks[:3]))
        lines.append("")
    analyst_rows = database.analyst_rows()
    if analyst_rows:
        lines.append("## 分析师近期权重")
        for item in analyst_rows:
            lines.append(f"- {str(item['analyst']).upper()}: {float(item['weight']):.2f}（{item['sample_count']} 个已结算样本）")
    return "\n".join(lines)


def write_daily_report(database: Database, report_date: date, reports_dir: Path) -> Path:
    """Persist as UTF-8 with BOM so common Windows editors display Chinese."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{report_date.isoformat()}.md"
    text = daily_report(database, report_date).replace("\r\n", "\n").replace("\n", "\r\n")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write(text)
    return path


def portfolio_markdown(contexts: Dict[str, object]) -> str:
    lines = ["# 富途持仓与自选匹配"]
    for symbol, context in sorted(contexts.items()):
        snapshot = context.snapshot_at.isoformat(timespec="minutes") if context.snapshot_at else "--"
        lines.append(
            f"- **{symbol}**｜自选 {'是' if context.in_watchlist else '否'}｜持仓 {context.held_quantity:g}｜"
            f"集中度 {context.concentration:.1%}｜快照 {snapshot}"
        )
    if not contexts:
        lines.append("当前没有已同步的持仓或自选。")
    return "\n".join(lines)
