from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
import socket
from typing import Dict, List, Optional, Tuple

from .config import AppConfig
from .analytics import update_analyst_weights
from .db import Database
from .desktop import DiscordDesktopCollector
from .futu_client import FutuReadOnlyClient
from .llm import OpenAIResponsesClient
from .models import ConsensusEvaluation, FlowEvent, MarketSnapshot, OptionCandidate, PortfolioContext, RawMessage
from .paper import PaperEngine, build_candidate
from .parser import parse_analyst_message, parse_flow_message
from .reports import evaluation_markdown
from .scoring import evaluate_consensus
from .timeutil import us_session_date_from_china_time


@dataclass
class ProcessedRecommendation:
    recommendation_id: int
    evaluation: ConsensusEvaluation
    candidate: OptionCandidate
    paper_trade_id: Optional[int]


class RadarPipeline:
    def __init__(self, config: AppConfig):
        self.config = config
        self.database = Database(config.database_path)
        llm = config.section("llm")
        self.llm = OpenAIResponsesClient(
            api_key=str(llm.get("api_key", "")),
            model=str(llm.get("model", "gpt-5-mini")),
            base_url=str(llm.get("base_url", "https://api.openai.com/v1")),
            timeout=int(llm.get("timeout_seconds", 90)),
        )
        desktop = config.section("desktop")
        desktop["source_server"] = config.section("discord").get("source_server", "Alpha夜航社")
        self.collector = DiscordDesktopCollector(
            desktop,
            config.evidence_dir,
            self.llm if llm.get("use_vision_ocr", True) and self.llm.enabled else None,
        )
        futu = config.section("futu")
        self.futu_enabled = bool(futu.get("enabled", True))
        self._futu_run_available = self.futu_enabled
        self.futu = FutuReadOnlyClient(
            host=str(futu.get("host", "127.0.0.1")),
            port=int(futu.get("port", 11111)),
            security_firm=str(futu.get("security_firm", "FUTUINC")),
        )
        self.paper = PaperEngine(self.database, config.section("paper"))
        self._portfolio_cache: Dict[str, PortfolioContext] = {}

    def collect_today(self, target_date: Optional[date] = None) -> List[ProcessedRecommendation]:
        target_date = target_date or us_session_date_from_china_time(datetime.now())
        channels = self.config.section("discord").get("source_channels", {})
        messages = self.collector.collect_today(dict(channels), target_date)
        return self.process_messages(messages, target_date)

    def ingest_raw(self, messages: List[RawMessage]) -> Tuple[List[FlowEvent], int]:
        events: List[FlowEvent] = []
        signal_count = 0
        refine = self.llm if self.llm.enabled and self.config.section("llm").get("use_text_refinement", True) else None
        for message in messages:
            raw_id, _ = self.database.insert_raw_message(message)
            message.id = raw_id
            if message.analyst == "flow" or message.channel == "异常期权":
                event = parse_flow_message(message)
                if event:
                    event.raw_message_id = raw_id
                    event_id, _ = self.database.insert_flow_event(event)
                    event.id = event_id
                    events.append(event)
                continue
            signal = parse_analyst_message(message, refiner=refine)
            if signal:
                signal.raw_message_id = raw_id
                signal_id, created = self.database.insert_signal(signal)
                signal.id = signal_id
                signal_count += int(created)
        return events, signal_count

    def sync_portfolio(self) -> Dict[str, PortfolioContext]:
        self._futu_run_available = self.futu_enabled
        if not self.futu_enabled:
            self._portfolio_cache = {}
            return self._portfolio_cache
        try:
            with socket.create_connection((self.futu.host, self.futu.port), timeout=1.0):
                pass
        except OSError:
            self._futu_run_available = False
            self._portfolio_cache = {}
            return self._portfolio_cache
        try:
            contexts, raw_records = self.futu.sync_portfolio()
            now = datetime.utcnow()
            for symbol, context in contexts.items():
                self.database.save_portfolio_snapshot(symbol, now, asdict(context))
            self._portfolio_cache = contexts
        except Exception:
            self._portfolio_cache = {}
            self._futu_run_available = False
        return self._portfolio_cache

    def _market(self, contract_key: str) -> MarketSnapshot:
        if not self._futu_run_available:
            status = "futu_disabled" if not self.futu_enabled else "futu_offline"
            return MarketSnapshot(contract_key=contract_key, observed_at=datetime.utcnow(), data_status=status)
        try:
            snapshot = self.futu.exact_snapshot(contract_key)
        except Exception as exc:
            snapshot = MarketSnapshot(
                contract_key=contract_key,
                observed_at=datetime.utcnow(),
                data_status=f"futu_error:{type(exc).__name__}",
            )
        self.database.save_option_snapshot(contract_key, snapshot.observed_at, asdict(snapshot))
        return snapshot

    def _choose_execution_market(
        self,
        original: MarketSnapshot,
        evaluation: ConsensusEvaluation,
        scoring: Dict[str, object],
    ) -> MarketSnapshot:
        try:
            _, expiry_text, _, _ = original.contract_key.split("|")
            dte = (date.fromisoformat(expiry_text) - date.today()).days
        except Exception:
            dte = -1
        delta = abs(original.delta) if original.delta is not None else None
        exact_ok = bool(
            original.data_status == "ok"
            and int(scoring.get("min_dte", 14)) <= dte <= int(scoring.get("max_dte", 90))
            and (original.spread_pct is None or original.spread_pct <= float(scoring.get("max_spread_pct", 0.12)))
            and (original.open_interest is None or original.open_interest >= float(scoring.get("min_open_interest", 100)))
            and (delta is None or float(scoring.get("min_abs_delta", 0.30)) <= delta <= float(scoring.get("max_abs_delta", 0.65)))
        )
        if exact_ok or not self._futu_run_available:
            return original
        symbol = original.contract_key.split("|", 1)[0].split(".", 1)[1]
        try:
            choices = self.futu.execution_candidates(
                symbol,
                evaluation.final_direction,
                int(scoring.get("min_dte", 14)),
                int(scoring.get("max_dte", 90)),
            )
        except Exception:
            return original
        qualified = []
        for item in choices:
            if item.spread_pct is not None and item.spread_pct > float(scoring.get("max_spread_pct", 0.12)):
                continue
            if item.open_interest is not None and item.open_interest < float(scoring.get("min_open_interest", 100)):
                continue
            if item.delta is not None and not (
                float(scoring.get("min_abs_delta", 0.30))
                <= abs(item.delta)
                <= float(scoring.get("max_abs_delta", 0.65))
            ):
                continue
            rank = (
                abs(abs(item.delta or 0.45) - 0.45),
                item.spread_pct if item.spread_pct is not None else 1.0,
                -(item.open_interest or 0.0),
            )
            qualified.append((rank, item))
        return min(qualified, key=lambda pair: pair[0])[1] if qualified else original

    def process_messages(self, messages: List[RawMessage], target_date: Optional[date] = None) -> List[ProcessedRecommendation]:
        target_date = target_date or us_session_date_from_china_time(datetime.now())
        self.ingest_raw(messages)
        events = self.database.flow_events_for_date(target_date)

        # Analyst channels sometimes arrive without the raw feed page in the same capture.
        existing_keys = {event.event_key for event in events}
        for signal in self.database.signals_for_session(target_date):
            if signal.flow_event_key in existing_keys:
                continue
            synthetic = FlowEvent(
                event_key=signal.flow_event_key,
                contract_key=signal.contract_key,
                symbol=signal.symbol,
                expiry=signal.expiry,
                strike=signal.strike,
                option_type=signal.option_type,
                premium=signal.premium or 0.0,
                average_price=signal.average_price,
                dte=signal.dte,
                observed_at=signal.observed_at,
                session_date=target_date,
            )
            event_id, _ = self.database.insert_flow_event(synthetic)
            synthetic.id = event_id
            events.append(synthetic)
            existing_keys.add(signal.flow_event_key)

        contexts = self.sync_portfolio()
        scoring = self.config.section("scoring")
        output: List[ProcessedRecommendation] = []
        for event in events:
            self.database.enrich_signals_for_event(event)
            signals = self.database.signals_for_event(event.event_key)
            if not signals:
                continue
            # Raw flow is the source of truth for DTE and option average price.
            weights = self.database.get_weights(signal.analyst for signal in signals)
            market = self._market(event.contract_key)
            portfolio = contexts.get(event.symbol, PortfolioContext(symbol=event.symbol))
            portfolio.open_paper_positions = sum(
                1 for trade in self.database.open_trades() if str(trade["contract_key"]).startswith(f"US.{event.symbol}|")
            )
            evaluation = evaluate_consensus(
                signals,
                analyst_weights=weights,
                market=market,
                portfolio=portfolio,
                recommendation_threshold=float(scoring.get("recommendation_threshold", 65)),
                disagreement_threshold=float(scoring.get("disagreement_threshold", 0.25)),
            )
            signal_ids = [int(signal.id) for signal in signals if signal.id is not None]
            recommendation_id = self.database.save_recommendation(
                evaluation, signal_ids, event.session_date or target_date
            )
            execution_market = self._choose_execution_market(market, evaluation, scoring)
            candidate = build_candidate(evaluation, execution_market, portfolio, self.config.section("paper"))
            self.database.attach_execution(recommendation_id, {
                "strategy": candidate.strategy,
                "contract_key": candidate.market.contract_key,
                "entry_debit": candidate.entry_debit,
                "quantity": candidate.quantity,
                "take_profit": candidate.take_profit,
                "stop_loss": candidate.stop_loss,
                "market_status": candidate.market.data_status,
            })
            execution_expiry = date.fromisoformat(candidate.market.contract_key.split("|")[1])
            trade_id = self.paper.maybe_open(candidate, recommendation_id, execution_expiry)
            output.append(ProcessedRecommendation(recommendation_id, evaluation, candidate, trade_id))
        self.mark_open_trades()
        update_analyst_weights(
            self.database, int(self.config.section("scoring").get("weight_lookback_days", 60))
        )
        return sorted(output, key=lambda item: item.evaluation.score, reverse=True)

    def mark_open_trades(self) -> None:
        if not self._futu_run_available:
            return
        prices: Dict[str, float] = {}
        for trade in self.database.open_trades():
            key = str(trade["contract_key"])
            snapshot = self._market(key)
            if snapshot.midpoint is not None:
                prices[key] = snapshot.midpoint
        self.paper.mark(prices)

    @staticmethod
    def render_results(results: List[ProcessedRecommendation]) -> str:
        return "\n\n".join(evaluation_markdown(item.evaluation, item.candidate) for item in results)
