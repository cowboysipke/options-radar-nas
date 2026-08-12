from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .models import (
    AnalystVote,
    BrokerSnapshot,
    ConsensusEvaluation,
    FlowEvent,
    ParsedSignal,
    RawMessage,
    SignalOutcome,
    SourceCursor,
    StrategyVersion,
    WatchlistItem,
)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    analyst TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_timestamp TEXT,
    content TEXT NOT NULL,
    screenshot_path TEXT,
    content_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS parsed_signals (
    id INTEGER PRIMARY KEY,
    raw_message_id INTEGER REFERENCES raw_messages(id),
    flow_event_key TEXT NOT NULL,
    contract_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    option_type TEXT NOT NULL,
    decision TEXT NOT NULL,
    direction TEXT NOT NULL,
    direction_source TEXT NOT NULL,
    confidence REAL,
    confidence_raw TEXT,
    analyst_family TEXT NOT NULL,
    analyst TEXT NOT NULL,
    channel TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    rationale_json TEXT NOT NULL,
    underlying_entry REAL,
    underlying_target REAL,
    underlying_stop REAL,
    premium REAL,
    average_price REAL,
    dte INTEGER,
    win_rate REAL,
    risk_score INTEGER,
    risk_notes_json TEXT NOT NULL,
    completeness REAL NOT NULL,
    UNIQUE(raw_message_id, contract_key)
);

