"""Main NAS service for Discord signals, Futu OpenD data and Feishu output.

All scores, filters, sizing and back-test results are produced by deterministic
Python code.  DeepSeek is limited to schema-checked extraction and prose.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .ai_provider import DeepSeekProvider
from .analyst_backtest import AnalystBacktestCoordinator, CompositeBarsSource
from .analytics import (
    compute_family_alphas,
    update_analyst_weights_from_backtest,
)
from .backtest_service import BacktestCoordinator
from .backup_providers import AlpacaProvider
from .config import AppConfig, load_config
from .db import Database
from .discord_rest import DiscordRestSource, read_token
from .discord_source import DiscordBrowserSource
from .feishu import FeishuBot, FeishuCallbacks, build_card
from .futu_provider import (
    FutuHistoryBar,
    FutuMarketSnapshot,
    FutuOptionContract,
    FutuProvider,
)
from .gex import GexResult, compute_gex
from .history_adapters import AlpacaHistoryAdapter, SyntheticHistoryAdapter
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


def _format_price(value: Any) -> str:
    if value is None:
        return "待行情"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "待行情"


def _format_premium(value: Any) -> str:
    """Render option trade premium like the source feed does: $3.2M, $968K."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number <= 0:
        return "-"
    if number >= 1_000_000:
        text = f"{number / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"${text}M"
    if number >= 1_000:
        text = f"{number / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"${text}K"
    return f"${number:,.0f}"


