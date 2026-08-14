"""One-shot live integration test for the Options Radar local runtime.

Probes every configured API, collects yesterday's Discord session, runs a
deterministic one-month back-test demo, and sends a Feishu summary card.

Run:
    .venv\\Scripts\\python.exe live_test.py [--config config.local.yaml] [--date YYYY-MM-DD]

Secrets stay in data-local/secrets; nothing here is committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from options_radar.backtest_service import BacktestCoordinator
from options_radar.config import load_config
from options_radar.db import Database
from options_radar.feishu import (
    FeishuWebhookSender,
    OutboxMessage,
    build_card,
    load_feishu_webhook,
)
from options_radar.history_adapters import SyntheticHistoryAdapter
from options_radar.models import AnalystVote, ConsensusEvaluation
from options_radar.service import _secret_environment
from options_radar.timeutil import us_session_date_from_china_time


def section(title: str) -> None:
    print("\n" + "=" * 72 + "\n" + title + "\n" + "=" * 72, flush=True)


def safe(callable_, default: Any = None) -> Any:
    try:
        return callable_()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"} or default


def completed_session(now: datetime) -> date:
    """The most recent US cash session that already closed (China local view)."""
    return us_session_date_from_china_time(now - timedelta(hours=17))


def seed_demo_month(database: Database, end: date, count: int = 22) -> int:
    """Deterministic recommendations for the trailing month, demo-labeled."""
    sessions: List[date] = []
    cursor = end
    while len(sessions) < count:
        if cursor.weekday() < 5:
            sessions.append(cursor)
        cursor -= timedelta(days=1)
    sessions.reverse()
    seeded = 0
    for index, session in enumerate(sessions):
        score = round(58 + (index * 7) % 33, 1)
        strike = 100.0 + index
        expiry = session + timedelta(days=30)
        key = f"US.DEMO{index:02d}|{expiry.isoformat()}|{strike:.0f}|C"
        evaluation = ConsensusEvaluation(
            contract_key=key, evaluated_at=datetime.combine(session, datetime.min.time()),
            final_direction="BULL", score=score, grade="B" if score >= 65 else "C",
            disagreement=False, consensus_strength=0.9, components={},
            votes=[AnalystVote("pa", "price_action", "TRADE", "BULL", 0.8, 1.0, ["demo"])],
            risk_flags=["演示样本"], market_status="synthetic", eligible=score >= 65,
        )
        rec_id = database.save_recommendation(evaluation, [1], session)
        database.attach_execution(rec_id, {
            "strategy": "LONG_CALL", "contract_key": key,
            "max_entry_price": round(1.5 + (index % 9) * 0.3, 2),
            "take_profit": 2.5, "stop_loss": 1.0,
        })
        seeded += 1
    return seeded


def render_summary(results: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("**运行时间** {time}".format(**results))

    alpaca = results.get("alpaca") or {}
    lines.append("**Alpaca 行情**: {status} feed={feed} {err}".format(
        status=alpaca.get("status", "?"), feed=alpaca.get("feed", "?"),
        err=("｜" + str(alpaca["last_error"])) if alpaca.get("last_error") else "",
    ))
    deepseek = results.get("deepseek") or {}
    lines.append("**DeepSeek**: {status} {detail}".format(
        status=deepseek.get("status", "?"),
        detail=("｜" + str(deepseek["detail"])) if deepseek.get("detail") else "",
    ))
    discord = results.get("discord") or {}
    lines.append("**Discord**: {collector} status={status} 频道 {resolved}/{configured}".format(
        collector=discord.get("collector", "?"), status=discord.get("status", "?"),
        resolved=discord.get("resolved", 0), configured=discord.get("configured", 0),
    ))

    analysis = results.get("analysis") or {}
    lines.append("**昨日分析**（{session}）: 原始消息 {raw} / 信号 {signals} / 事件 {events}".format(**analysis))
    for item in (analysis.get("recommendations") or [])[:5]:
        lines.append("- {contract} {score}分 {grade} {direction} eligible={eligible} 行情={market}".format(**item))
    if not (analysis.get("recommendations") or []):
        lines.append("- 无合格推荐（多为行情缺失或未命中频道）")

    backtest = results.get("backtest") or {}
    lines.append("**一月回测**（演示样本 {seeded} 条）: 结算 {saved} 笔，filled {filled}，no-fill {no_fill}".format(
        seeded=backtest.get("seeded", 0), saved=(backtest.get("settlement") or {}).get("saved", 0),
        filled=backtest.get("filled", 0), no_fill=backtest.get("no_fill", 0),
    ))
    lines.append("平均收益 {avg}% / 最大回撤 {dd}%".format(
        avg=round((backtest.get("avg_net_return") or 0) * 100, 2),
        dd=round((backtest.get("max_drawdown") or 0) * 100, 2),
    ))

    real = results.get("backtest_real") or []
    if real:
        lines.append("**真实K线回放**（Alpaca，昨日top3合约）:")
        for item in real:
            pnl = item.get("pnl_pct")
            lines.append("- {contract} bars={bars} 最新收盘={last_close} {status} pnl={pnl}%".format(
                contract=item["contract"], bars=item.get("bars", 0),
                last_close=item.get("last_close"), status=item.get("status"),
                pnl="--" if pnl is None else round(pnl * 100, 2),
            ))

    feishu = results.get("feishu") or {}
    lines.append("**飞书发送**: {status}".format(status=feishu.get("status", "?")))
    if results.get("errors"):
        lines.append("**失败项**: " + "；".join(str(item) for item in results["errors"]))
    return "\n".join(lines)


def send_feishu(text: str, title: str = "Options Radar 实测报告", template: str = "blue") -> None:
    url = load_feishu_webhook()
    if not url:
        raise RuntimeError("feishu webhook 未配置")
    card = build_card(title, text, template)
    payload = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    item = OutboxMessage(
        0, "live-test-" + datetime.now().strftime("%Y%m%d%H%M%S"), "live-test",
        "webhook", "chat_id", "interactive", payload, 0, 0.0,
    )
    FeishuWebhookSender(url)(item)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.local.yaml"))
    parser.add_argument("--date", default="", help="session date YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=8, help="max flow events to evaluate per session")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    os.environ.setdefault("DATA_DIR", str(config_path.parent / "data-local"))
    config = load_config(str(config_path))
    _secret_environment(config)
    # Windows console defaults to GBK; set UTF-8 so Chinese log output is readable.
    try:
        import subprocess, sys
        if sys.platform == "win32":
            subprocess.run(["chcp", "65001"], capture_output=True, shell=True)
    except Exception:
        pass

    results: Dict[str, Any] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "errors": []}

    # Futu password MD5 (only used later by OpenD; kept for completeness).
    plain = config_path.parent / "data-local" / "secrets" / "futu_plaintext"
    if plain.is_file():
        digest = hashlib.md5(plain.read_text(encoding="utf-8").strip().encode("utf-8")).hexdigest()
        (config_path.parent / "data-local" / "secrets" / "futu_login_password_md5").write_text(digest, encoding="utf-8")
        print("futu md5 written", flush=True)

    from options_radar.service import OptionsRadarService

    service = OptionsRadarService(str(config_path), str(config_path.parent / "data-local"))
    try:
        # ---- Probe: Alpaca ----
        section("PROBE: Alpaca")
        alpaca_health = safe(lambda: service.alpaca.health()) or {}
        results["alpaca"] = {
            "status": alpaca_health.get("status"),
            "feed": getattr(service.alpaca, "feed", "indicative"),
            "last_error": alpaca_health.get("last_error"),
            "last_success_at": alpaca_health.get("last_success_at"),
        }
        print(json.dumps(results["alpaca"], ensure_ascii=False, indent=2), flush=True)

        # ---- Probe: DeepSeek ----
        section("PROBE: DeepSeek")
        ai_health = safe(lambda: service.ai.health(check_remote=True)) or {}
        results["deepseek"] = {
            "status": ai_health.get("status"),
            "detail": ai_health.get("message") or ai_health.get("last_error") or ai_health.get("model"),
        }
        print(json.dumps(results["deepseek"], ensure_ascii=False, indent=2), flush=True)

        # ---- Probe: Discord channels + token ----
        section("PROBE: Discord")
        source = service.source
        health = source.health()
        channel_info: Dict[str, Any] = {}
        for role in list(source.channel_targets):
            snowflake = safe(lambda role=role: source._snowflake(role))
            channel_info[role] = snowflake if isinstance(snowflake, str) else None
        resolved = sum(1 for value in channel_info.values() if value)
        results["discord"] = {
            "collector": health.get("collector"), "status": health.get("status"),
            "configured": health.get("configured_channels"), "resolved": resolved,
            "channels": channel_info, "last_error": health.get("last_error"),
        }
        print(json.dumps(results["discord"], ensure_ascii=False, indent=2), flush=True)

        # ---- Yesterday analysis ----
        now = datetime.now()
        target = date.fromisoformat(args.date) if args.date else completed_session(now)
        section(f"COLLECT + ANALYZE session {target.isoformat()}")
        collected: List[Any] = []
        for role in list(source.channel_targets):
            if role in {"guide", "subscriptions"}:
                continue
            try:
                fetched = source.fetch_since(role, None, scroll_pages=2)
            except Exception as exc:
                results["errors"].append(f"discord:{role}:{type(exc).__name__}")
                continue
            for message in fetched:
                message.analyst = role
                collected.append(source.as_raw_message(message))
        results["collect"] = {"messages": len(collected)}
        print("fetched messages:", len(collected), flush=True)
        service._ingest(collected)
        try:
            evals = service._evaluate(target, limit=args.limit)
        except Exception as exc:
            results["errors"].append(f"evaluate:{type(exc).__name__}:{str(exc)[:160]}")
            evals = []
        recommendations = [
            {
                "contract": item["contract_key"], "score": item["score"], "grade": item["grade"],
                "direction": item["direction"], "eligible": item["eligible"],
                "market": item["market_status"],
            }
            for item in evals
        ]
        results["analysis"] = {
            "session": target.isoformat(),
            "raw": len(collected),
            "signals": safe(lambda: len(service.database.signals_for_session(target))) or 0,
            "events": safe(lambda: len(service.database.flow_events_for_date(target))) or 0,
            "recommendations": recommendations,
        }
        print(json.dumps(results["analysis"], ensure_ascii=False, indent=2), flush=True)

        # ---- One-month back-test demo on a throwaway database ----
        section("BACKTEST: past month replay (deterministic demo)")
        with tempfile.TemporaryDirectory() as tmp:
            demo_db = Database(Path(tmp) / "demo.db")
            seeded = seed_demo_month(demo_db, target)
            coordinator = BacktestCoordinator(demo_db, SyntheticHistoryAdapter(), service.config.section("paper"))
            start = target - timedelta(days=34)
            settlement = coordinator.replay(start, target)
            summary = coordinator.replay_summary(start, target)
            results["backtest"] = {
                "seeded": seeded,
                "settlement": settlement,
                "filled": summary["filled"],
                "no_fill": summary["no_fill"],
                "avg_net_return": round(summary["avg_net_return"], 4),
                "max_drawdown": round(summary["max_drawdown"], 4),
                "sample_outcomes": summary["outcomes"][-12:],
            }
        print(json.dumps(results["backtest"], ensure_ascii=False, indent=2), flush=True)

        # ---- Real bars replay for yesterday's top contracts (Alpaca) ----
        section("BACKTEST: real option bars replay (Alpaca)")
        real_replay: List[Dict[str, Any]] = []
        from options_radar.history_adapters import AlpacaHistoryAdapter
        alpaca_history = AlpacaHistoryAdapter(service.alpaca)
        for item in recommendations[:3]:
            contract_key = item["contract"]
            try:
                bars = alpaca_history.aggregate_bars(contract_key, target - timedelta(days=12), target, 1, "day")
            except Exception as exc:
                results["errors"].append(f"alpaca_bars:{contract_key}:{type(exc).__name__}")
                continue
            if not bars:
                continue
            from options_radar.optimizer import OptionBar, simulate_long_option

            parsed = []
            for bar in bars:
                stamp = datetime.fromtimestamp(float(bar["t"]) / 1000.0)
                if None in (bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c")):
                    continue
                parsed.append(OptionBar(
                    observed_at=stamp, open=float(bar["o"]), high=float(bar["h"]),
                    low=float(bar["l"]), close=float(bar["c"]), complete=True,
                ))
            outcome = simulate_long_option(
                parsed, quantity=1,
                take_profit_pct=0.35, stop_loss_pct=0.25,
            )
            real_replay.append({
                "contract": contract_key,
                "bars": len(parsed),
                "last_close": parsed[-1].close if parsed else None,
                "status": outcome.status,
                "pnl_pct": round(outcome.pnl_pct, 4) if outcome.pnl_pct is not None else None,
                "exit_reason": outcome.exit_reason,
            })
        results["backtest_real"] = real_replay
        print(json.dumps(real_replay, ensure_ascii=False, indent=2), flush=True)

        # ---- Send summary to Feishu ----
        section("SEND: Feishu webhook")
        try:
            send_feishu("开始执行 Options Radar 实测，结果稍后汇总。")
            send_feishu(render_summary(results))
            results["feishu"] = {"status": "sent", "mode": "webhook"}
        except Exception as exc:
            results["errors"].append(f"feishu:{type(exc).__name__}:{str(exc)[:200]}")
            results["feishu"] = {"status": "failed"}
        print(json.dumps(results["feishu"], ensure_ascii=False, indent=2), flush=True)
    finally:
        service.stop()

    print("\n" + "#" * 72)
    print("SUMMARY", json.dumps(results, ensure_ascii=False, indent=2))
    ok = not results["errors"] and results.get("analysis", {}).get("recommendations")
    print("RESULT:", "PASS" if ok else "PARTIAL/FAIL")


if __name__ == "__main__":
    main()