CREATE TABLE IF NOT EXISTS flow_events (
    id INTEGER PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    raw_message_id INTEGER REFERENCES raw_messages(id),
    contract_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    option_type TEXT NOT NULL,
    premium REAL NOT NULL,
    average_price REAL,
    dte INTEGER,
    observed_at TEXT NOT NULL,
    session_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS option_snapshots (
    id INTEGER PRIMARY KEY,
    contract_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyst_weights (
    analyst TEXT PRIMARY KEY,
    weight REAL NOT NULL DEFAULT 1.0,
    sample_count INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY,
    contract_key TEXT NOT NULL,
    session_date TEXT,
    evaluated_at TEXT NOT NULL,
    score REAL NOT NULL,
    grade TEXT NOT NULL,
    final_direction TEXT NOT NULL,
    disagreement INTEGER NOT NULL,
    eligible INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    signal_ids_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY,
    recommendation_id INTEGER REFERENCES recommendations(id),
    contract_key TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    entry_price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    max_loss REAL NOT NULL,
    take_profit_price REAL NOT NULL,
    stop_loss_price REAL NOT NULL,
    expiry TEXT NOT NULL,
    status TEXT NOT NULL,
    closed_at TEXT,
    exit_price REAL,
    exit_reason TEXT,
    pnl REAL,
    pnl_pct REAL
);

CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    symbol TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    group_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_cursors (
    channel_id TEXT PRIMARY KEY,
    last_message_id TEXT NOT NULL,
    last_timestamp TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_snapshots (
    id INTEGER PRIMARY KEY,
    as_of TEXT NOT NULL,
    nav REAL,
    cash REAL,
    source TEXT NOT NULL,
    quality TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    id INTEGER PRIMARY KEY,
    recommendation_id INTEGER NOT NULL REFERENCES recommendations(id),
    horizon_days INTEGER NOT NULL,
    status TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    pnl_pct REAL,
    max_favorable REAL,
    max_adverse REAL,
    observed_at TEXT NOT NULL,
    UNIQUE(recommendation_id, horizon_days)
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    version TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    promoted_at TEXT
);

CREATE TABLE IF NOT EXISTS backtest_bars (
    contract_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    complete INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(contract_key, observed_at)
);

CREATE TABLE IF NOT EXISTS notification_queue (
    id INTEGER PRIMARY KEY,
    destination TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS source_rules (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    screenshot_path TEXT,
    content_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_key, content_hash)
);

CREATE TABLE IF NOT EXISTS analyst_profiles (
    analyst TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS terminology_mappings (
    term TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    explanation TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(term, rule_version)
);

CREATE TABLE IF NOT EXISTS execution_rule_versions (
    version TEXT PRIMARY KEY,
    rules_json TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS futu_quote_rights (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    market TEXT NOT NULL,
    security_type TEXT NOT NULL,
    level TEXT NOT NULL,
    is_realtime INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS futu_quota_snapshots (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_data_points (
    id INTEGER PRIMARY KEY,
    instrument_key TEXT NOT NULL,
    field_name TEXT NOT NULL,
    value REAL,
    source TEXT NOT NULL,
    quality TEXT NOT NULL,
    market_timestamp TEXT,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist_snapshots (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    group_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opend_login_states (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    state TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signal_contract_time ON parsed_signals(contract_key, observed_at);
CREATE INDEX IF NOT EXISTS idx_rec_time ON recommendations(evaluated_at);
CREATE INDEX IF NOT EXISTS idx_trade_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_outcome_rec ON signal_outcomes(recommendation_id);
CREATE INDEX IF NOT EXISTS idx_broker_asof ON broker_snapshots(as_of);
CREATE INDEX IF NOT EXISTS idx_notify_due ON notification_queue(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_backtest_bar_time ON backtest_bars(observed_at);
CREATE INDEX IF NOT EXISTS idx_source_rule_active ON source_rules(source_key, active, observed_at);
CREATE INDEX IF NOT EXISTS idx_market_point_key ON market_data_points(instrument_key, received_at);
CREATE INDEX IF NOT EXISTS idx_futu_rights_time ON futu_quote_rights(observed_at);
CREATE INDEX IF NOT EXISTS idx_opend_state_time ON opend_login_states(observed_at);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(recommendations)").fetchall()}
            if "session_date" not in columns:
                connection.execute("ALTER TABLE recommendations ADD COLUMN session_date TEXT")
            # Provider retries can return the same field and exchange timestamp.
            # Keep one canonical point before enforcing idempotent cache writes.
            connection.execute(
                """DELETE FROM market_data_points WHERE id NOT IN (
                    SELECT MAX(id) FROM market_data_points
                    GROUP BY instrument_key, field_name, source, quality, COALESCE(market_timestamp, received_at)
                )"""
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_market_point_identity
                ON market_data_points(
                    instrument_key, field_name, source, quality,
                    COALESCE(market_timestamp, received_at)
                )"""
            )

    @staticmethod
    def message_hash(channel: str, analyst: str, content: str, source_timestamp: Optional[datetime]) -> str:
        normalized = " ".join(content.split())
        stamp = source_timestamp.isoformat() if source_timestamp else ""
        return hashlib.sha256(f"{channel}|{analyst}|{stamp}|{normalized}".encode("utf-8")).hexdigest()

    def insert_raw_message(self, message: RawMessage) -> Tuple[int, bool]:
        digest = message.content_hash or self.message_hash(
            message.channel, message.analyst, message.content, message.source_timestamp
        )
        with self.connect() as connection:
            try:
                cursor = connection.execute(
                    """INSERT INTO raw_messages
                    (channel, analyst, observed_at, source_timestamp, content, screenshot_path, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        message.channel,
                        message.analyst,
                        message.observed_at.isoformat(),
                        message.source_timestamp.isoformat() if message.source_timestamp else None,
                        message.content,
                        message.screenshot_path,
                        digest,
                    ),
                )
                return int(cursor.lastrowid), True
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT id FROM raw_messages WHERE content_hash = ?", (digest,)
                ).fetchone()
                return int(row["id"]), False

    def insert_signal(self, signal: ParsedSignal) -> Tuple[int, bool]:
        with self.connect() as connection:
            try:
                cursor = connection.execute(
                    """INSERT INTO parsed_signals
                    (raw_message_id, flow_event_key, contract_key, symbol, expiry, strike, option_type,
                     decision, direction, direction_source, confidence, confidence_raw, analyst_family,
                     analyst, channel, observed_at, rationale_json, underlying_entry, underlying_target,
                     underlying_stop, premium, average_price, dte, win_rate, risk_score,
                     risk_notes_json, completeness)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        signal.raw_message_id,
                        signal.flow_event_key,
                        signal.contract_key,
                        signal.symbol,
                        signal.expiry.isoformat(),
                        signal.strike,
                        signal.option_type,
                        signal.decision,
                        signal.direction,
                        signal.direction_source,
                        signal.confidence,
                        signal.confidence_raw,
                        signal.analyst_family,
                        signal.analyst,
                        signal.channel,
                        signal.observed_at.isoformat(),
                        json.dumps(signal.rationale, ensure_ascii=False),
                        signal.underlying_entry,
                        signal.underlying_target,
                        signal.underlying_stop,
                        signal.premium,
                        signal.average_price,
                        signal.dte,
                        signal.win_rate,
                        signal.risk_score,
                        json.dumps(signal.risk_notes, ensure_ascii=False),
                        signal.completeness,
                    ),
                )
                return int(cursor.lastrowid), True
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT id FROM parsed_signals WHERE raw_message_id = ? AND contract_key = ?",
                    (signal.raw_message_id, signal.contract_key),
                ).fetchone()
                return int(row["id"]), False

    def insert_flow_event(self, event: FlowEvent) -> Tuple[int, bool]:
        with self.connect() as connection:
            try:
                cursor = connection.execute(
                    """INSERT INTO flow_events
                    (event_key, raw_message_id, contract_key, symbol, expiry, strike, option_type,
                     premium, average_price, dte, observed_at, session_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_key, event.raw_message_id, event.contract_key, event.symbol,
                        event.expiry.isoformat(), event.strike, event.option_type, event.premium,
                        event.average_price, event.dte, event.observed_at.isoformat(),
                        (event.session_date or event.observed_at.date()).isoformat(),
                    ),
                )
                return int(cursor.lastrowid), True
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT id FROM flow_events WHERE event_key = ?", (event.event_key,)
                ).fetchone()
                return int(row["id"]), False

    def flow_events_for_date(self, trade_date: date) -> List[FlowEvent]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM flow_events WHERE session_date = ? ORDER BY observed_at", (trade_date.isoformat(),)
            ).fetchall()
        return [FlowEvent(
            id=int(row["id"]), event_key=str(row["event_key"]), raw_message_id=row["raw_message_id"],
            contract_key=str(row["contract_key"]), symbol=str(row["symbol"]),
            expiry=date.fromisoformat(str(row["expiry"])), strike=float(row["strike"]),
            option_type=str(row["option_type"]), premium=float(row["premium"]),
            average_price=float(row["average_price"]) if row["average_price"] is not None else None,
            dte=int(row["dte"]) if row["dte"] is not None else None,
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
            session_date=date.fromisoformat(str(row["session_date"])),
        ) for row in rows]

    def signals_for_session(self, session_date: date) -> List[ParsedSignal]:
        start = datetime.combine(session_date, datetime.min.time()).replace(hour=12)
        end = start + timedelta(days=1)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM parsed_signals WHERE observed_at >= ? AND observed_at < ? ORDER BY observed_at",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [self._signal_from_row(row) for row in rows]

    def signals_for_date(self, trade_date: date) -> List[ParsedSignal]:
        start = datetime.combine(trade_date, datetime.min.time())
        end = start + timedelta(days=1)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM parsed_signals WHERE observed_at >= ? AND observed_at < ? ORDER BY observed_at",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [self._signal_from_row(row) for row in rows]

    def get_signals(self, contract_key: str, since: datetime) -> List[ParsedSignal]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM parsed_signals WHERE contract_key = ? AND observed_at >= ? ORDER BY observed_at",
                (contract_key, since.isoformat()),
            ).fetchall()
        return [self._signal_from_row(row) for row in rows]

    def signals_for_event(self, event_key: str) -> List[ParsedSignal]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM parsed_signals WHERE flow_event_key = ? ORDER BY observed_at", (event_key,)
            ).fetchall()
        return [self._signal_from_row(row) for row in rows]

    def enrich_signals_for_event(self, event: FlowEvent) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE parsed_signals SET premium=?, average_price=?, dte=?
                WHERE flow_event_key=?""",
                (event.premium, event.average_price, event.dte, event.event_key),
            )

    def get_weights(self, analysts: Iterable[str]) -> Dict[str, float]:
        names = list(dict.fromkeys(analysts))
        if not names:
            return {}
        marks = ",".join("?" for _ in names)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT analyst, weight FROM analyst_weights WHERE analyst IN ({marks})", names
            ).fetchall()
        found = {str(row["analyst"]): float(row["weight"]) for row in rows}
        return {name: found.get(name, 1.0) for name in names}

    def save_weight(self, analyst: str, weight: float, sample_count: int, metrics: Dict[str, float]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO analyst_weights(analyst, weight, sample_count, metrics_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(analyst) DO UPDATE SET weight=excluded.weight,
                sample_count=excluded.sample_count, metrics_json=excluded.metrics_json,
                updated_at=excluded.updated_at""",
                (analyst, weight, sample_count, json.dumps(metrics), datetime.utcnow().isoformat()),
            )

    def save_recommendation(
        self, evaluation: ConsensusEvaluation, signal_ids: Sequence[int], session_date: Optional[date] = None
    ) -> int:
        payload = {
            "contract_key": evaluation.contract_key,
            "evaluated_at": evaluation.evaluated_at.isoformat(),
            "final_direction": evaluation.final_direction,
            "score": evaluation.score,
            "grade": evaluation.grade,
            "disagreement": evaluation.disagreement,
            "consensus_strength": evaluation.consensus_strength,
            "components": evaluation.components,
            "votes": [vote.__dict__ for vote in evaluation.votes],
            "risk_flags": evaluation.risk_flags,
            "market_status": evaluation.market_status,
            "eligible": evaluation.eligible,
            "session_date": session_date.isoformat() if session_date else None,
        }
        signal_ids_text = json.dumps(list(signal_ids))
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM recommendations WHERE contract_key=? AND signal_ids_json=? ORDER BY id DESC LIMIT 1",
                (evaluation.contract_key, signal_ids_text),
            ).fetchone()
            if existing:
                recommendation_id = int(existing["id"])
                connection.execute(
                    """UPDATE recommendations SET session_date=?, evaluated_at=?, score=?, grade=?, final_direction=?,
                    disagreement=?, eligible=?, payload_json=? WHERE id=?""",
                    (session_date.isoformat() if session_date else None,
                     evaluation.evaluated_at.isoformat(), evaluation.score, evaluation.grade,
                     evaluation.final_direction, int(evaluation.disagreement), int(evaluation.eligible),
                     json.dumps(payload, ensure_ascii=False), recommendation_id),
                )
                return recommendation_id
            cursor = connection.execute(
                """INSERT INTO recommendations
                (contract_key, session_date, evaluated_at, score, grade, final_direction, disagreement,
                 eligible, payload_json, signal_ids_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evaluation.contract_key,
                    session_date.isoformat() if session_date else None,
                    evaluation.evaluated_at.isoformat(),
                    evaluation.score,
                    evaluation.grade,
                    evaluation.final_direction,
                    int(evaluation.disagreement),
                    int(evaluation.eligible),
                    json.dumps(payload, ensure_ascii=False),
                    signal_ids_text,
                ),
            )
            return int(cursor.lastrowid)

    def save_option_snapshot(self, contract_key: str, observed_at: datetime, payload: Dict[str, object]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO option_snapshots(contract_key, observed_at, payload_json) VALUES (?, ?, ?)",
                (contract_key, observed_at.isoformat(), json.dumps(payload, ensure_ascii=False)),
            )

    def save_portfolio_snapshot(self, symbol: str, observed_at: datetime, payload: Dict[str, object]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO portfolio_snapshots(observed_at, symbol, payload_json) VALUES (?, ?, ?)",
                (observed_at.isoformat(), symbol, json.dumps(payload, ensure_ascii=False)),
            )

    def open_trade(self, payload: Dict[str, object]) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO paper_trades
                (recommendation_id, contract_key, strategy, direction, opened_at, entry_price,
                 quantity, max_loss, take_profit_price, stop_loss_price, expiry, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
                (
                    payload.get("recommendation_id"), payload["contract_key"], payload["strategy"],
                    payload["direction"], payload["opened_at"], payload["entry_price"],
                    payload["quantity"], payload["max_loss"], payload["take_profit_price"],
                    payload["stop_loss_price"], payload["expiry"],
                ),
            )
            return int(cursor.lastrowid)

    def open_trades(self) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY opened_at").fetchall()
        return [dict(row) for row in rows]

    def close_trade(self, trade_id: int, closed_at: datetime, exit_price: float, reason: str) -> None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
            if row is None or row["status"] != "OPEN":
                return
            entry = float(row["entry_price"])
            quantity = int(row["quantity"])
            pnl = (exit_price - entry) * 100.0 * quantity
            pnl_pct = (exit_price / entry - 1.0) if entry else 0.0
            connection.execute(
                """UPDATE paper_trades SET status='CLOSED', closed_at=?, exit_price=?, exit_reason=?,
                pnl=?, pnl_pct=? WHERE id=?""",
                (closed_at.isoformat(), exit_price, reason, pnl, pnl_pct, trade_id),
            )

    def paper_stats(self) -> Dict[str, float]:
        with self.connect() as connection:
            closed = connection.execute(
                "SELECT pnl, pnl_pct FROM paper_trades WHERE status='CLOSED' ORDER BY closed_at"
            ).fetchall()
            open_count = int(connection.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'"
            ).fetchone()[0])
        pnls = [float(row["pnl"] or 0.0) for row in closed]
        wins = sum(1 for value in pnls if value > 0)
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in pnls:
            equity += value
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity - peak)
        return {
            "closed": float(len(closed)),
            "open": float(open_count),
            "win_rate": wins / len(closed) if closed else 0.0,
            "realized_pnl": sum(pnls),
            "max_drawdown": max_drawdown,
        }

    def recommendations_for_date(self, trade_date: date) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM recommendations WHERE session_date = ? ORDER BY score DESC",
                (trade_date.isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def recommendations_since(self, since: date) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM recommendations WHERE session_date>=?
                ORDER BY session_date, evaluated_at""", (since.isoformat(),)
            ).fetchall()
        return [dict(row) for row in rows]

    def recommendation_by_id(self, recommendation_id: int) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recommendations WHERE id=?", (recommendation_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_backtest_bars(self, contract_key: str, bars: Iterable[Dict[str, object]]) -> int:
        count = 0
        with self.connect() as connection:
            for bar in bars:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO backtest_bars
                    (contract_key, observed_at, open, high, low, close, complete)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (contract_key, str(bar["observed_at"]), float(bar["open"]),
                     float(bar["high"]), float(bar["low"]), float(bar["close"]),
                     int(bool(bar.get("complete", True)))),
                )
                count += cursor.rowcount
        return count

    def backtest_bars(
        self, contract_key: str, start: datetime, end: datetime
    ) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM backtest_bars WHERE contract_key=?
                AND observed_at>=? AND observed_at<=? ORDER BY observed_at""",
                (contract_key, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [dict(row) for row in rows]

    def attach_execution(self, recommendation_id: int, execution: Dict[str, object]) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM recommendations WHERE id = ?", (recommendation_id,)
            ).fetchone()
            if row is None:
                return
            payload = json.loads(str(row["payload_json"]))
            payload["execution"] = execution
            connection.execute(
                "UPDATE recommendations SET payload_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), recommendation_id),
            )

    def analyst_rows(self) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM analyst_weights ORDER BY weight DESC, analyst"
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_recommendation(self, contract_key: str) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recommendations WHERE contract_key = ? ORDER BY evaluated_at DESC LIMIT 1",
                (contract_key,),
            ).fetchone()
        return dict(row) if row else None

    def closed_trade_samples(self, since: datetime) -> List[Dict[str, object]]:
        with self.connect() as connection:
            trades = connection.execute(
                """SELECT t.*, r.signal_ids_json FROM paper_trades t
                JOIN recommendations r ON r.id=t.recommendation_id
                WHERE t.status='CLOSED' AND t.closed_at >= ?""",
                (since.isoformat(),),
            ).fetchall()
            samples: List[Dict[str, object]] = []
            for trade in trades:
                signal_ids = json.loads(str(trade["signal_ids_json"]))
                if not signal_ids:
                    continue
                marks = ",".join("?" for _ in signal_ids)
                signals = connection.execute(
                    f"SELECT analyst, confidence FROM parsed_signals WHERE id IN ({marks})", signal_ids
                ).fetchall()
                for signal in signals:
                    samples.append({
                        "analyst": str(signal["analyst"]),
                        "confidence": float(signal["confidence"]) if signal["confidence"] is not None else 0.5,
                        "pnl_pct": float(trade["pnl_pct"] or 0.0),
                        "closed_at": str(trade["closed_at"]),
                    })
        return samples

    def upsert_watchlist(
        self, symbol: str, source: str = "manual", group_name: str = "默认"
    ) -> WatchlistItem:
        """Create or re-enable a cloud watchlist item."""
        symbol = symbol.strip().upper()
        if not symbol or not all(character.isalnum() or character in {".", "-"} for character in symbol):
            raise ValueError("invalid symbol")
        now = datetime.utcnow()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO watchlist_items
                (symbol, source, group_name, enabled, added_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET source=excluded.source,
                group_name=excluded.group_name, enabled=1, updated_at=excluded.updated_at""",
                (symbol, source, group_name, now.isoformat(), now.isoformat()),
            )
            row = connection.execute(
                "SELECT * FROM watchlist_items WHERE symbol=?", (symbol,)
            ).fetchone()
        return WatchlistItem(
            symbol=str(row["symbol"]), source=str(row["source"]),
            group_name=str(row["group_name"]), enabled=bool(row["enabled"]),
            added_at=datetime.fromisoformat(str(row["added_at"])),
        )

    def remove_watchlist(self, symbol: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE watchlist_items SET enabled=0, updated_at=? WHERE symbol=? AND enabled=1",
                (datetime.utcnow().isoformat(), symbol.strip().upper()),
            )
        return cursor.rowcount > 0

    def list_watchlist(self, enabled_only: bool = True) -> List[WatchlistItem]:
        query = "SELECT * FROM watchlist_items"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY group_name, symbol"
        with self.connect() as connection:
            rows = connection.execute(query).fetchall()
        return [WatchlistItem(
            symbol=str(row["symbol"]), source=str(row["source"]),
            group_name=str(row["group_name"]), enabled=bool(row["enabled"]),
            added_at=datetime.fromisoformat(str(row["added_at"])),
        ) for row in rows]

    def save_source_cursor(self, cursor: SourceCursor) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO source_cursors(channel_id, last_message_id, last_timestamp, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET last_message_id=excluded.last_message_id,
                last_timestamp=excluded.last_timestamp, updated_at=excluded.updated_at""",
                (cursor.channel_id, cursor.last_message_id, cursor.last_timestamp.isoformat(),
                 datetime.utcnow().isoformat()),
            )

    def get_source_cursor(self, channel_id: str) -> Optional[SourceCursor]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_cursors WHERE channel_id=?", (channel_id,)
            ).fetchone()
        if row is None:
            return None
        return SourceCursor(
            channel_id=str(row["channel_id"]),
            last_message_id=str(row["last_message_id"]),
            last_timestamp=datetime.fromisoformat(str(row["last_timestamp"])),
        )

    def save_broker_snapshot(self, snapshot: BrokerSnapshot) -> int:
        payload = {
            "positions": snapshot.positions,
            "source": snapshot.source,
            "quality": snapshot.quality,
        }
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO broker_snapshots(as_of, nav, cash, source, quality, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (snapshot.as_of.isoformat(), snapshot.nav, snapshot.cash, snapshot.source,
                 snapshot.quality, json.dumps(payload, ensure_ascii=False)),
            )
        return int(cursor.lastrowid)

    def latest_broker_snapshot(self) -> Optional[BrokerSnapshot]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM broker_snapshots ORDER BY as_of DESC, id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        return BrokerSnapshot(
            as_of=datetime.fromisoformat(str(row["as_of"])),
            nav=float(row["nav"]) if row["nav"] is not None else None,
            cash=float(row["cash"]) if row["cash"] is not None else None,
            positions=dict(payload.get("positions", {})),
            source=str(row["source"]), quality=str(row["quality"]),
        )

    def save_signal_outcome(self, outcome: SignalOutcome) -> int:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO signal_outcomes
                (recommendation_id, horizon_days, status, entry_price, exit_price, pnl_pct,
                 max_favorable, max_adverse, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recommendation_id, horizon_days) DO UPDATE SET
                status=excluded.status, entry_price=excluded.entry_price,
                exit_price=excluded.exit_price, pnl_pct=excluded.pnl_pct,
                max_favorable=excluded.max_favorable, max_adverse=excluded.max_adverse,
                observed_at=excluded.observed_at""",
                (outcome.recommendation_id, outcome.horizon_days, outcome.status,
                 outcome.entry_price, outcome.exit_price, outcome.pnl_pct,
                 outcome.max_favorable, outcome.max_adverse, outcome.observed_at.isoformat()),
            )
            row = connection.execute(
                "SELECT id FROM signal_outcomes WHERE recommendation_id=? AND horizon_days=?",
                (outcome.recommendation_id, outcome.horizon_days),
            ).fetchone()
        return int(row["id"])

    def signal_outcomes(self, since: Optional[datetime] = None) -> List[SignalOutcome]:
        query = "SELECT * FROM signal_outcomes"
        params: Tuple[object, ...] = ()
        if since is not None:
            query += " WHERE observed_at>=?"
            params = (since.isoformat(),)
        query += " ORDER BY observed_at"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [SignalOutcome(
            recommendation_id=int(row["recommendation_id"]),
            horizon_days=int(row["horizon_days"]), status=str(row["status"]),
            entry_price=float(row["entry_price"]) if row["entry_price"] is not None else None,
            exit_price=float(row["exit_price"]) if row["exit_price"] is not None else None,
            pnl_pct=float(row["pnl_pct"]) if row["pnl_pct"] is not None else None,
            max_favorable=float(row["max_favorable"]) if row["max_favorable"] is not None else None,
            max_adverse=float(row["max_adverse"]) if row["max_adverse"] is not None else None,
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
        ) for row in rows]

    def analyst_family_outcome_counts(self, horizon_days: int = 5) -> Dict[str, int]:
        """Count distinct settled recommendations represented by each analyst family."""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT r.id, r.signal_ids_json FROM signal_outcomes o
                JOIN recommendations r ON r.id=o.recommendation_id
                WHERE o.horizon_days=? AND o.status='filled'""", (horizon_days,)
            ).fetchall()
            families: Dict[str, set] = {}
            for row in rows:
                signal_ids = json.loads(str(row["signal_ids_json"]))
                if not signal_ids:
                    continue
                marks = ",".join("?" for _ in signal_ids)
                signals = connection.execute(
                    f"SELECT DISTINCT analyst_family FROM parsed_signals WHERE id IN ({marks})",
                    signal_ids,
                ).fetchall()
                for signal in signals:
                    families.setdefault(str(signal["analyst_family"]), set()).add(int(row["id"]))
        return {family: len(values) for family, values in families.items()}

    def analyst_outcome_samples(
        self, since: datetime, horizon_days: int = 3
    ) -> List[Dict[str, object]]:
        output: List[Dict[str, object]] = []
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT o.*, r.signal_ids_json FROM signal_outcomes o
                JOIN recommendations r ON r.id=o.recommendation_id
                WHERE o.horizon_days=? AND o.status='filled' AND o.observed_at>=?""",
                (horizon_days, since.isoformat()),
            ).fetchall()
            for row in rows:
                ids = json.loads(str(row["signal_ids_json"]))
                if not ids:
                    continue
                marks = ",".join("?" for _ in ids)
                signals = connection.execute(
                    f"SELECT analyst, confidence FROM parsed_signals WHERE id IN ({marks})", ids
                ).fetchall()
                for signal in signals:
                    output.append({
                        "analyst": str(signal["analyst"]),
                        "confidence": float(signal["confidence"]) if signal["confidence"] is not None else 0.5,
                        "pnl_pct": float(row["pnl_pct"] or 0.0),
                    })
        return output

    def save_strategy_version(self, strategy: StrategyVersion) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO strategy_versions
                (version, status, parameters_json, metrics_json, created_at, promoted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(version) DO UPDATE SET status=excluded.status,
                parameters_json=excluded.parameters_json, metrics_json=excluded.metrics_json,
                promoted_at=excluded.promoted_at""",
                (strategy.version, strategy.status, json.dumps(strategy.parameters),
                 json.dumps(strategy.metrics), strategy.created_at.isoformat(),
                 strategy.promoted_at.isoformat() if strategy.promoted_at else None),
            )

    def strategy_versions(self) -> List[StrategyVersion]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM strategy_versions ORDER BY created_at DESC"
            ).fetchall()
        return [StrategyVersion(
            version=str(row["version"]), status=str(row["status"]),
            parameters=json.loads(str(row["parameters_json"])),
            metrics=json.loads(str(row["metrics_json"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            promoted_at=datetime.fromisoformat(str(row["promoted_at"])) if row["promoted_at"] else None,
        ) for row in rows]

    def enqueue_notification(
        self, destination: str, kind: str, payload: Dict[str, object],
        due_at: Optional[datetime] = None
    ) -> int:
        now = datetime.utcnow()
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO notification_queue
                (destination, kind, payload_json, status, attempts, next_attempt_at, created_at)
                VALUES (?, ?, ?, 'PENDING', 0, ?, ?)""",
                (destination, kind, json.dumps(payload, ensure_ascii=False),
                 (due_at or now).isoformat(), now.isoformat()),
            )
        return int(cursor.lastrowid)

    def due_notifications(self, now: Optional[datetime] = None, limit: int = 20) -> List[Dict[str, object]]:
        now = now or datetime.utcnow()
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM notification_queue
                WHERE status='PENDING' AND next_attempt_at<=?
                ORDER BY next_attempt_at, id LIMIT ?""", (now.isoformat(), limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notification(
        self, notification_id: int, sent: bool, retry_at: Optional[datetime] = None
    ) -> None:
        with self.connect() as connection:
            if sent:
                connection.execute(
                    "UPDATE notification_queue SET status='SENT', sent_at=? WHERE id=?",
                    (datetime.utcnow().isoformat(), notification_id),
                )
            else:
                connection.execute(
                    """UPDATE notification_queue SET attempts=attempts+1,
                    next_attempt_at=?, status=CASE WHEN attempts>=3 THEN 'FAILED' ELSE 'PENDING' END
                    WHERE id=?""", ((retry_at or datetime.utcnow()).isoformat(), notification_id),
                )

    def save_source_rule(
        self,
        source_key: str,
        rule_version: str,
        title: str,
        content: str,
        screenshot_path: Optional[str] = None,
        observed_at: Optional[datetime] = None,
    ) -> int:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = observed_at or datetime.utcnow()
        with self.connect() as connection:
            connection.execute("UPDATE source_rules SET active=0 WHERE source_key=?", (source_key,))
            connection.execute(
                """INSERT OR IGNORE INTO source_rules
                (source_key, rule_version, title, content, screenshot_path, content_hash, observed_at, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                (source_key, rule_version, title, content, screenshot_path, digest, now.isoformat()),
            )
            connection.execute(
                "UPDATE source_rules SET active=1 WHERE source_key=? AND content_hash=?",
                (source_key, digest),
            )
            row = connection.execute(
                "SELECT id FROM source_rules WHERE source_key=? AND content_hash=?",
                (source_key, digest),
            ).fetchone()
        return int(row["id"])

    def source_rules(self, active_only: bool = False) -> List[Dict[str, object]]:
        query = "SELECT * FROM source_rules"
        if active_only:
            query += " WHERE active=1"
        query += " ORDER BY observed_at DESC"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query).fetchall()]

    def save_analyst_profile(
        self, analyst: str, family: str, profile: Dict[str, object], rule_version: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO analyst_profiles(analyst, family, profile_json, rule_version, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(analyst) DO UPDATE SET family=excluded.family,
                profile_json=excluded.profile_json, rule_version=excluded.rule_version,
                updated_at=excluded.updated_at""",
                (analyst, family, json.dumps(profile, ensure_ascii=False), rule_version,
                 datetime.utcnow().isoformat()),
            )

    def analyst_profiles(self) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM analyst_profiles ORDER BY analyst").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["profile"] = json.loads(str(item.pop("profile_json")))
            result.append(item)
        return result

    def save_opend_state(
        self, state: str, message: str = "", payload: Optional[Dict[str, object]] = None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO opend_login_states(observed_at, state, message, payload_json) VALUES (?, ?, ?, ?)",
                (datetime.utcnow().isoformat(), state, message,
                 json.dumps(payload or {}, ensure_ascii=False)),
            )

    def latest_opend_state(self) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM opend_login_states ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(str(result.pop("payload_json")))
        return result

    def save_futu_quota(self, payload: Dict[str, object]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO futu_quota_snapshots(observed_at, payload_json) VALUES (?, ?)",
                (datetime.utcnow().isoformat(), json.dumps(payload, ensure_ascii=False)),
            )

    def save_market_data_points(
        self, instrument_key: str, fields: Dict[str, Dict[str, object]]
    ) -> int:
        received_at = datetime.utcnow().isoformat()
        rows = []
        for field_name, point in fields.items():
            rows.append((
                instrument_key,
                field_name,
                point.get("value"),
                str(point.get("source", "futu")),
                str(point.get("quality", "native")),
                point.get("market_timestamp"),
                received_at,
            ))
        if not rows:
            return 0
        with self.connect() as connection:
            before = connection.total_changes
            connection.executemany(
                """INSERT OR IGNORE INTO market_data_points
                (instrument_key, field_name, value, source, quality, market_timestamp, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            return connection.total_changes - before

    def market_provenance(self, instrument_key: str, limit: int = 250) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT instrument_key, field_name, value, source, quality,
                          market_timestamp, received_at
                   FROM market_data_points WHERE instrument_key=?
                   ORDER BY received_at DESC, id DESC LIMIT ?""",
                (instrument_key, max(1, min(int(limit), 2000))),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _signal_from_row(row: sqlite3.Row) -> ParsedSignal:
        return ParsedSignal(
            id=int(row["id"]),
            raw_message_id=row["raw_message_id"],
            flow_event_key=str(row["flow_event_key"]),
            contract_key=str(row["contract_key"]),
            symbol=str(row["symbol"]),
            expiry=date.fromisoformat(str(row["expiry"])),
            strike=float(row["strike"]),
            option_type=str(row["option_type"]),
            decision=str(row["decision"]),
            direction=str(row["direction"]),
            direction_source=str(row["direction_source"]),
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            confidence_raw=str(row["confidence_raw"]) if row["confidence_raw"] is not None else None,
            analyst_family=str(row["analyst_family"]),
            analyst=str(row["analyst"]),
            channel=str(row["channel"]),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
            rationale=json.loads(str(row["rationale_json"])),
            underlying_entry=float(row["underlying_entry"]) if row["underlying_entry"] is not None else None,
            underlying_target=float(row["underlying_target"]) if row["underlying_target"] is not None else None,
            underlying_stop=float(row["underlying_stop"]) if row["underlying_stop"] is not None else None,
            premium=float(row["premium"]) if row["premium"] is not None else None,
            average_price=float(row["average_price"]) if row["average_price"] is not None else None,
            dte=int(row["dte"]) if row["dte"] is not None else None,
            win_rate=float(row["win_rate"]) if row["win_rate"] is not None else None,
            risk_score=int(row["risk_score"]) if row["risk_score"] is not None else None,
            risk_notes=json.loads(str(row["risk_notes_json"])),
            completeness=float(row["completeness"]),
        )
