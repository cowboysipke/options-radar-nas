from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List

from .bot import run_bot
from .config import load_config
from .db import Database
from .migration import import_watchlist_csv, migrate_futu_watchlist
from .models import RawMessage
from .pipeline import RadarPipeline
from .reports import daily_report, write_daily_report
from .tray import run_tray
from .timeutil import us_session_date_from_china_time


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_pipeline(config_path: str) -> RadarPipeline:
    config_file = Path(config_path).resolve()
    load_env(config_file.parent / ".env")
    return RadarPipeline(load_config(str(config_file)))


def command_init(args) -> None:
    root = Path(args.directory).resolve()
    source = Path(__file__).resolve().parent.parent / "config.example.yaml"
    destination = root / "config.yaml"
    if not destination.exists():
        shutil.copyfile(str(source), str(destination))
    env_source = source.parent / ".env.example"
    env_destination = root / ".env"
    if not env_destination.exists():
        shutil.copyfile(str(env_source), str(env_destination))
    print(f"initialized: {destination}")


def command_collect(args) -> None:
    pipeline = build_pipeline(args.config)
    target = date.fromisoformat(args.date) if args.date else us_session_date_from_china_time(datetime.now())
    results = pipeline.collect_today(target)
    print(pipeline.render_results(results) or "No parsed recommendations")


def command_ingest(args) -> None:
    pipeline = build_pipeline(args.config)
    path = Path(args.file)
    messages: List[RawMessage] = []
    if path.suffix.lower() in {".json", ".jsonl"}:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            messages.append(RawMessage(
                channel=item.get("channel", args.channel),
                analyst=item.get("analyst", args.analyst),
                observed_at=datetime.fromisoformat(item.get("observed_at", datetime.now().isoformat())),
                source_timestamp=datetime.fromisoformat(item["source_timestamp"]) if item.get("source_timestamp") else None,
                content=item["content"],
            ))
    else:
        messages.append(RawMessage(
            channel=args.channel,
            analyst=args.analyst,
            observed_at=datetime.now(),
            content=path.read_text(encoding="utf-8"),
        ))
    results = pipeline.process_messages(
        messages, date.fromisoformat(args.date) if args.date else us_session_date_from_china_time(datetime.now())
    )
    print(pipeline.render_results(results) or "Messages stored; waiting for matching flow/analyst cards")


def command_report(args) -> None:
    pipeline = build_pipeline(args.config)
    target = date.fromisoformat(args.date) if args.date else us_session_date_from_china_time(datetime.now())
    text = daily_report(pipeline.database, target)
    report_path = write_daily_report(pipeline.database, target, Path(args.config).resolve().parent / "reports")
    print(text)
    print(f"\n日报已保存：{report_path}")


def command_backtest(args) -> None:
    from .service import OptionsRadarService

    config_path = str(Path(args.config).resolve())
    service = OptionsRadarService(config_path, str(Path(config_path).parent))
    try:
        result = service.replay_backtest(args.start, args.end)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        service.stop()


def command_doctor(args) -> None:
    pipeline = build_pipeline(args.config)
    checks = {}
    for module in ("yaml", "PIL", "win32gui", "pywinauto", "rapidocr_onnxruntime", "discord", "futu"):
        try:
            __import__(module)
            checks[module] = "ok"
        except Exception as exc:
            checks[module] = f"missing:{type(exc).__name__}"
    futu = pipeline.config.section("futu")
    try:
        with socket.create_connection((str(futu.get("host", "127.0.0.1")), int(futu.get("port", 11111))), timeout=1):
            checks["OpenD"] = "reachable"
    except OSError:
        checks["OpenD"] = "offline"
    checks["LLM_API_KEY"] = "set" if pipeline.llm.enabled else "empty"
    checks["DISCORD_BOT_TOKEN"] = "set" if os.getenv("DISCORD_BOT_TOKEN") else "empty"
    checks["database"] = str(pipeline.database.path)
    print(json.dumps(checks, ensure_ascii=False, indent=2))


def command_bot(args) -> None:
    run_bot(build_pipeline(args.config))


def command_tray(args) -> None:
    run_tray(build_pipeline(args.config))


def command_migrate_futu(args) -> None:
    config = load_config(args.config)
    symbols = migrate_futu_watchlist(
        Database(config.database_path), host=args.host, port=args.port
    )
    print(json.dumps({"imported": len(symbols), "symbols": symbols}, ensure_ascii=False, indent=2))


def command_import_watchlist(args) -> None:
    config = load_config(args.config)
    symbols = import_watchlist_csv(Database(config.database_path), Path(args.file))
    print(json.dumps({"imported": len(symbols), "symbols": symbols}, ensure_ascii=False, indent=2))


def command_serve(args) -> None:
    os.environ["CONFIG_PATH"] = str(Path(args.config).resolve())
    os.environ.setdefault("DATA_DIR", str(Path(args.config).resolve().parent))
    from .nas_runtime import NasRuntime
    runtime = NasRuntime()
    try:
        runtime.run_forever()
    finally:
        runtime.stop()


def parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent.parent
    result = argparse.ArgumentParser(prog="options-radar")
    result.add_argument("--config", default=str(root / "config.yaml"))
    commands = result.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create config.yaml and .env")
    init.add_argument("--directory", default=str(root))
    init.set_defaults(func=command_init)

    collect = commands.add_parser("collect", help="foreground Discord collection")
    collect.add_argument("--date")
    collect.set_defaults(func=command_collect)

    ingest = commands.add_parser("ingest", help="ingest a text or JSONL fixture")
    ingest.add_argument("file")
    ingest.add_argument("--channel", default="pa分析师")
    ingest.add_argument("--analyst", default="pa")
    ingest.add_argument("--date")
    ingest.set_defaults(func=command_ingest)

    report = commands.add_parser("report", help="print daily report")
    report.add_argument("--date")
    report.set_defaults(func=command_report)

    backtest = commands.add_parser("backtest", help="settle offline back-test outcomes for a date range")
    backtest.add_argument("--start", help="YYYY-MM-DD")
    backtest.add_argument("--end", help="YYYY-MM-DD")
    backtest.set_defaults(func=command_backtest)

    doctor = commands.add_parser("doctor", help="check runtime dependencies")
    doctor.set_defaults(func=command_doctor)

    bot = commands.add_parser("bot", help="start Discord slash-command bot")
    bot.set_defaults(func=command_bot)
    tray = commands.add_parser("tray", help="start Windows tray collector")
    tray.set_defaults(func=command_tray)
    migrate = commands.add_parser("migrate-futu", help="one-time read-only Futu watchlist migration")
    migrate.add_argument("--host", default="127.0.0.1")
    migrate.add_argument("--port", type=int, default=11111)
    migrate.set_defaults(func=command_migrate_futu)
    csv_import = commands.add_parser("import-watchlist", help="import a Symbol/Code/Ticker CSV")
    csv_import.add_argument("file")
    csv_import.set_defaults(func=command_import_watchlist)
    serve = commands.add_parser("serve", help="run the NAS service and setup UI")
    serve.set_defaults(func=command_serve)
    return result


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
