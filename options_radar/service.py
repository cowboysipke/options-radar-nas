"""Main NAS service for Discord signals, Futu OpenD data and Feishu output.

All scores, filters, sizing and back-test results are produced by deterministic
Python code.  DeepSeek is limited to schema-checked extraction and prose.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .ai_provider import DeepSeekProvider
from .analytics import update_analyst_weights_from_outcomes
from .backtest_service import BacktestCoordinator
from .backup_providers import AlpacaProvider, MarketDataAppProvider, TradierProvider
from .config import AppConfig, load_config
from .db import Database
from .discord_source import DiscordBrowserSource
from .feishu import FeishuBot, FeishuCallbacks, build_card
from .futu_provider import (
    FutuHistoryBar,
    FutuMarketSnapshot,
    FutuOptionContract,
    FutuProvider,
)
from .ibkr_flex import IBKRFlexClient
from .ibkr_provider import IBKRProvider
from .massive_client import MassiveClient
from .models import BrokerSnapshot, FlowEvent, MarketSnapshot, PortfolioContext, RawMessage, SourceCursor
from .paper import build_candidate
from .parser import parse_analyst_message, parse_flow_message
from .reports import daily_report, portfolio_markdown
from .rulebook import RulebookCompiler
from .scoring import evaluate_consensus
from .provider_adapters import FutuUnifiedProvider, MassiveUnifiedProvider
from .provider_registry import ProviderRegistry, market_snapshot_from_composite
from .timeutil import us_session_date_from_china_time


RULE_CHANNELS = {"guide", "subscriptions", "rules", "concepts"}


def _iso(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _as_jsonable(value: Any) -> Dict[str, Any]:
    return json.loads(json.dumps(asdict(value), ensure_ascii=False, default=_iso))


def _naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class DeepSeekTextRefiner:
    def __init__(self, provider: DeepSeekProvider):
        self.provider = provider

    def refine_signal(self, text: str, base: Dict[str, Any]) -> Dict[str, Any]:
        use_pro = bool(base.get("direction") == "UNKNOWN" and base.get("decision") == "TRADE")
        result = self.provider.extract_signal(text, use_pro=use_pro)
        if result.ai_degraded or result.data is None:
            return base
        return result.data.dict()


def _secret_environment(config: AppConfig) -> None:
    mapping = {
        "deepseek_api_key": "DEEPSEEK_API_KEY_FILE",
        "feishu_app_secret": "FEISHU_APP_SECRET_FILE",
        "ibkr_flex_token": "IBKR_FLEX_TOKEN_FILE",
        "massive_api_key": "MASSIVE_API_KEY_FILE",
        "alpaca_api_key": "ALPACA_API_KEY_FILE",
        "alpaca_api_secret": "ALPACA_API_SECRET_FILE",
        "marketdata_api_key": "MARKETDATA_API_KEY_FILE",
        "tradier_token": "TRADIER_TOKEN_FILE",
    }
    for name, environment in mapping.items():
        value = str(config.section("secret_refs").get(name, "")).strip()
        if value:
            os.environ.setdefault(environment, value)
    ai = config.section("ai")
    os.environ.setdefault("DEEPSEEK_BASE_URL", str(ai.get("base_url", "https://api.deepseek.com")))
    os.environ.setdefault("DEEPSEEK_FLASH_MODEL", str(ai.get("flash_model", "deepseek-v4-flash")))
    os.environ.setdefault("DEEPSEEK_PRO_MODEL", str(ai.get("pro_model", "deepseek-v4-pro")))
    app_id = str(config.section("notifications").get("feishu_app_id", "")).strip()
    if app_id:
        os.environ.setdefault("FEISHU_APP_ID", app_id)


class IBKRHistoryAdapter:
    """Adapter for the deterministic back-test coordinator using IBKR."""

    def __init__(self, provider: IBKRProvider):
        self.provider = provider
        self._codes: Dict[str, Any] = {}

    def remember(self, contract_key: str, code: str) -> None:
        self._codes[contract_key] = code

    @staticmethod
    def occ_ticker(contract_key: str) -> str:
        return contract_key

    def resolve(self, contract_key: str) -> Optional[Any]:
        if contract_key in self._codes:
            return self._codes[contract_key]
        try:
            root, expiry_text, strike_text, option_type = contract_key.split("|")
            symbol = root.split(".", 1)[-1]
            expiry = date.fromisoformat(expiry_text)
            strike = float(strike_text)
        except (ValueError, IndexError):
            return None
        for contract in self.provider.get_option_chain(symbol, expiry, expiry, option_type):
            if abs(contract.strike - strike) < 0.0001:
                self.remember(contract_key, contract)
                return contract
        return None

    def aggregate_bars(
        self, contract_key: str, start: date, end: date,
        multiplier: int = 5, timespan: str = "minute",
    ) -> List[Dict[str, Any]]:
        del timespan
        code = self.resolve(contract_key)
        if not code:
            return []
        interval = "K_5M" if int(multiplier) == 5 else f"K_{int(multiplier)}M"
        bars = self.provider.get_history(code, start, end, bar_size="5 mins" if int(multiplier) == 5 else f"{int(multiplier)} mins")
        output: List[Dict[str, Any]] = []
        for bar in bars:
            if None in (bar.open, bar.high, bar.low, bar.close):
                continue
            output.append({
                "t": int(bar.timestamp.timestamp() * 1000), "o": bar.open,
                "h": bar.high, "l": bar.low, "c": bar.close,
            })
        return output


# Kept as a compatibility adapter for the archived demo and its fixtures.
class FutuHistoryAdapter:
    def __init__(self, provider: FutuProvider):
        self.provider = provider
        self._codes: Dict[str, str] = {}

    def remember(self, contract_key: str, code: str) -> None:
        self._codes[contract_key] = code

    @staticmethod
    def occ_ticker(contract_key: str) -> str:
        return contract_key

    def resolve(self, contract_key: str) -> Optional[str]:
        if contract_key in self._codes:
            return self._codes[contract_key]
        try:
            root, expiry_text, strike_text, option_type = contract_key.split("|")
            symbol = root.split(".", 1)[-1]
            expiry = date.fromisoformat(expiry_text)
            strike = float(strike_text)
        except (ValueError, IndexError):
            return None
        for contract in self.provider.get_option_chain(symbol, expiry, expiry, option_type):
            if abs(contract.strike - strike) < 0.0001:
                self.remember(contract_key, contract.code)
                return contract.code
        return None

    def aggregate_bars(self, contract_key: str, start: date, end: date,
                       multiplier: int = 5, timespan: str = "minute") -> List[Dict[str, Any]]:
        del timespan
        code = self.resolve(contract_key)
        if not code:
            return []
        interval = "K_5M" if int(multiplier) == 5 else f"K_{int(multiplier)}M"
        return [{"t": int(bar.timestamp.timestamp() * 1000), "o": bar.open,
                 "h": bar.high, "l": bar.low, "c": bar.close}
                for bar in self.provider.get_history(code, start, end, interval=interval)
                if None not in (bar.open, bar.high, bar.low, bar.close)]


class OptionsRadarService:
    """Single lifecycle component used by the NAS supervisor."""

    def __init__(
        self, config_path: str, data_dir: str, *,
        futu_provider: Optional[FutuProvider] = None,
        discord_source: Optional[DiscordBrowserSource] = None,
    ):
        self.config = load_config(config_path)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _secret_environment(self.config)
        self.database = Database(self.config.database_path)
        self.ai = DeepSeekProvider(self.config.database_path)
        self.refiner = DeepSeekTextRefiner(self.ai)
        futu = self.config.section("futu")
        self.futu = futu_provider or FutuProvider(
            host=str(futu.get("host", "127.0.0.1")),
            port=int(futu.get("port", 11111)),
            security_firm=str(futu.get("security_firm", "NONE")),
        )
        self._futu_injected = futu_provider is not None
        provider_config = self.config.section("providers")
        ibkr_config = self.config.section("ibkr")
        self.ibkr = IBKRProvider(
            host=str(ibkr_config.get("host", "127.0.0.1")),
            port=int(ibkr_config.get("port", 0)) or None,
            client_id=int(ibkr_config.get("client_id", 71)),
        )
        self.ibkr_flex = IBKRFlexClient()
        self.massive = MassiveClient()
        def secret_value(direct: str, file_var: str) -> str:
            value = os.getenv(direct, "").strip()
            path = os.getenv(file_var, "").strip()
            if not value and path and Path(path).is_file():
                value = Path(path).read_text(encoding="utf-8").strip()
            return value

        self.alpaca = AlpacaProvider(
            secret_value("ALPACA_API_KEY", "ALPACA_API_KEY_FILE"),
            secret_value("ALPACA_API_SECRET", "ALPACA_API_SECRET_FILE"),
        )
        self.marketdata_app = MarketDataAppProvider(
            secret_value("MARKETDATA_API_KEY", "MARKETDATA_API_KEY_FILE")
        )
        self.tradier = TradierProvider(
            secret_value("TRADIER_TOKEN", "TRADIER_TOKEN_FILE"),
            sandbox=str(os.getenv("TRADIER_MODE", "sandbox")).lower() != "live",
        )
        execution_config = dict(provider_config.get("execution", {}))
        enabled_config = dict(provider_config.get("enabled", {}))
        default_priority = ["futu", "ibkr", "tradier", "alpaca", "massive", "marketdata_app"] if futu_provider is not None else ["ibkr", "massive", "futu", "tradier", "alpaca", "marketdata_app"]
        self.providers = ProviderRegistry(
            {
                "futu": FutuUnifiedProvider(self.futu),
                "ibkr": self.ibkr,
                "tradier": self.tradier,
                "alpaca": self.alpaca,
                "massive": MassiveUnifiedProvider(self.massive),
                "marketdata_app": self.marketdata_app,
            },
            priority=provider_config.get("market_priority", default_priority),
            enabled=enabled_config,
            max_quote_age_seconds=int(execution_config.get("max_quote_age_seconds", 60)),
            conflict_threshold_pct=float(execution_config.get("conflict_threshold_pct", 15)),
        )
        self.history_market = IBKRHistoryAdapter(self.ibkr)
        self.backtests = BacktestCoordinator(self.database, self.history_market, self.config.section("paper"))
        self.rulebook = RulebookCompiler(self.database, self.config.section("analyst_families"))
        self._last_backtest: Optional[Dict[str, Any]] = None
        self._last_optimization: Optional[Dict[str, Any]] = None

        discord = self.config.section("discord")
        channels = dict(discord.get("channel_urls", {})) or dict(discord.get("channel_names", {}))
        self.channel_roles = {str(role): str(role) for role in channels}
        if discord.get("source_channels") and not discord.get("channel_names"):
            channels = {str(role): str(name) for name, role in discord["source_channels"].items()}
        self.source = discord_source or DiscordBrowserSource(
            profile_dir=self.data_dir / "browser-profile",
            evidence_dir=self.data_dir / "evidence",
            channel_urls=channels,
            server_name=str(discord.get("source_server", "")),
            headless=bool(discord.get("headless", True)),
        )
        self._portfolio: Dict[str, PortfolioContext] = {}
        self._last_results: List[Dict[str, Any]] = []
        self._last_collection: Optional[str] = None
        self._last_sync: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_market_by_contract: Dict[str, MarketSnapshot] = {}
        self._scheduler: Any = None
        self._feishu_thread: Optional[threading.Thread] = None
        self._started = False
        self._feishu_closed = False
        self._lock = threading.RLock()
        self.feishu = FeishuBot(
            callbacks=FeishuCallbacks(
                today_recommendations=self.today_recommendations,
                positions=self.positions,
                add_watchlist=self.add_watchlist,
                remove_watchlist=self.remove_watchlist,
                explain_rank=self.explain_rank,
                free_chat=self.free_chat,
                sync_futu=lambda: _as_jsonable(self.sync_broker(force=True)),
                system_status=self.health,
            ),
            database_path=self.data_dir / "feishu_state.db",
        )

    @staticmethod
    def _trade_date() -> date:
        return us_session_date_from_china_time(datetime.now())

    def _watch_symbols(self) -> List[str]:
        return [item.symbol for item in self.database.list_watchlist()]

    def sync_broker(self, force: bool = False) -> BrokerSnapshot:
        """Synchronise IBKR read-only portfolio; Futu is imported on demand only."""
        del force
        if self._futu_injected:
            return self._sync_futu_compat()
        positions = self.ibkr.sync_positions()
        payload_positions: Dict[str, Dict[str, float]] = {}
        for contract_key, item in positions.positions.items():
            symbol = str(contract_key).split("|", 1)[0].split(".", 1)[-1].upper()
            payload_positions[symbol] = {
                "quantity": float(item.get("quantity", 0.0)),
                "market_value": float(item.get("market_value", 0.0)),
                "cost_price": float(item.get("average_cost", item.get("cost_basis", 0.0))),
                "nominal_price": float(item.get("market_price", 0.0)),
            }
        watched = {item.symbol for item in self.database.list_watchlist()}
        snapshot = BrokerSnapshot(
            as_of=_naive_utc(positions.observed_at) or datetime.utcnow(),
            nav=getattr(positions, "nav", None), cash=getattr(positions, "cash", None),
            positions=payload_positions, source="ibkr", quality=positions.quality,
        )
        self.database.save_broker_snapshot(snapshot)
        gross = max(sum(abs(float(item.get("market_value", 0.0))) for item in payload_positions.values()), 0.0)
        all_symbols = watched | set(payload_positions)
        open_counts: Dict[str, int] = {}
        for trade in self.database.open_trades():
            symbol = str(trade["contract_key"]).split("|", 1)[0].split(".", 1)[-1]
            open_counts[symbol] = open_counts.get(symbol, 0) + 1
        self._portfolio = {}
        for symbol in all_symbols:
            row = payload_positions.get(symbol, {})
            context = PortfolioContext(
                symbol=symbol, in_watchlist=symbol in watched,
                held_quantity=float(row.get("quantity", 0.0)),
                concentration=abs(float(row.get("market_value", 0.0))) / gross if gross else 0.0,
                open_paper_positions=open_counts.get(symbol, 0), nav=snapshot.nav,
                snapshot_at=snapshot.as_of,
            )
            self._portfolio[symbol] = context
            self.database.save_portfolio_snapshot(symbol, snapshot.as_of, _as_jsonable(context))
        self._last_sync = datetime.utcnow().isoformat()
        return snapshot

    def _sync_futu_compat(self) -> BrokerSnapshot:
        watchlists = self.futu.sync_watchlists()
        positions = self.futu.sync_positions()
        watched = set(watchlists.symbols)
        group_by_symbol: Dict[str, str] = {}
        for group, codes in watchlists.groups.items():
            for code in codes:
                group_by_symbol.setdefault(code.split(".", 1)[-1].upper(), str(group))
        for symbol in watched:
            self.database.upsert_watchlist(symbol, source="futu_opend", group_name=group_by_symbol.get(symbol, "富途自选"))
        payload_positions = {item.symbol: {"quantity": float(item.quantity), "market_value": float(item.market_value or 0),
                                           "cost_price": float(item.cost_price or 0), "nominal_price": float(item.nominal_price or 0)}
                            for item in positions.positions}
        snapshot = BrokerSnapshot(_naive_utc(positions.as_of) or datetime.utcnow(), getattr(positions, "nav", None),
                                  getattr(positions, "cash", None), payload_positions, "futu_opend", "native")
        self.database.save_broker_snapshot(snapshot)
        gross = max(float(getattr(positions, "gross_market_value", 0) or 0), 0.0)
        self._portfolio = {symbol: PortfolioContext(symbol=symbol, in_watchlist=symbol in watched,
            held_quantity=float(payload_positions.get(symbol, {}).get("quantity", 0)),
            concentration=abs(float(payload_positions.get(symbol, {}).get("market_value", 0))) / gross if gross else 0,
            nav=snapshot.nav, snapshot_at=snapshot.as_of) for symbol in (watched | set(payload_positions))}
        self._last_sync = datetime.utcnow().isoformat()
        return snapshot

    def _ingest(self, messages: List[RawMessage]) -> None:
        rule_messages: List[RawMessage] = []
        for message in messages:
            raw_id, created = self.database.insert_raw_message(message)
            message.id = raw_id
            if message.analyst in RULE_CHANNELS:
                if created:
                    rule_messages.append(message)
                continue
            if not created:
                continue
            if message.analyst == "flow":
                event = parse_flow_message(message)
                if event:
                    event.raw_message_id = raw_id
                    self.database.insert_flow_event(event)
                continue
            signal = parse_analyst_message(message, refiner=self.refiner if self.ai.enabled else None)
            if signal:
                signal.raw_message_id = raw_id
                self.database.insert_signal(signal)
        if rule_messages:
            self.rulebook.compile(rule_messages)

    def _collect_messages(self, backfill: bool = False) -> List[RawMessage]:
        output: List[RawMessage] = []
        for channel_id in self.source.channel_urls:
            cursor = self.database.get_source_cursor(channel_id)
            role = self.channel_roles.get(channel_id, channel_id)
            fetch_cursor = cursor
            if backfill:
                fetch_cursor = SourceCursor(
                    channel_id=channel_id, last_message_id="0",
                    last_timestamp=datetime.utcnow() - timedelta(hours=24),
                )
            pages = 20 if backfill else (50 if cursor is None and role in RULE_CHANNELS else 0)
            messages = self.source.fetch_since(channel_id, fetch_cursor, scroll_pages=pages)
            for message in messages:
                message.analyst = role
                output.append(self.source.as_raw_message(message))
            if messages and not backfill:
                last = max(messages, key=lambda item: (item.created_at, item.message_id))
                self.database.save_source_cursor(SourceCursor(
                    channel_id=channel_id, last_message_id=last.message_id,
                    last_timestamp=last.edited_at or last.created_at,
                ))
        return output

    def _events_for_date(self, target_date: date) -> List[FlowEvent]:
        events = self.database.flow_events_for_date(target_date)
        existing = {item.event_key for item in events}
        for signal in self.database.signals_for_session(target_date):
            if signal.flow_event_key in existing:
                continue
            event = FlowEvent(
                event_key=signal.flow_event_key, contract_key=signal.contract_key,
                symbol=signal.symbol, expiry=signal.expiry, strike=signal.strike,
                option_type=signal.option_type, premium=signal.premium or 0.0,
                average_price=signal.average_price, dte=signal.dte,
                observed_at=signal.observed_at, session_date=target_date,
            )
            event.id = self.database.insert_flow_event(event)[0]
            events.append(event)
            existing.add(event.event_key)
        return events

    def _resolve_contract(self, event: FlowEvent) -> Optional[Any]:
        contracts = self.ibkr.get_option_chain(event.symbol, event.expiry, event.expiry, event.option_type)
        for contract in contracts:
            if abs(contract.strike - event.strike) < 0.0001:
                self.history_market.remember(event.contract_key, contract.code)
                return contract
        return None

    def _underlying_features(self, symbol: str, target_date: date) -> Dict[str, Optional[float]]:
        try:
            bars = self.ibkr.get_underlying_bars(symbol, target_date - timedelta(days=45), target_date, "1 day")
        except Exception:
            bars = []
        complete = [bar for bar in bars if None not in (bar.high, bar.low, bar.close)]
        if not complete:
            return {"high": None, "low": None, "atr": None, "trend": None}
        previous = complete[-1]
        ranges: List[float] = []
        prior_close: Optional[float] = None
        for bar in complete[-15:]:
            values = [float(bar.high) - float(bar.low)]
            if prior_close is not None:
                values += [abs(float(bar.high) - prior_close), abs(float(bar.low) - prior_close)]
            ranges.append(max(values))
            prior_close = float(bar.close)
        closes = [float(bar.close) for bar in complete[-20:]]
        trend = 0.0
        if closes:
            mean = sum(closes) / len(closes)
            trend = 1.0 if closes[-1] > mean else -1.0 if closes[-1] < mean else 0.0
        return {
            "high": float(previous.high), "low": float(previous.low),
            "atr": sum(ranges[-14:]) / min(14, len(ranges)), "trend": trend,
        }

    def _market_for_event(self, event: FlowEvent, target_date: date) -> MarketSnapshot:
        composite = self.providers.composite_snapshot(event.contract_key)
        snapshot = market_snapshot_from_composite(composite)
        features = self._underlying_features(event.symbol, target_date)
        snapshot.trend_alignment = features["trend"]
        snapshot.underlying_previous_high = features["high"]
        snapshot.underlying_previous_low = features["low"]
        snapshot.underlying_atr14 = features["atr"]
        ibkr_candidate = composite.candidates.get("ibkr")
        if ibkr_candidate:
            snapshot.futu_code = ibkr_candidate.instrument_code
        self.database.save_market_data_points(
            event.contract_key,
            {name: {
                "value": point.value, "source": point.provider, "quality": point.quality,
                "market_timestamp": point.market_timestamp.isoformat() if point.market_timestamp else None,
            } for name, point in composite.fields.items()},
        )
        return snapshot

    def _evaluate(self, target_date: date) -> List[Dict[str, Any]]:
        try:
            self.sync_broker(force=True)
        except Exception as exc:
            self._last_error = f"futu_sync:{type(exc).__name__}:{str(exc)[:120]}"
        scoring = self.config.section("scoring")
        paper = self.config.section("paper")
        output: List[Dict[str, Any]] = []
        active_codes: List[str] = []
        for event in sorted(self._events_for_date(target_date), key=lambda item: item.observed_at, reverse=True):
            self.database.enrich_signals_for_event(event)
            signals = self.database.signals_for_event(event.event_key)
            if not signals:
                continue
            try:
                market = self._market_for_event(event, target_date)
            except Exception as exc:
                market = MarketSnapshot(event.contract_key, datetime.utcnow(), provider="futu_opend", data_status="missing")
                market.field_quality["error"] = f"{type(exc).__name__}:{str(exc)[:100]}"
            active_codes.append(event.contract_key)
            self._last_market_by_contract[event.contract_key] = market
            self.database.save_option_snapshot(event.contract_key, market.observed_at, _as_jsonable(market))
            portfolio = self._portfolio.get(event.symbol, PortfolioContext(
                symbol=event.symbol, in_watchlist=event.symbol in set(self._watch_symbols()),
            ))
            evaluation = evaluate_consensus(
                signals, self.database.get_weights(item.analyst for item in signals),
                market=market, portfolio=portfolio,
                recommendation_threshold=float(scoring.get("recommendation_threshold", 65)),
                disagreement_threshold=float(scoring.get("disagreement_threshold", 0.25)),
            )
            dte = event.dte if event.dte is not None else (event.expiry - target_date).days
            hard_filter = None
            if not int(scoring.get("min_dte", 14)) <= dte <= int(scoring.get("max_dte", 60)):
                hard_filter = f"DTE {dte} 不在默认范围"
            elif market.delta is None or not float(scoring.get("min_abs_delta", 0.30)) <= abs(market.delta) <= float(scoring.get("max_abs_delta", 0.65)):
                hard_filter = "Delta 缺失或不在默认范围"
            elif market.spread_pct is None or market.spread_pct > float(scoring.get("max_spread_pct", 0.12)):
                hard_filter = "买卖价差缺失或超过默认上限"
            elif market.open_interest is None or market.open_interest < float(scoring.get("min_open_interest", 100)):
                hard_filter = "Open Interest 缺失或低于默认下限"
            elif market.data_status != "ok":
                hard_filter = "缺少新鲜实时bid/ask，仅进入观察榜"
            elif market.data_conflicts:
                hard_filter = "多供应商实时行情冲突，暂停生成入场限价"
            if hard_filter:
                evaluation.score = min(evaluation.score, 64.0)
                evaluation.grade = "C" if evaluation.score >= 50 else "D"
                evaluation.eligible = False
                evaluation.risk_flags.append(hard_filter)
            recommendation_id = self.database.save_recommendation(
                evaluation, [int(item.id) for item in signals if item.id], target_date
            )
            candidate = build_candidate(evaluation, market, portfolio, paper)
            execution = {
                "strategy": candidate.strategy, "contract_key": candidate.market.contract_key,
                "futu_code": market.futu_code, "bid": market.bid, "ask": market.ask,
                "last": market.last, "volume": market.volume, "open_interest": market.open_interest,
                "iv": market.implied_volatility, "delta": market.delta, "spread_pct": market.spread_pct,
                "entry_debit": candidate.entry_debit, "max_entry_price": candidate.max_entry_price,
                "underlying_entry": candidate.underlying_entry, "underlying_target": candidate.underlying_target,
                "underlying_stop": candidate.underlying_stop, "quantity": candidate.quantity,
                "quantity_status": candidate.quantity_status, "risk_per_contract": candidate.risk_per_contract,
                "max_loss": candidate.max_loss, "take_profit": candidate.take_profit,
                "stop_loss": candidate.stop_loss,
                "valid_until": candidate.valid_until.isoformat() if candidate.valid_until else None,
                "invalidation": candidate.invalidation, "data_quality": candidate.data_quality,
                "market_status": candidate.market.data_status,
                "market_observed_at": candidate.market.observed_at.isoformat(),
            }
            self.database.attach_execution(recommendation_id, execution)
            output.append({
                "recommendation_id": recommendation_id, "contract_key": evaluation.contract_key,
                "grade": evaluation.grade, "score": evaluation.score,
                "direction": evaluation.final_direction, "eligible": evaluation.eligible,
                **execution,
            })
        if active_codes:
            try:
                self.ibkr.subscribe(active_codes)
            except Exception as exc:
                self._last_error = f"ibkr_subscribe:{type(exc).__name__}:{str(exc)[:120]}"
        output.sort(key=lambda item: float(item["score"]), reverse=True)
        return output

    def collect(self, backfill: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                messages = self._collect_messages(backfill=backfill)
                self._ingest(messages)
                results = self._evaluate(self._trade_date())
                self._last_results = results
                self._last_collection = datetime.utcnow().isoformat()
                self._last_error = None
                strong = [item for item in results if item["grade"] == "A" and item["eligible"]]
                if strong:
                    body = "\n".join(
                        f"**{item['contract_key']}**｜{item['score']:.1f}｜{item['direction']}｜"
                        f"触发 {item.get('underlying_entry') or '--'}｜限价 {item.get('max_entry_price') or '--'}"
                        for item in strong[:3]
                    )
                    self.feishu.enqueue_card(build_card("A级异常期权提醒", body, "red"), source_message_id="strong-" + self._last_collection)
                source_health = self.source.health()
                if source_health.get("status") == "login_required":
                    self.feishu.enqueue_card(
                        build_card("Discord登录待恢复", "请打开NAS设置页查看二维码并扫码。游标已保留，登录后自动补采。", "orange", note=str(source_health.get("login_qr_path", ""))),
                        source_message_id="discord-login",
                    )
                return results
            except Exception as exc:
                self._last_error = f"collect:{type(exc).__name__}:{str(exc)[:120]}"
                return []

    def backfill(self) -> List[Dict[str, Any]]:
        return self.collect(backfill=True)

    def publish_daily(self) -> Optional[str]:
        report_date = self._trade_date()
        return self.feishu.enqueue_card(
            build_card(f"异常期权日报｜{report_date.isoformat()}", self._report_text(report_date, include_ai=True), "blue"),
            source_message_id="daily-" + report_date.isoformat(),
        )

    def run_backtests(self) -> Dict[str, int]:
        self._last_backtest = self.backtests.record_daily(self._trade_date())
        update_analyst_weights_from_outcomes(self.database)
        return self._last_backtest

    def run_optimizer(self) -> Dict[str, Any]:
        self._last_optimization = self.backtests.optimize_weekly()
        if self._last_optimization.get("status") in {"promoted", "shadow_started"}:
            self.feishu.enqueue_card(
                build_card("策略周报", json.dumps(self._last_optimization, ensure_ascii=False), "purple"),
                source_message_id="optimizer-" + datetime.utcnow().strftime("%Y%m%d%H"),
            )
        return self._last_optimization

    def check_ai_budget(self) -> Dict[str, Any]:
        health = self.ai.health()
        budget = health.get("budget", {}) if isinstance(health.get("budget"), dict) else {}
        ratio = float(budget.get("usage_ratio", 0.0))
        if ratio >= 0.8:
            self.feishu.enqueue_card(
                build_card("DeepSeek费用提醒", f"本月已用 {ratio:.0%}，限额 ¥{float(budget.get('limit_cny', 30)):.0f}。数值流程继续运行。", "orange"),
                source_message_id="ai-budget-" + datetime.utcnow().strftime("%Y-%m"),
            )
        return health

    def _report_text(self, report_date: date, include_ai: bool = False) -> str:
        text = daily_report(self.database, report_date)
        if include_ai and self.ai.enabled:
            explanations = []
            for rank in range(1, 4):
                payload = self._ranked_payload(rank)
                if payload:
                    summary = self.ai.summarize(payload)
                    if summary.text:
                        explanations.append(f"{rank}. {summary.text}")
            if explanations:
                text += "\n\n## AI易读说明（不改变数值）\n" + "\n".join(explanations)
        return text

    def today_recommendations(self) -> str:
        return self._report_text(self._trade_date(), include_ai=True)

    def positions(self) -> str:
        if not self._portfolio:
            try:
                self.sync_broker(force=True)
            except Exception:
                pass
        return portfolio_markdown(self._portfolio)

    def add_watchlist(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.strip().upper()
        self.database.upsert_watchlist(symbol, source="manual_v2", group_name="Options Radar")
        return {"status": "ready", "symbol": symbol, "source": "manual_v2"}

    def remove_watchlist(self, symbol: str) -> Dict[str, Any]:
        self.database.remove_watchlist(symbol.strip().upper())
        return {"status": "ready", "symbol": symbol.strip().upper()}

    def import_futu_watchlist(self) -> Dict[str, Any]:
        """One-time migration helper. It never participates in daily pricing."""
        watchlists = self.futu.sync_watchlists()
        imported = []
        for group, codes in watchlists.groups.items():
            for code in codes:
                symbol = code.split(".", 1)[-1].upper()
                self.database.upsert_watchlist(symbol, source="futu_import", group_name=str(group))
                imported.append(symbol)
        return {"status": "ready", "count": len(sorted(set(imported))), "symbols": sorted(set(imported))}

    def _ranked_payload(self, rank: int) -> Optional[Dict[str, Any]]:
        eligible, symbols = [], set()
        for row in self.database.recommendations_for_date(self._trade_date()):
            payload = json.loads(str(row["payload_json"]))
            symbol = str(payload.get("contract_key", "")).split("|", 1)[0]
            if payload.get("eligible") and float(payload.get("score", 0)) >= 65 and symbol not in symbols:
                eligible.append(payload)
                symbols.add(symbol)
        return eligible[rank - 1] if 0 < rank <= len(eligible) else None

    def explain_rank(self, rank: int) -> str:
        payload = self._ranked_payload(rank)
        if payload is None:
            return f"今日合格推荐中没有第 {rank} 名。"
        answer = self.ai.summarize(payload, use_pro=bool(payload.get("disagreement")))
        return answer.text or f"{payload['contract_key']}：{payload['score']:.1f}分，方向 {payload['final_direction']}。"

    def free_chat(self, question: str) -> str:
        context = {
            "top_recommendations": [self._ranked_payload(index) for index in range(1, 4)],
            "watchlist": self._watch_symbols(),
            "portfolio_summary": [
                {"symbol": symbol, "held": value.held_quantity != 0, "concentration": value.concentration}
                for symbol, value in self._portfolio.items()
            ],
        }
        answer = self.ai.answer(question, context)
        return answer.text or "已检查当前日报与脱敏组合上下文，暂无更多可引用数据。"

    # Dashboard callbacks -------------------------------------------------
    def dashboard_recommendations(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        return [json.loads(str(row["payload_json"])) for row in self.database.recommendations_for_date(self._trade_date())]

    def dashboard_portfolio(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        return {symbol: _as_jsonable(context) for symbol, context in self._portfolio.items()}

    def dashboard_signals(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        rows = []
        for event in self.database.flow_events_for_date(self._trade_date()):
            rows.append({
                "event_key": event.event_key,
                "contract_key": event.contract_key,
                "symbol": event.symbol,
                "observed_at": event.observed_at.isoformat(),
                "signals": [_as_jsonable(signal) for signal in self.database.signals_for_event(event.event_key)],
            })
        return rows

    def dashboard_rules(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        return self.database.source_rules()

    def dashboard_analysts(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        return self.database.analyst_rows()

    def dashboard_backtest(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        return {"paper": self.database.paper_stats(), "last_run": self._last_backtest, "optimization": self._last_optimization}

    def dashboard_contracts(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        return self.dashboard_recommendations({})

    def dashboard_providers(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        result = self.providers.statuses()
        result["ibkr_flex"] = {
            "configured": self.ibkr_flex.configured, "connected": False,
            "status": "configured" if self.ibkr_flex.configured else "not_configured",
            "quality": "eod",
        }
        return result

    def provider_status(self, provider: str = "", **_kwargs: Any) -> Any:
        return self.providers.status(str(provider))

    def test_provider(self, provider: str = "", **_kwargs: Any) -> Any:
        return self.providers.test(str(provider))

    def enable_provider(self, provider: str = "", **_kwargs: Any) -> Any:
        return self.providers.set_enabled(str(provider), True)

    def disable_provider(self, provider: str = "", **_kwargs: Any) -> Any:
        return self.providers.set_enabled(str(provider), False)

    def set_provider_priority(self, provider: str = "", priority: int = 0, **_kwargs: Any) -> Any:
        return {"market_priority": self.providers.set_priority(str(provider), int(priority))}

    def market_provenance(self, contract_key: str = "", **_kwargs: Any) -> Any:
        live = self.providers.provenance(str(contract_key))
        live["stored_points"] = self.database.market_provenance(str(contract_key))
        return live

    def market_compare(self, contract_key: str = "", **_kwargs: Any) -> Any:
        return self.providers.compare(str(contract_key))

    def discover_ibkr(self, **_kwargs: Any) -> Any:
        return {"endpoints": [item.__dict__ for item in self.ibkr.discover()]}

    def sync_ibkr(self, **_kwargs: Any) -> Any:
        snapshots = self.ibkr.sync_accounts()
        for item in snapshots:
            self.database.save_broker_snapshot(BrokerSnapshot(
                as_of=_naive_utc(item.observed_at) or datetime.utcnow(), nav=item.nav, cash=item.cash,
                positions=item.positions, source="ibkr", quality=item.quality,
            ))
        return {
            "status": "ok", "accounts": len(snapshots),
            "positions": sum(len(item.positions) for item in snapshots),
            "nav_present": any(item.nav is not None for item in snapshots),
        }

    def dashboard_callbacks(self) -> Dict[str, Any]:
        def provider_action(payload: Mapping[str, Any]) -> Any:
            action = str(payload.get("action", "test"))
            methods = {
                "test": self.test_provider, "enable": self.enable_provider,
                "disable": self.disable_provider, "priority": self.set_provider_priority,
            }
            method = methods.get(action)
            if method is None:
                raise ValueError(f"unknown provider action: {action}")
            return method(**{key: value for key, value in payload.items() if key != "action"})

        return {
            "recommendations": self.dashboard_recommendations,
            "portfolio": self.dashboard_portfolio,
            "signals": self.dashboard_signals,
            "rules": self.dashboard_rules,
            "analysts": self.dashboard_analysts,
            "backtest": self.dashboard_backtest,
            "contracts": self.dashboard_contracts,
            "providers": self.dashboard_providers,
            "provider_status": lambda payload: self.provider_status(**payload),
            "provider_test": lambda payload: self.test_provider(**payload),
            "provider_enable": lambda payload: self.enable_provider(**payload),
            "provider_disable": lambda payload: self.disable_provider(**payload),
            "provider_priority": lambda payload: self.set_provider_priority(**payload),
            "provider_action": provider_action,
            "market_provenance": lambda payload: self.market_provenance(**payload),
            "market_compare": lambda payload: self.market_compare(**payload),
            "ibkr_discover": lambda payload: self.discover_ibkr(**payload),
            "ibkr_sync": lambda payload: self.sync_ibkr(**payload),
            "collect": lambda payload: self.collect(bool(payload.get("backfill", False))),
            "report": lambda _payload: {"queue_id": self.publish_daily()},
            "futu_sync": lambda _payload: _as_jsonable(self.sync_broker(force=True)),
            "futu_import_watchlist": lambda _payload: self.import_futu_watchlist(),
        }

    def _run_feishu(self) -> None:
        try:
            self.feishu.run_forever()
        except Exception as exc:
            self._last_error = f"feishu:{type(exc).__name__}:{str(exc)[:120]}"

    def start(self) -> None:
        if self._started:
            return
        if not bool(self.config.raw.get("setup_completed", False)):
            self._last_error = "setup_required"
            return
        from apscheduler.executors.pool import ThreadPoolExecutor
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        self._scheduler = BackgroundScheduler(
            timezone=self.config.raw.get("timezone", "Asia/Shanghai"), daemon=True,
            executors={"default": ThreadPoolExecutor(1)},
        )
        self._scheduler.add_job(self.collect, "interval", seconds=60, id="discord-poll", max_instances=1, coalesce=True)
        self._scheduler.add_job(self.backfill, "interval", minutes=10, id="discord-rescan", max_instances=1, coalesce=True)
        self._scheduler.add_job(self.backfill, CronTrigger(hour=16, minute=30, timezone="America/New_York"), id="discord-close-backfill")
        self._scheduler.add_job(self.sync_broker, "interval", minutes=5, id="ibkr-sync", max_instances=1, coalesce=True)
        self._scheduler.add_job(self.publish_daily, CronTrigger(hour=17, minute=15, timezone="America/New_York"), id="daily-report")
        self._scheduler.add_job(self.run_backtests, CronTrigger(hour=18, minute=0, timezone="America/New_York"), id="daily-backtest")
        self._scheduler.add_job(self.run_optimizer, CronTrigger(day_of_week="sun", hour=9, minute=0, timezone="Asia/Shanghai"), id="weekly-optimizer")
        self._scheduler.add_job(self.check_ai_budget, "interval", hours=1, id="ai-budget")
        self._scheduler.start()
        self._feishu_thread = threading.Thread(target=self._run_feishu, name="feishu-websocket", daemon=True)
        self._feishu_thread.start()
        self._started = True

    def stop(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
        if not self._feishu_closed:
            self.feishu.close()
            self._feishu_closed = True
        self.source.close()
        self.futu.close()
        self.ibkr.close()
        self._started = False

    def health(self) -> Dict[str, Any]:
        broker = self.database.latest_broker_snapshot()
        return {
            "status": "degraded" if self._last_error else ("ok" if self._started else "stopped"),
            "last_collection": self._last_collection, "last_sync": self._last_sync,
            "last_error": self._last_error, "discord": self.source.health(),
            "opend": {"status": "migration_only", "enabled": False},
            "ibkr": self.providers.status("ibkr"),
            "ai": self.ai.health(), "feishu": self.feishu.health(),
            "portfolio": {
                "source": broker.source if broker else "ibkr", "as_of": broker.as_of.isoformat() if broker else None,
                "nav_present": bool(broker and broker.nav is not None),
                "positions": len(broker.positions) if broker else 0,
            },
            "watchlist_count": len(self._watch_symbols()),
            "providers": self.providers.statuses(),
            "last_backtest": self._last_backtest, "last_optimization": self._last_optimization,
        }


def create_service(config_path: str, data_dir: str) -> OptionsRadarService:
    return OptionsRadarService(config_path=config_path, data_dir=data_dir)