def _to_float(value: Any) -> Optional[float]:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


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
        "feishu_webhook": "FEISHU_WEBHOOK_URL_FILE",
        "discord_user_token": "DISCORD_USER_TOKEN_FILE",
        "massive_api_key": "MASSIVE_API_KEY_FILE",
        "alpaca_api_key": "ALPACA_API_KEY_FILE",
        "alpaca_api_secret": "ALPACA_API_SECRET_FILE",
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

    def underlying_features(self, symbol: str, end: date, lookback_days: int = 45) -> Dict[str, Optional[float]]:
        try:
            bars = self.provider.get_underlying_bars(symbol, end - timedelta(days=lookback_days), end, "1 day")
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
        # Full-text LLM refinement for every analyst card is expensive (one
        # call per message).  Local deterministic parsing covers the vast
        # majority of cards; enable refinement only where the operator asked.
        ai_refine = str(self.config.section("ai").get("refine_analyst_cards", "false")).strip().lower()
        self.ai_refine_enabled = ai_refine in {"1", "true", "yes", "on"}
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
            market_data_type=int(ibkr_config.get("market_data_type", 3)),
        )
        self.ibkr_flex = IBKRFlexClient()
        self.massive = MassiveClient()
        self.massive_backtest = MassiveClient(requests_per_minute=5)
        def secret_value(direct: str, file_var: str) -> str:
            value = os.getenv(direct, "").strip()
            path = os.getenv(file_var, "").strip()
            if not value and path and Path(path).is_file():
                value = Path(path).read_text(encoding="utf-8").strip()
            return value

        alpaca_config = self.config.section("alpaca")
        self.alpaca = AlpacaProvider(
            secret_value("ALPACA_API_KEY", "ALPACA_API_KEY_FILE"),
            secret_value("ALPACA_API_SECRET", "ALPACA_API_SECRET_FILE"),
            base_url=str(alpaca_config.get("data_base_url", "https://data.alpaca.markets")),
            contracts_base_url=str(alpaca_config.get("contracts_base_url", "https://paper-api.alpaca.markets")),
            feed=str(alpaca_config.get("feed", "indicative")),
        )
        self.alpaca_history = AlpacaProvider(
            secret_value("ALPACA_API_KEY", "ALPACA_API_KEY_FILE"),
            secret_value("ALPACA_API_SECRET", "ALPACA_API_SECRET_FILE"),
            base_url=str(alpaca_config.get("data_base_url", "https://data.alpaca.markets")),
            contracts_base_url=str(alpaca_config.get("contracts_base_url", "https://paper-api.alpaca.markets")),
            feed=str(alpaca_config.get("feed", "indicative")),
        )
        execution_config = dict(provider_config.get("execution", {}))
        enabled_config = dict(provider_config.get("enabled", {}))
        default_priority = ["futu", "alpaca", "massive"]
        self.providers = ProviderRegistry(
            {
                "futu": FutuUnifiedProvider(self.futu),
                "alpaca": self.alpaca,
                "massive": MassiveUnifiedProvider(self.massive),
            },
            priority=provider_config.get("market_priority", default_priority),
            enabled=enabled_config,
            max_quote_age_seconds=int(execution_config.get("max_quote_age_seconds", 60)),
            conflict_threshold_pct=float(execution_config.get("conflict_threshold_pct", 15)),
        )
        market_priority = list(provider_config.get("market_priority", default_priority))
        # Underlying-feature enrichment (HV/ATR/trend) uses Alpaca daily bars:
        # it is far faster (200 req/min vs Massive 5 req/min) and covers more
        # symbols. Analyst signal back-test also uses Alpaca daily bars below.
        self.history_market = AlpacaHistoryAdapter(self.alpaca_history)
        backtest_config = self.config.section("backtest")
        if bool(backtest_config.get("use_synthetic_when_unavailable", True)):
            self.history_market = SyntheticHistoryAdapter(self.history_market, enabled=True)
        self.backtests = BacktestCoordinator(self.database, self.history_market, self.config.section("paper"))
        self.analyst_backtests = AnalystBacktestCoordinator(
            self.database, CompositeBarsSource(self.alpaca_history, self.massive),
        )
        self.rulebook = RulebookCompiler(self.database, self.config.section("analyst_families"))
        self._last_backtest: Optional[Dict[str, Any]] = None
        self._last_backtest_at: float = 0.0
        self._last_optimization: Optional[Dict[str, Any]] = None
        self._backtest_lock = threading.Lock()
        self._backtest_running = False
        self._backtest_summary: Dict[str, Any] = {}
        self._analyst_backtest_running = False
        self._analyst_backtest_at: float = 0.0

        discord = self.config.section("discord")
        channels = dict(discord.get("channel_urls", {}))
        # Legacy configs store a name->role mapping. Only fall back to it when
        # no explicit channel URLs are configured; URLs let the browser
        # navigate directly and are preferred.
        source_channels = discord.get("source_channels")
        if not channels and isinstance(source_channels, dict):
            channels = {str(role): str(name) for name, role in source_channels.items()}
        elif isinstance(source_channels, dict):
            for name, role in source_channels.items():
                channels.setdefault(str(role), str(name))
        self.channel_roles = {str(role): str(role) for role in channels}
        if discord_source is not None:
            self.source = discord_source
        else:
            # Browser DOM collection is the primary channel: it reads the
            # logged-in session's visible content, including subscription
            # posts that the REST API cannot see.
            self.source = DiscordBrowserSource(
                profile_dir=self.data_dir / "browser-profile",
                evidence_dir=self.data_dir / "evidence",
                channel_urls=channels,
                server_name=str(discord.get("source_server", "")),
                headless=(False if os.getenv("OPTIONS_RADAR_LOCAL") == "1" else bool(discord.get("headless", True))),
            )
        self._portfolio: Dict[str, PortfolioContext] = {}
        self._stock_meta: Dict[str, Dict[str, Any]] = {}
        self._portfolio_refresh_lock = threading.Lock()
        self._portfolio_refresh_running = False
        self._portfolio_refresh_status = "idle"
        self._portfolio_refresh_error: Optional[str] = None
        self._last_results: List[Dict[str, Any]] = []
        self._last_top5_keys: set = set()
        self._last_collection: Optional[str] = None
        self._last_sync: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_market_by_contract: Dict[str, MarketSnapshot] = {}
        self._scheduler: Any = None
        self._feishu_thread: Optional[threading.Thread] = None
        self._started = False
        self._feishu_closed = False
        self._lock = threading.RLock()
        self._health_cache: Optional[Dict[str, Any]] = None
        self._health_cache_at: float = 0.0
        self._health_cache_lock = threading.Lock()
        self._group_cache: Optional[Dict[str, str]] = None
        self._group_cache_at: float = 0.0
        self._group_cache_lock = threading.Lock()
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
            signal = parse_analyst_message(
                message, refiner=self.refiner if (self.ai.enabled and self.ai_refine_enabled) else None
            )
            if signal:
                signal.raw_message_id = raw_id
                self.database.insert_signal(signal)
        if rule_messages:
            self.rulebook.compile(rule_messages)

    def _collect_messages(self, backfill: bool = False) -> List[RawMessage]:
        output: List[RawMessage] = []
        for channel_id in self.source.channel_urls:
            role = self.channel_roles.get(channel_id, channel_id)
            cursor = self.database.get_source_cursor(channel_id)
            fetch_cursor = cursor
            pages = 24
            if backfill:
                # Backfill ~90 trading days (≈126 calendar days incl. weekends)
                # so the analyst back-test has a 3-month sample.
                fetch_cursor = SourceCursor(
                    channel_id=channel_id, last_message_id="0",
                    last_timestamp=datetime.utcnow() - timedelta(days=126),
                )
                pages = 200
            try:
                # Per-channel timeout keeps one slow/hung channel from
                # blocking the whole poll cycle.
                messages = self.source.fetch_since(channel_id, fetch_cursor, scroll_pages=pages, timeout_ms=60000)
            except Exception as exc:
                # Continue with other channels; one unavailable channel must
                # not erase valid analyst messages.
                self._last_error = f"discord:{channel_id}:{type(exc).__name__}:{str(exc)[:120]}"
                continue
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

    def _underlying_features(self, symbol: str, target_date: date) -> Dict[str, Optional[float]]:
        return self.history_market.underlying_features(symbol, target_date, lookback_days=45)

    def _market_for_event(self, event: FlowEvent, target_date: date) -> MarketSnapshot:
        composite = self.providers.composite_snapshot(event.contract_key)
        snapshot = market_snapshot_from_composite(composite)
        features = self._underlying_features(event.symbol, target_date)
        snapshot.trend_alignment = features["trend"]
        snapshot.underlying_previous_high = features["high"]
        snapshot.underlying_previous_low = features["low"]
        snapshot.underlying_atr14 = features["atr"]
        snapshot.underlying_hv = features.get("hv")
        futu_candidate = composite.candidates.get("futu")
        if futu_candidate:
            snapshot.futu_code = futu_candidate.instrument_code
        snapshot.atm_iv = self._atm_iv(event, snapshot)
        overview = self._underlying_overview(str(event.symbol))
        if overview:
            snapshot.iv_rank = overview.get("iv_rank")
            snapshot.hv_30d = overview.get("hv_30d")
        self.database.save_market_data_points(
            event.contract_key,
            {name: {
                "value": point.value, "source": point.provider, "quality": point.quality,
                "market_timestamp": point.market_timestamp.isoformat() if point.market_timestamp else None,
            } for name, point in composite.fields.items()},
        )
        return snapshot

    def _atm_iv(self, event: FlowEvent, snapshot: MarketSnapshot) -> Optional[float]:
        """ATM implied volatility for the same expiry, used for the VRP premium check.

        The back-test sells the ATM strike, so the IV premium must be measured on
        the ATM option, not the (possibly ITM/OTM) flow contract whose IV carries
        skew. ATM put/call IV are nearly identical by put-call parity, so one leg
        suffices.
        """
        spot = snapshot.underlying_price
        if spot is None:
            spot = self._stock_spot(str(event.symbol))
        expiry = event.expiry
        if spot is None or not expiry:
            return None
        # The ATM strike must exist in the option chain; round(spot) may not
        # (e.g. wide 2.5/5 strikes), so pick the nearest listed strike like the
        # back-test's two-candidate fallback.
        root = str(event.contract_key).split("|", 1)[0]
        for strike in (round(spot), round(spot / 5.0) * 5.0):
            atm_key = f"{root}|{expiry.isoformat()}|{strike}|P"
            try:
                atm_composite = self.providers.composite_snapshot(atm_key)
                point = atm_composite.fields.get("implied_volatility")
                if point is not None and point.value is not None:
                    return float(point.value)
            except Exception:
                continue
        return None

    def _stock_spot(self, symbol: str) -> Optional[float]:
        """Realtime underlying price from Futu when the option quote lacks owner price."""
        try:
            snap = self.futu.get_snapshots([f"US.{symbol}"])
            stock = snap.get(f"US.{symbol}")
            return float(stock.last) if stock is not None and stock.last is not None else None
        except Exception:
            return None

    def _underlying_overview(self, symbol: str) -> Dict[str, Optional[float]]:
        """Futu underlying-level IV rank / HV stats (cached per process)."""
        cache = getattr(self, "_overview_cache", None)
        if cache is None:
            cache = {}
            self._overview_cache = cache
        if symbol in cache:
            return cache[symbol]
        try:
            overview = self.futu.get_underlying_overview([f"US.{symbol}"])
            result = overview.get(f"US.{symbol}", {}) if overview else {}
        except Exception:
            result = {}
        cache[symbol] = result
        return result

    def get_gex(self, symbol: str) -> Optional[GexResult]:
        """Compute (and cache) dealer gamma exposure for one underlying."""
        cache = getattr(self, "_gex_cache", None)
        if cache is None:
            cache = {}
            self._gex_cache = cache
        if symbol in cache:
            return cache[symbol]
        try:
            spot = self._stock_spot(symbol)
            if spot is None:
                return None
            result = compute_gex(self.futu, symbol, spot)
            if result.strikes:
                cache[symbol] = result
            return result
        except Exception:
            return None

    def gex_view(self, symbol: str = "", **_kwargs: Any) -> Dict[str, Any]:
        """JSON-friendly GEX snapshot for the dashboard curve."""
        symbol = str(symbol or "").strip()
        if not symbol:
            return {"status": "error", "message": "symbol required"}
        result = self.get_gex(symbol)
        if result is None:
            return {"status": "empty", "symbol": symbol}
        return {
            "status": "ok", "symbol": symbol, "spot": result.spot,
            "strikes": result.strikes, "net_gex": result.net_gex,
            "call_wall": result.call_wall, "put_wall": result.put_wall,
            "gamma_flip": result.gamma_flip, "zero_gamma": result.zero_gamma,
            "regime": result.regime,
            "max_pos_gex": result.max_pos_gex, "max_neg_gex": result.max_neg_gex,
            "expiry_count": result.expiry_count,
        }

    @staticmethod
    def _strategy_hint(direction: str, iv_rank: Optional[float], regime: Optional[str]) -> str:
        """Seller-first strategy hint from IV richness and gamma regime.

        IV rank >= 70 (expensive) is the only strong sell-side edge; negative
        gamma asks for defined-risk credit spreads instead of naked shorts.
        """
        if direction not in ("BULL", "BEAR"):
            return "方向不明，观望"
        expensive = iv_rank is not None and iv_rank >= 70.0
        cheap = iv_rank is not None and iv_rank <= 30.0
        neg_gamma = regime == "negative"
        if expensive:
            base = "Sell Put" if direction == "BULL" else "Sell Call"
            if neg_gamma:
                return f"{base}（负 Gamma，建议 Credit Spread 保护）"
            return f"{base}（IV 贵 + 环境允许）"
        if cheap:
            if direction == "BULL":
                return "IV 偏低，卖方无优势，优先 Buy Stock"
            return "IV 偏低，卖方无优势，观望"
        return "IV 中性，卖方机会一般"

    def _earnings(self) -> Dict[str, Dict[str, Any]]:
        cache = getattr(self, "_earnings_cache", None)
        if cache is None:
            cache = {"_loaded": False}
            self._earnings_cache = cache
        if cache.get("_loaded"):
            return cache
        try:
            cache.update(self.futu.get_earnings_screener(100))
        except Exception:
            pass
        cache["_loaded"] = True
        return cache

    def _event_risk(self, symbol: str) -> Dict[str, Any]:
        info = self._earnings().get(f"US.{symbol}") or {}
        earnings_time = str(info.get("earnings_time") or "")
        if not earnings_time:
            return {"event_risk": "LOW", "next_earnings_days": None, "expected_move": None}
        days = None
        try:
            earnings_date = date.fromisoformat(earnings_time[:10])
            days = (earnings_date - date.today()).days
        except ValueError:
            pass
        if days is not None and days <= 7:
            risk = "HIGH"
        elif days is not None and days <= 14:
            risk = "MEDIUM"
        else:
            risk = "LOW"
        return {
            "event_risk": risk,
            "next_earnings_days": days,
            "expected_move": info.get("expected_move_ratio"),
        }

    @staticmethod
    def _short_gamma_risk(gex: Optional[GexResult], event_risk: str) -> str:
        if gex is None:
            return "LOW"
        flags = 0
        if gex.regime == "negative":
            flags += 1
        if gex.gamma_flip is not None and gex.spot is not None and gex.spot > 0:
            if abs(gex.spot - gex.gamma_flip) / gex.spot <= 0.03:
                flags += 1
        if event_risk == "HIGH":
            flags += 1
        if flags >= 2:
            return "HIGH"
        if flags == 1:
            return "MEDIUM"
        return "LOW"

    @staticmethod
    def _strike_hint(direction: str, gex: Optional[GexResult]) -> Optional[str]:
        if gex is None:
            return None
        if direction == "BULL" and gex.put_wall is not None:
            return f"优先行权价 ${gex.put_wall}（Put Wall 支撑）"
        if direction == "BEAR" and gex.call_wall is not None:
            return f"优先行权价 ${gex.call_wall}（Call Wall 阻力）"
        return None

    def _evaluate(self, target_date: date, limit: Optional[int] = None, sync: bool = True) -> List[Dict[str, Any]]:
        if sync:
            try:
                self.sync_broker(force=True)
            except Exception as exc:
                self._last_error = f"futu_sync:{type(exc).__name__}:{str(exc)[:120]}"
        scoring = self.config.section("scoring")
        paper = self.config.section("paper")
        output: List[Dict[str, Any]] = []
        events = sorted(self._events_for_date(target_date), key=lambda item: item.observed_at, reverse=True)
        if limit:
            events = events[:max(1, int(limit))]
        for event in events:
            self.database.enrich_signals_for_event(event)
            signals = self.database.signals_for_event(event.event_key)
            if not signals:
                continue
            try:
                market = self._market_for_event(event, target_date)
            except Exception as exc:
                market = MarketSnapshot(event.contract_key, datetime.utcnow(), provider="futu_opend", data_status="missing")
                market.field_quality["error"] = f"{type(exc).__name__}:{str(exc)[:100]}"
            self._last_market_by_contract[event.contract_key] = market
            self.database.save_option_snapshot(event.contract_key, market.observed_at, _as_jsonable(market))
            portfolio = self._portfolio.get(event.symbol, PortfolioContext(
                symbol=event.symbol, in_watchlist=event.symbol in set(self._watch_symbols()),
            ))
            dte = event.dte if event.dte is not None else (event.expiry - target_date).days
            max_dte = int(scoring.get("max_dte", 60))

            if 14 <= dte <= max_dte:
                # -- 卖方流：DTE 14~60，卖 ATM PUT/CALL --
                evaluation = evaluate_consensus(
                    signals, self.database.get_weights(item.analyst for item in signals),
                    family_alphas=self.database.family_alphas(),
                    market=market, portfolio=portfolio,
                    recommendation_threshold=float(scoring.get("recommendation_threshold", 65)),
                    disagreement_threshold=float(scoring.get("disagreement_threshold", 0.25)),
                )
                quality_filter = None
                if market.delta is not None and not float(scoring.get("min_abs_delta", 0.30)) <= abs(market.delta) <= float(scoring.get("max_abs_delta", 0.65)):
                    quality_filter = "Delta 不在默认范围"
                elif market.spread_pct is not None and market.spread_pct > float(scoring.get("max_spread_pct", 0.12)):
                    quality_filter = "买卖价差超过默认上限"
                elif market.open_interest is not None and market.open_interest < float(scoring.get("min_open_interest", 100)):
                    quality_filter = "Open Interest 低于默认下限"
                if quality_filter:
                    evaluation.score = min(evaluation.score, 64.0)
                    evaluation.grade = "C" if evaluation.score >= 50 else "D"
                    evaluation.eligible = False
                    evaluation.risk_flags.append(quality_filter)
                missing_fields = []
                if market.data_status != "ok":
                    missing_fields.append("缺少新鲜实时bid/ask，仅进入观察榜")
                if market.delta is None:
                    missing_fields.append("Delta 缺失")
                if market.spread_pct is None:
                    missing_fields.append("买卖价差缺失")
                if market.open_interest is None:
                    missing_fields.append("Open Interest 缺失")
                if market.data_conflicts:
                    missing_fields.append("多供应商实时行情冲突，暂停生成入场限价")
                if missing_fields:
                    evaluation.eligible = False
                    evaluation.risk_flags.extend(missing_fields)
                recommendation_id = self.database.save_recommendation(
                    evaluation, [int(item.id) for item in signals if item.id], target_date, strategy_type="sell"
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
                gex = self.get_gex(str(event.symbol))
                if gex is not None:
                    execution["gex"] = {
                        "regime": gex.regime,
                        "call_wall": gex.call_wall,
                        "put_wall": gex.put_wall,
                        "gamma_flip": gex.gamma_flip,
                        "spot": gex.spot,
                    }
                execution["iv_rank"] = market.iv_rank
                execution["strategy_hint"] = self._strategy_hint(
                    evaluation.final_direction, market.iv_rank,
                    gex.regime if gex is not None else None,
                )
                event = self._event_risk(str(event.symbol))
                execution["event_risk"] = event["event_risk"]
                execution["next_earnings_days"] = event["next_earnings_days"]
                execution["expected_move"] = event["expected_move"]
                execution["short_gamma_risk"] = self._short_gamma_risk(gex, event["event_risk"])
                execution["strike_hint"] = self._strike_hint(evaluation.final_direction, gex)
                self.database.attach_execution(recommendation_id, execution)
                output.append({
                    "recommendation_id": recommendation_id, "contract_key": evaluation.contract_key,
                    "grade": evaluation.grade, "score": evaluation.score,
                    "direction": evaluation.final_direction, "eligible": evaluation.eligible,
                    "strategy_type": "sell", **execution,
                })
        output.sort(key=lambda item: float(item["score"]), reverse=True)
        return output

    def publish_top5(self) -> Optional[str]:
        """Send the top recommendations only when a new contract enters either list.

        Two streams are pushed: sell-side (DTE 14-60) and buy-side short-term
        (DTE 7-14, fpd). Repeating the same contracts every poll spams Feishu,
        so each stream tracks its previously-sent set and skips when unchanged.
        """
        result = self.dashboard_recommendations({"date": self._trade_date().isoformat()})
        if not isinstance(result, Mapping):
            return None
        sell = result.get("sell") or []
        buy = result.get("buy") or []
        last_keys = getattr(self, "_last_top5_keys", set())

        def _body(items, title):
            keys = {str(item.get("contract_key", "")) for item in items}
            if keys <= last_keys and last_keys:
                return None
            text = "\n".join(
                f"**#{i+1} {item['contract_key']}**｜{float(item.get('score', 0)):.1f}分｜"
                f"{item.get('grade', '-')}｜{item.get('direction', '-')}｜"
                f"行情:{item.get('market_status', '待行情')}｜执行:{item.get('execution_status', '待行情')}\n"
                f"理由：{item.get('reason', '暂无理由')}"
                for i, item in enumerate(items)
            )
            return text

        sent_any = False
        sell_body = _body(sell, "卖方")
        if sell_body:
            self.feishu.enqueue_card(build_card("今日推荐·卖方", sell_body, "blue"), source_message_id="top5-sell-" + datetime.utcnow().strftime("%Y%m%d"))
            sent_any = True
        buy_body = _body(buy, "买方短线")
        if buy_body:
            self.feishu.enqueue_card(build_card("今日推荐·买方短线", buy_body, "purple"), source_message_id="top5-buy-" + datetime.utcnow().strftime("%Y%m%d"))
            sent_any = True
        if sent_any:
            self._last_top5_keys = {str(item.get("contract_key", "")) for item in sell + buy}
        return "queued" if sent_any else None

    def collect(self, backfill: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                messages = self._collect_messages(backfill=backfill)
                self._ingest(messages)
                results = self._evaluate(self._trade_date())
                self._last_results = results
                self._last_collection = datetime.utcnow().isoformat()
                self._last_error = None
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
        self._last_backtest = self.analyst_backtests.run()
        self._last_backtest_at = time.monotonic()
        try:
            update_analyst_weights_from_backtest(self.database, horizon_days=0)
        except Exception:
            pass
        try:
            compute_family_alphas(self.database, horizon_days=0)
        except Exception:
            pass
        return self._last_backtest

    def replay_backtest(self, start: str = "", end: str = "") -> Dict[str, Any]:
        """Offline back-test replay over an explicit date range."""
        start_date = date.fromisoformat(start) if start else self._trade_date() - timedelta(days=30)
        end_date = date.fromisoformat(end) if end else self._trade_date()
        settlement = self.backtests.replay(start_date, end_date)
        summary = self.backtests.replay_summary(start_date, end_date)
        return {"settlement": settlement, **summary}

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
        try:
            watchlists = self.futu.sync_watchlists()
        except Exception as exc:
            return {
                "status": "error",
                "message": f"富途自选导入失败：{type(exc).__name__}",
                "detail": str(exc)[:300],
            }
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
    def _dashboard_date(self) -> date:
        """Return the most recent US session date with data.

        Prefers a session with stored recommendations; otherwise falls back to
        the most recent session that produced flow events so the dashboard
        updates as soon as new flow arrives, even before analysts confirm.
        """
        today = self._trade_date()
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT session_date FROM recommendations WHERE session_date IS NOT NULL ORDER BY session_date DESC LIMIT 1"
                ).fetchone()
            if row and row["session_date"]:
                return date.fromisoformat(str(row["session_date"]))
        except Exception:
            pass
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT session_date FROM flow_events WHERE session_date IS NOT NULL ORDER BY session_date DESC LIMIT 1"
                ).fetchone()
            if row and row["session_date"]:
                return date.fromisoformat(str(row["session_date"]))
        except Exception:
            pass
        return today

    @staticmethod
    def _recommendation_view(payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Flatten stored execution fields and build a user-facing reason."""
        execution = payload.get("execution") if isinstance(payload.get("execution"), Mapping) else {}
        result = dict(payload)
        result.update(dict(execution))
        result["execution"] = dict(execution)
        if not result.get("direction") and result.get("final_direction"):
            result["direction"] = result["final_direction"]

        votes = payload.get("votes") if isinstance(payload.get("votes"), list) else []
        vote_text = "、".join(
            f"{item.get('analyst', '?')}:{item.get('direction', '?')}/{item.get('decision', '?')}"
            for item in votes if isinstance(item, Mapping)
        )
        components = payload.get("components") if isinstance(payload.get("components"), Mapping) else {}
        component_labels = {"consensus": "方向共识", "analyst_history": "历史胜率", "signal_quality": "信号质量", "market_quality": "行情质量", "portfolio_fit": "组合适配"}
        component_text = "、".join(
            f"{component_labels.get(str(key), key)} {float(value):.1f}"
            for key, value in components.items()
            if key in component_labels
        )
        reasons = []
        if vote_text:
            reasons.append("分析师判断：" + vote_text)
        if component_text:
            reasons.append("评分组成：" + component_text)
        risk_flags = payload.get("risk_flags") if isinstance(payload.get("risk_flags"), list) else []
        if risk_flags:
            reasons.append("风险提示：" + "；".join(str(item) for item in risk_flags[:3]))

        required = ("bid", "ask", "max_entry_price", "take_profit", "stop_loss")
        missing = [field for field in required if result.get(field) is None]
        result["missing_execution_fields"] = missing
        result["data_complete"] = not missing
        result["execution_status"] = "可执行" if not missing else "待行情/字段未就绪"
        if missing:
            labels = {"bid": "bid", "ask": "ask", "max_entry_price": "入场价", "take_profit": "止盈", "stop_loss": "止损"}
            reasons.append("执行字段未就绪：" + "、".join(labels[field] for field in missing))
        result["reason"] = "；".join(reasons) or "暂无可引用的分析理由。"
        return result

    def reevaluate_today(self) -> Dict[str, Any]:
        """Re-score today's recommendations from the latest market data and weights.

        Unlike collect(), this does not re-sync Discord messages or IBKR
        positions, so it returns quickly and only refreshes scores.
        """
        target = self._trade_date()
        results = self._evaluate(target, sync=False)
        return {"status": "ok", "count": len(results), "date": target.isoformat()}

    def dashboard_recommendations(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        date_str = str((_payload or {}).get("date", "")).strip()
        session = date.fromisoformat(date_str) if date_str else self._dashboard_date()
        views = [
            self._recommendation_view(json.loads(str(row["payload_json"])))
            for row in self.database.recommendations_for_date(session)
        ]
        if views:
            premium_by_key = {
                str(event.contract_key): event.premium
                for event in self.database.flow_events_for_date(session)
            }
            for view in views:
                key = str(view.get("contract_key", ""))
                if key in premium_by_key and not view.get("premium"):
                    view["premium"] = premium_by_key[key]
            sell = sorted(
                [v for v in views if v.get("strategy_type") != "buy"],
                key=lambda item: float(item.get("score", 0)), reverse=True,
            )[:3]
            buy = sorted(
                [v for v in views if v.get("strategy_type") == "buy"],
                key=lambda item: float(item.get("score", 0)), reverse=True,
            )[:3]
            return {"sell": sell, "buy": buy, "session_date": session.isoformat(), "is_latest": session == self._dashboard_date()}
        # No analyst confirmation for this session yet: surface recent flow
        # events as observation candidates so the dashboard updates as new
        # flow arrives.
        flow_only = []
        for event in self.database.flow_events_for_date(session):
            signals = self.database.signals_for_event(event.event_key)
            flow_only.append({
                "contract_key": event.contract_key,
                "symbol": event.symbol,
                "grade": "-",
                "score": 0.0,
                "direction": "观察",
                "final_direction": "观察",
                "market_status": "flow",
                "eligible": False,
                "data_quality": "仅flow",
                "execution_status": "待分析师确认",
                "analyst_count": len(signals),
                "premium": event.premium,
                "observed_at": event.observed_at.isoformat(),
                "reason": "异常期权事件（无分析师确认）",
            })
        flow_only.sort(key=lambda item: float(item.get("premium", 0) or 0), reverse=True)
        return {"sell": flow_only[:10], "buy": [], "session_date": session.isoformat(), "is_latest": session == self._dashboard_date()}

    def dashboard_portfolio(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        metadata = self.database.all_instrument_metadata()
        watch_symbols = set()
        try:
            watch_symbols = {item.symbol for item in self.database.list_watchlist(enabled_only=True)}
        except Exception:
            pass
        flow_symbols = set()
        flow_by_symbol: Dict[str, List[str]] = {}
        try:
            with self.database.connect() as connection:
                flow_symbols = {str(row[0]) for row in connection.execute(
                    "SELECT DISTINCT symbol FROM flow_events WHERE symbol IS NOT NULL"
                ).fetchall()}
                flow_by_symbol = {}
                for row in connection.execute(
                    "SELECT symbol, contract_key, premium, observed_at FROM flow_events "
                    "WHERE symbol IS NOT NULL ORDER BY observed_at DESC"
                ).fetchall():
                    flow_by_symbol.setdefault(str(row["symbol"]), []).append(
                        f"{row['contract_key']} · ${float(row['premium']):,.0f}"
                    )
        except Exception:
            pass
        symbols = set(self._portfolio.keys())
        if not symbols:
            symbols = watch_symbols | set(metadata.keys())
        overview: Dict[str, Dict[str, Optional[float]]] = {}
        if watch_symbols:
            try:
                overview = self.futu.get_underlying_overview(
                    [f"US.{s}" for s in sorted(watch_symbols)]
                )
            except Exception:
                overview = {}
        gex_cache = getattr(self, "_gex_cache", {})
        group_by_symbol = self._futu_group_cache()
        result = {}
        for symbol in sorted(symbols):
            context = self._portfolio.get(symbol) or PortfolioContext(
                symbol=symbol, in_watchlist=symbol in watch_symbols,
            )
            item = _as_jsonable(context)
            meta = metadata.get(symbol, {})
            item["company_name"] = meta.get("name_en") or self._stock_meta.get(symbol, {}).get("name", "")
            item["company_name_zh"] = meta.get("name_zh") or ""
            item["industry"] = meta.get("industry") or ""
            item["group_name"] = meta.get("group_name") or group_by_symbol.get(symbol) or ""
            item["current_price"] = meta.get("current_price")
            item["change_pct"] = meta.get("change_pct")
            item["updated_at"] = meta.get("updated_at") or ""
            item["has_flow"] = symbol in flow_symbols
            if item["has_flow"]:
                item["flow_contracts"] = flow_by_symbol.get(symbol, [])[:6]
            ov = overview.get(f"US.{symbol}", {})
            item["iv_rank"] = ov.get("iv_rank")
            item["hv_30d"] = ov.get("hv_30d")
            gex = gex_cache.get(symbol)
            item["gex_regime"] = gex.regime if gex is not None else None
            result[symbol] = item
        return result

    def _futu_group_cache(self, force: bool = False) -> Dict[str, str]:
        """Map symbol -> Futu watchlist group name, refreshed at most every 10 min."""
        futu_client = getattr(self, "futu", None)
        if futu_client is None:
            return {}
        lock = getattr(self, "_group_cache_lock", None)
        if lock is None:
            return {}
        now = time.monotonic()
        with lock:
            if not force and self._group_cache is not None and now - self._group_cache_at < 600:
                return dict(self._group_cache)
            mapping: Dict[str, str] = {}
            try:
                snapshot = futu_client.sync_watchlists()
                for group_name, codes in snapshot.groups.items():
                    for code in codes:
                        symbol = str(code).split(".", 1)[-1].upper()
                        mapping.setdefault(symbol, str(group_name))
            except Exception:
                pass
            self._group_cache = mapping
            self._group_cache_at = now
            return dict(mapping)

    def _refresh_stock_meta(self) -> None:
        """Populate company names, prices and change for portfolio symbols.

        Uses one batched Alpaca stock snapshot and one batched asset lookup;
        IBKR is only used as a secondary name source when it is already
        connected.  Chinese names are produced by DeepSeek only when missing
        and are capped per refresh; results persist in SQLite.
        """
        try:
            watch = {item.symbol for item in self.database.list_watchlist(enabled_only=True)}
        except Exception:
            watch = set()
        symbols = list(set(self._portfolio.keys()) | watch)
        if not symbols:
            return
        group_by_symbol = self._futu_group_cache()
        # Holdings first, then watchlist; cap per-symbol asset lookups so one
        # manual refresh finishes quickly and fills the rest on later refreshes.
        symbols = sorted(
            symbols,
            key=lambda sym: (
                0 if (self._portfolio.get(sym) or PortfolioContext(sym)).held_quantity != 0 else 1,
                sym,
            ),
        )
        asset_budget = 40
        translate_budget = 15
        snapshots: Dict[str, Any] = {}
        valid_symbols = [sym for sym in symbols if sym and ".." not in sym and not sym.startswith(".")]
        for chunk_start in range(0, len(valid_symbols), 50):
            chunk = valid_symbols[chunk_start:chunk_start + 50]
            try:
                payload = self.alpaca._get("/v2/stocks/snapshots", {"symbols": ",".join(chunk), "feed": "iex"})
                raw = payload.get("snapshots") if isinstance(payload.get("snapshots"), Mapping) else payload
                if isinstance(raw, Mapping):
                    snapshots.update(dict(raw))
            except Exception:
                continue

        names: Dict[str, str] = {}
        try:
            payload = self.alpaca._request(
                f"{self.alpaca.contracts_base_url}/v2/assets",
                {"symbols": ",".join(symbols)}, headers=self.alpaca._headers,
            )
            rows = payload.get("data") if isinstance(payload, Mapping) and isinstance(payload.get("data"), list) else (
                payload if isinstance(payload, list) else []
            )
            for row in rows:
                if isinstance(row, Mapping) and row.get("symbol") and row.get("name") and str(row.get("status", "")).lower() == "active":
                    names[str(row["symbol"]).upper()] = str(row["name"])
        except Exception:
            pass

        def _asset_name(sym: str) -> str:
            try:
                payload = self.alpaca._request(
                    f"{self.alpaca.contracts_base_url}/v2/assets/{urllib.parse.quote(sym)}",
                    {}, headers=self.alpaca._headers,
                )
                return str(payload.get("name") or "") if isinstance(payload, Mapping) else ""
            except Exception:
                return ""

        translate_budget = 15
        for sym in symbols:
            item = snapshots.get(sym) if isinstance(snapshots.get(sym), Mapping) else None
            price: Optional[float] = None
            change_pct: Optional[float] = None
            if item:
                quote = item.get("latestQuote") if isinstance(item.get("latestQuote"), Mapping) else {}
                trade = item.get("latestTrade") if isinstance(item.get("latestTrade"), Mapping) else {}
                prev = item.get("prevDailyBar") if isinstance(item.get("prevDailyBar"), Mapping) else {}
                price = _to_float(trade.get("p") or quote.get("bp") or quote.get("ap"))
                prev_close = _to_float(prev.get("c"))
                if price is not None and prev_close:
                    change_pct = (price - prev_close) / prev_close
            if price is None:
                # Futu provides a realtime stock quote when Alpaca's IEX feed is
                # empty (for example a thinly covered symbol or market close).
                try:
                    futu_snap = self.futu.get_snapshots([f"US.{sym}"])
                    stock = futu_snap.get(f"US.{sym}")
                    if stock is not None and stock.last is not None:
                        price = float(stock.last)
                except Exception:
                    pass
            name_en = names.get(sym) or sym
            if name_en == sym and sym not in names and asset_budget > 0:
                found = _asset_name(sym)
                asset_budget -= 1
                name_en = found or sym
            existing = self.database.instrument_metadata(sym)
            name_zh = str(existing.get("name_zh") or "") if existing else ""
            if name_zh.startswith("{") or name_zh.startswith('"'):
                name_zh = ""
            if not name_zh and name_en != sym and self.ai.enabled and translate_budget > 0:
                try:
                    translated = self.ai.translate_name(name_en)
                    name_zh = (translated.text or "").strip()
                    translate_budget -= 1
                except Exception:
                    pass
            self._stock_meta[sym] = {"name": name_en, "price": price, "change_pct": change_pct}
            self.database.save_instrument_metadata(
                sym, name_en=name_en, name_zh=name_zh or None,
                industry=None, group_name=group_by_symbol.get(sym), current_price=price,
                change_pct=change_pct, source="manual_refresh",
            )

    def _portfolio_refresh_worker(self) -> None:
        try:
            self._portfolio_refresh_status = "syncing"
            self._portfolio_refresh_error = None
            try:
                self.sync_broker(force=True)
            except Exception as exc:
                self._portfolio_refresh_error = f"broker_sync:{type(exc).__name__}:{str(exc)[:150]}"
            self._refresh_stock_meta()
            self._portfolio_refresh_status = "ready"
            self._last_sync = datetime.utcnow().isoformat()
        except Exception as exc:
            self._portfolio_refresh_status = "error"
            self._portfolio_refresh_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            with self._portfolio_refresh_lock:
                self._portfolio_refresh_running = False

    def refresh_portfolio(self, _payload: Optional[Mapping[str, Any]] = None, **_kwargs: Any) -> Dict[str, Any]:
        """Queue one manual portfolio/metadata refresh without blocking the page."""
        with self._portfolio_refresh_lock:
            if self._portfolio_refresh_running:
                return {"status": "running", "message": "持仓正在后台刷新，请稍候刷新页面。"}
            self._portfolio_refresh_running = True
        threading.Thread(
            target=self._portfolio_refresh_worker,
            name="portfolio-refresh",
            daemon=True,
        ).start()
        return {"status": "queued", "message": "持仓刷新已开始，稍后刷新页面查看结果。"}

    def dashboard_signals(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        date_str = str((_payload or {}).get("date", "")).strip()
        session = date.fromisoformat(date_str) if date_str else self._dashboard_date()
        rows = []
        for event in self.database.flow_events_for_date(session):
            rows.append({
                "event_key": event.event_key,
                "contract_key": event.contract_key,
                "symbol": event.symbol,
                "premium": event.premium,
                "observed_at": event.observed_at.isoformat(),
                "signals": [_as_jsonable(signal) for signal in self.database.signals_for_event(event.event_key)],
            })
        return rows

    def dashboard_rules(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        return self.database.source_rules()

    def dashboard_analysts(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        return self.database.analyst_rows()

    def dashboard_dates(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        """Return all distinct session dates with flow or recommendations, newest first."""
        try:
            with self.database.connect() as connection:
                rows = connection.execute(
                    "SELECT session_date FROM flow_events WHERE session_date IS NOT NULL "
                    "UNION SELECT session_date FROM recommendations WHERE session_date IS NOT NULL "
                    "ORDER BY session_date DESC LIMIT 60"
                ).fetchall()
            return [str(row["session_date"]) for row in rows]
        except Exception:
            return []

    def _backtest_worker(self, start: date, end: date) -> None:
        """Run replay in the background so the page never blocks on slow bars."""
        try:
            self.backtests.replay(start, end)
            with self._backtest_lock:
                self._backtest_summary = self.backtests.replay_summary(start, end)
                self._last_backtest = datetime.utcnow().isoformat()
                self._last_backtest_at = time.monotonic()
        except Exception:
            with self._backtest_lock:
                self._backtest_summary = {"error": "replay_failed"}
        finally:
            with self._backtest_lock:
                self._backtest_running = False

    def _analyst_backtest_worker(self) -> None:
        """Replay TRADE signals against daily bars in the background."""
        try:
            self.analyst_backtests.run()
            try:
                update_analyst_weights_from_backtest(self.database, horizon_days=0)
            except Exception:
                pass
            try:
                compute_family_alphas(self.database, horizon_days=0)
            except Exception:
                pass
            self._analyst_backtest_at = time.monotonic()
        except Exception as exc:
            self._last_error = f"analyst_backtest:{type(exc).__name__}:{str(exc)[:120]}"
        finally:
            with self._backtest_lock:
                self._analyst_backtest_running = False

    def _analyst_backtest_pending(self) -> int:
        """Count TRADE signal groups lacking sell-side series or buy-side outcomes."""
        with self.database.connect() as conn:
            signal_count = conn.execute(
                """SELECT COUNT(*) FROM (
                       SELECT p.analyst, f.contract_key
                       FROM parsed_signals p
                       JOIN flow_events f ON p.flow_event_key = f.event_key
                       WHERE p.decision='TRADE' AND p.direction IN ('BULL','BEAR')
                         AND f.session_date IS NOT NULL
                         AND julianday(f.expiry) - julianday(f.session_date) >= 7
                       GROUP BY p.analyst, f.contract_key
                   )"""
            ).fetchone()[0]
            series_count = conn.execute("SELECT COUNT(*) FROM analyst_backtest_series").fetchone()[0]
            buyside_count = conn.execute(
                "SELECT COUNT(DISTINCT analyst || char(1) || contract_key) FROM analyst_buyside_outcomes"
            ).fetchone()[0]
            plan_signal_count = conn.execute(
                """SELECT COUNT(*) FROM (
                       SELECT p.analyst, f.contract_key
                       FROM parsed_signals p
                       JOIN flow_events f ON p.flow_event_key = f.event_key
                       WHERE p.decision='TRADE' AND p.direction IN ('BULL','BEAR')
                         AND f.session_date IS NOT NULL
                         AND p.underlying_entry IS NOT NULL
                         AND p.underlying_target IS NOT NULL
                         AND p.underlying_stop IS NOT NULL
                       GROUP BY p.analyst, f.contract_key
                   )"""
            ).fetchone()[0]
            plan_count = conn.execute(
                "SELECT COUNT(DISTINCT analyst || char(1) || contract_key) FROM analyst_plan_outcomes"
            ).fetchone()[0]
        sell_pending = max(0, signal_count - series_count)
        buy_pending = max(0, signal_count - buyside_count)
        plan_pending = max(0, plan_signal_count - plan_count)
        return max(sell_pending, buy_pending, plan_pending)

    def _maybe_start_analyst_backtest(self) -> None:
        with self._backtest_lock:
            running = self._analyst_backtest_running
        pending = self._analyst_backtest_pending()
        fresh = time.monotonic() - self._analyst_backtest_at < 3600
        if running or fresh or pending == 0:
            return
        with self._backtest_lock:
            self._analyst_backtest_running = True
        threading.Thread(target=self._analyst_backtest_worker, daemon=True).start()

    def dashboard_backtest(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        payload = _payload if isinstance(_payload, Mapping) else {}
        raw = payload.get("horizon_days")
        try:
            horizon = 0 if raw in (None, "") else int(raw)
        except (TypeError, ValueError):
            horizon = 0
        self._maybe_start_analyst_backtest()
        self._ensure_horizon_settled(horizon)
        return {
            "paper": self.database.paper_stats(),
            "optimization": self._last_optimization,
            "analyst_accuracy": {
                "summary": self.database.analyst_backtest_summary_for_horizon(horizon),
                "daily": self.database.analyst_backtest_daily_summary(horizon),
                "horizon_days": horizon,
                "running": self._analyst_backtest_running,
            },
            "buyside": {
                "summary": self.database.analyst_buyside_summary(),
            },
            "plan": {
                "summary": self.database.analyst_plan_summary(),
            },
        }

    def _ensure_horizon_settled(self, horizon_days: int) -> None:
        """Settle a requested holding period on demand (cached thereafter)."""
        try:
            self.analyst_backtests.settle_horizon(int(horizon_days))
        except Exception:
            pass

    def analyst_backtest_detail(self, analyst: str = "", horizon_days: int = 5, **_kwargs: Any) -> Any:
        horizon = int(horizon_days)
        try:
            self.analyst_backtests.settle_analyst_horizon(str(analyst), horizon)
        except Exception:
            pass
        return {
            "analyst": analyst,
            "horizon_days": horizon,
            "outcomes": self.database.analyst_backtest_outcomes_for_analyst(str(analyst), horizon),
        }

    def audit_sample(self, n: int = 50, seed: int = 42, **_kwargs: Any) -> Any:
        """Random reproducible sample of TRADE signals with the full review chain."""
        return {"seed": seed, "samples": self.database.audit_sample(int(n), int(seed))}

    def audit_review(self, signal_id: int = 0, **_kwargs: Any) -> Any:
        """Ask the AI to judge whether the stored parse and back-test result are consistent with the raw text."""
        signal = self.database.audit_signal(int(signal_id))
        if signal is None:
            return {"status": "error", "message": "信号不存在"}
        if not signal.get("raw_text"):
            return {"status": "error", "message": "原文本缺失"}
        outcome = signal.get("outcome") or {}
        context = {
            "原文本": str(signal["raw_text"])[:900],
            "现有解析": {
                "决策": signal.get("decision"), "方向": signal.get("direction"),
                "合约": signal.get("contract_key"), "分析师": signal.get("analyst"),
            },
            "回测口径": (
                "卖方策略：BULL 卖平值 PUT、BEAR 卖平值 CALL；平值行权价=信号日正股收盘；"
                "收益率按 25% 保证金口径；权利金涨 50% 止损，否则持有到 N 日或到期前 3 天平仓。"
                "因此 ATM 合约的 PUT/CALL 与信号关联的合约（原文本里的 CALL/PUT）相反是正常设计，不是异常。"
                "方向正确率看「信号日收盘 → 持有期末收盘」的涨跌；卖方盈亏看「次日开盘入场 → 平仓」。"
                "两者时间基准不同：正股先跌后涨时，方向最终正确但卖方会在中途暴跌时止损，这是正常的路径依赖，不是数据错误。"
            ),
            "回测结果": {
                "正股涨跌": outcome.get("underlying_change_pct"),
                "方向正确": outcome.get("direction_correct"),
                "卖方盈亏(保证金口径)": outcome.get("strategy_pnl_pct"),
                "退出原因": outcome.get("strategy_exit_reason"),
                "ATM合约": outcome.get("atm_ticker"),
            },
        }
        question = (
            "请判断「现有解析」的决策和方向是否与「原文本」明确陈述一致（重点看 执行观点/结论/decision 行）。"
            "不要因 ATM 合约的 PUT/CALL 与信号合约相反就判异常（那是卖方策略的正常设计）。"
            "回测方面只判断：正股涨跌与方向是否匹配、卖方盈亏与退出原因是否自洽。"
            "注意「方向最终对但卖方中途止损」是正常的先跌后涨路径，不要判为数据错误。"
            "简洁输出：解析是否一致、回测是否合理，各用一句话，指出真正的异常点。"
        )
        result = self.ai.answer(question, context)
        verdict = result.text if not result.ai_degraded else ("AI 不可用：" + str(result.reason))
        return {
            "status": "ok",
            "signal_id": int(signal_id),
            "verdict": verdict,
            "existing": {"decision": signal.get("decision"), "direction": signal.get("direction")},
        }

    def dashboard_contracts(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        return self.dashboard_recommendations({})

    def dashboard_providers(self, _payload: Optional[Mapping[str, Any]] = None) -> Any:
        return self.providers.statuses()

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
            "portfolio_refresh": self.refresh_portfolio,
            "signals": self.dashboard_signals,
            "rules": self.dashboard_rules,
            "analysts": self.dashboard_analysts,
            "dashboard_dates": self.dashboard_dates,
            "backtest": self.dashboard_backtest,
            "backtest_replay": lambda payload: self.replay_backtest(**{k: v for k, v in payload.items() if k in ("start", "end")}),
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
            "gex": lambda payload: self.gex_view(symbol=str(payload.get("symbol", ""))),
            "ibkr_sync": lambda payload: self.sync_ibkr(**payload),
            "collect": lambda payload: self.collect(bool(payload.get("backfill", False))),
            "reevaluate": lambda _payload: self.reevaluate_today(),
            "analyst_backtest_detail": lambda payload: self.analyst_backtest_detail(**payload),
            "audit_sample": lambda payload: self.audit_sample(**payload),
            "audit_review": lambda payload: self.audit_review(**payload),
            "report": lambda _payload: {"queue_id": self.publish_daily()},
            "feishu_test": lambda _payload: self.feishu.test_credentials(),
            "discord_login": lambda _payload: self.source.open_login(),
            "discord_refresh_qr": lambda _payload: self.source.refresh_qr(),
            "deepseek_test": lambda _payload: self.ai.health(check_remote=True),
            "backup": lambda _payload: self.create_backup(),
            "futu_sync": lambda _payload: _as_jsonable(self.sync_broker(force=True)),
            "futu_import_watchlist": lambda _payload: self.import_futu_watchlist(),
        }

    def create_backup(self) -> Dict[str, Any]:
        """Create a consistent SQLite backup in the local data directory."""
        import sqlite3
        backup_dir = self.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        destination = backup_dir / f"options-radar-{datetime.now():%Y%m%d-%H%M%S}.db"
        with sqlite3.connect(str(self.database.path)) as source, sqlite3.connect(str(destination)) as target:
            source.backup(target)
        self._prune_backups(keep=14)
        return {
            "status": "ok", "message": "备份已创建。", "path": str(destination),
            "size": destination.stat().st_size,
        }

    def _prune_backups(self, keep: int = 14) -> None:
        """Keep only the most recent ``keep`` backup files."""
        backup_dir = self.data_dir / "backups"
        if not backup_dir.is_dir():
            return
        files = sorted(backup_dir.glob("options-radar-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[keep:]:
            try:
                stale.unlink()
            except OSError:
                pass

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
        # Poll the Discord flow/analyst channels only during the US cash session
        # (Mon-Fri 09:30-16:00 ET). Outside that window the channels produce no
        # new actionable cards, so polling is paused to keep resources idle.
        cash_cron = dict(day_of_week="mon-fri", hour="9-15", timezone="America/New_York")
        self._scheduler.add_job(
            self.collect, CronTrigger(minute="*/6", **cash_cron),
            id="discord-poll", max_instances=1, coalesce=True,
        )
        self._scheduler.add_job(
            self.publish_top5, CronTrigger(minute="*/5", **cash_cron),
            id="feishu-top5", max_instances=1, coalesce=True,
        )
        self._scheduler.add_job(
            self.backfill, CronTrigger(minute="*/15", **cash_cron),
            id="discord-rescan", max_instances=1, coalesce=True,
        )
        self._scheduler.add_job(self.backfill, CronTrigger(hour=16, minute=30, timezone="America/New_York"), id="discord-close-backfill")
        self._scheduler.add_job(
            self.sync_broker, CronTrigger(minute="*/5", **cash_cron),
            id="ibkr-sync", max_instances=1, coalesce=True,
        )
        self._scheduler.add_job(self.publish_daily, CronTrigger(hour=17, minute=15, timezone="America/New_York"), id="daily-report")
        self._scheduler.add_job(self.run_backtests, CronTrigger(hour=18, minute=0, timezone="America/New_York"), id="daily-backtest")
        self._scheduler.add_job(self.create_backup, CronTrigger(hour=3, minute=0, timezone="Asia/Shanghai"), id="daily-backup")
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

    def health(self, force: bool = False) -> Dict[str, Any]:
        """Aggregated system status with a short cache so the diagnostics page
        and Feishu status commands never block on slow provider probes."""
        with self._health_cache_lock:
            if not force and self._health_cache is not None and time.monotonic() - self._health_cache_at < 30:
                return dict(self._health_cache)
        broker = self.database.latest_broker_snapshot()
        portfolio_section = {
            "source": broker.source if broker else "ibkr", "as_of": broker.as_of.isoformat() if broker else None,
            "nav_present": bool(broker and broker.nav is not None),
            "positions": len(broker.positions) if broker else 0,
        }
        result = {
            "status": "degraded" if self._last_error else ("ok" if self._started else "stopped"),
            "last_collection": self._last_collection, "last_sync": self._last_sync,
            "last_error": self._last_error, "discord": self.source.health(),
            "opend": {"status": "migration_only", "enabled": False},
            "futu": self.providers.status("futu"),
            "ibkr": {
                "provider": "ibkr", "configured": True, "connected": bool(broker),
                "status": "ready" if (broker and broker.nav is not None) else ("synced" if broker else "no_snapshot"),
                "quality": "realtime" if broker else "missing",
                "note": "仅用于持仓同步",
            },
            "ai": self.ai.health(), "feishu": self.feishu.health(),
            "portfolio": portfolio_section,
            "watchlist_count": len(self._watch_symbols()),
            "last_backtest": self._last_backtest, "last_optimization": self._last_optimization,
        }
        with self._health_cache_lock:
            self._health_cache = result
            self._health_cache_at = time.monotonic()
        return result


def create_service(config_path: str, data_dir: str) -> OptionsRadarService:
    return OptionsRadarService(config_path=config_path, data_dir=data_dir)
