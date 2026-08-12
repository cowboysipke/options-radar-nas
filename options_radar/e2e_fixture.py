"""Deterministic Discord fixture messages for end-to-end self-checks.

The fixtures mirror the real Discord card layout: one raw-flow line plus one
analyst card per family.  No external service is required to run the pipeline;
market and history providers degrade to their synthetic/missing state.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List

from .models import RawMessage


def fixture_observed_at(session_date: date) -> datetime:
    """Return a China-local timestamp that maps to ``session_date``.

    US cash sessions end before noon China time, so the local calendar day is
    one ahead: session 2026-08-05 is observed at 2026-08-06 00:31 +08:00.
    """
    return datetime(session_date.year, session_date.month, session_date.day, 0, 31) + timedelta(days=1)


def build_fixture_messages(session_date: date) -> List[RawMessage]:
    observed = fixture_observed_at(session_date)
    return [
        RawMessage(
            channel="异常期权", analyst="flow", observed_at=observed,
            content="FCX 69 P 2026-09-04 $263K AVG$2.21 30DTE Informational purposes only.",
        ),
        RawMessage(
            channel="pa分析师", analyst="pa", observed_at=observed,
            content="FCX 2026-09-04 69P | 解读 Premium $263,296 | DTE - "
                    "执行观点 不交易 neutral 置信2",
        ),
        RawMessage(
            channel="mr分析师", analyst="mr", observed_at=observed,
            content="FCX 2026-09-04 69P | MR 解读 Premium $263,296 | DTE - "
                    "decision trade confidence_score 4",
        ),
        RawMessage(
            channel="qmr分析师", analyst="qmr", observed_at=observed,
            content="FCX 2026-09-04 69P | QMR 解读 Premium $263,296 | DTE - "
                    "结论：交易 | 方向：偏空 | 信心：高 执行要点：结构偏向空头延续",
        ),
        RawMessage(
            channel="fqd分析师", analyst="fqd", observed_at=observed,
            content="FCX 2026-09-04 69P | 解读 Premium $263,296 | DTE - "
                    "执行观点 交易 bear 入场69.50 目标66.00 止损70.60 "
                    "价格结构 Flow-Price Divergence 期权结构 偏向bear "
                    "置信4 风险提示 风险4",
        ),
    ]


__all__ = ["build_fixture_messages", "fixture_observed_at"]
