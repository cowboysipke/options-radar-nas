"""Reliable Feishu (Lark) command transport for the options radar.

This module deliberately keeps the Feishu SDK at the edge of the program.  It
can therefore be imported, configured and tested on machines where
``lark-oapi`` is not installed.  Incoming webhook/websocket callbacks only
persist an inbox row; user callbacks and network I/O run in the worker.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


RETRY_DELAYS_SECONDS: Tuple[int, ...] = (60, 300, 900)
OUTBOX_UUID_NAMESPACE = uuid.UUID("9a267119-fd52-4a51-912a-689253ec1adc")

COMMAND_TODAY = "今日推荐"
COMMAND_POSITIONS = "查看持仓"
COMMAND_ADD_WATCHLIST = "添加自选"
COMMAND_REMOVE_WATCHLIST = "删除自选"
COMMAND_EXPLAIN = "为什么推荐第N名"
COMMAND_SYNC_FUTU = "重新同步富途"
COMMAND_SYSTEM_STATUS = "系统状态"
COMMAND_BIND = "绑定"

COMMAND_HELP = "\n".join(
    (
        "可用命令：",
        "- 今日推荐",
        "- 查看持仓",
        "- 添加自选 SYMBOL",
        "- 删除自选 SYMBOL",
        "- 为什么推荐第N名（例如：为什么推荐第2名）",
        "- 重新同步富途",
        "- 系统状态",
        "- 其他文字将作为自由问答",
    )
)


class FeishuConfigurationError(RuntimeError):
    """Raised when environment/Docker-secret configuration is incomplete."""


class FeishuDeliveryError(RuntimeError):
    """Raised when Feishu rejects an outgoing message."""


@dataclass(frozen=True)
class FeishuCredentials:
    app_id: str
    app_secret: str = field(repr=False)

    def __repr__(self) -> str:
        return "FeishuCredentials(app_id={!r}, app_secret='***')".format(self.app_id)


def probe_feishu_credentials(
    credentials: Optional[FeishuCredentials] = None, timeout: float = 10.0
) -> Dict[str, Any]:
    """Read-only Feishu credential probe.

    The endpoint only exchanges app credentials for a short-lived tenant token;
    the token is intentionally discarded.  Responses contain no secret or
    token, making this safe to return from a diagnostics page.
    """

    try:
        value = credentials or load_feishu_credentials()
    except FeishuConfigurationError as exc:
        return {"provider": "feishu", "status": "not_configured", "message": str(exc)}
    payload = json.dumps({"app_id": value.app_id, "app_secret": value.app_secret}).encode("utf-8")
    request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "options-radar/0.2"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        code = int(body.get("code", -1)) if isinstance(body, Mapping) else -1
        if code != 0:
            return {"provider": "feishu", "status": "error", "code": code, "message": str(body.get("msg", "api_error"))[:160]}
        return {
            "provider": "feishu", "status": "ok", "code": 0,
            "expires_in": int(body.get("expire", 0) or 0),
            "app_id": value.app_id,
        }
    except urllib.error.HTTPError as exc:
        return {"provider": "feishu", "status": "error", "code": int(exc.code), "message": "http_error"}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"provider": "feishu", "status": "error", "message": "network_error"}
    except (ValueError, UnicodeError):
        return {"provider": "feishu", "status": "error", "message": "invalid_response"}


def _read_docker_secret(secret_dir: Path, names: Sequence[str]) -> str:
    for name in names:
        path = secret_dir / name
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            continue
        if value:
            return value
    return ""


def load_feishu_credentials(
    environ: Optional[Mapping[str, str]] = None,
    secret_dir: Optional[Path] = None,
) -> FeishuCredentials:
    """Load credentials only from the process environment or Docker secrets.

    Environment variables take precedence.  ``secret_dir`` exists to make the
    standard ``/run/secrets`` mechanism testable; it is not an application
    configuration source.
    """

    env = os.environ if environ is None else environ
    directory = Path("/run/secrets") if secret_dir is None else Path(secret_dir)
    app_id = str(env.get("FEISHU_APP_ID", "") or env.get("LARK_APP_ID", "")).strip()
    app_secret = str(env.get("FEISHU_APP_SECRET", "") or env.get("LARK_APP_SECRET", "")).strip()
    app_id_file = str(env.get("FEISHU_APP_ID_FILE", "")).strip()
    app_secret_file = str(env.get("FEISHU_APP_SECRET_FILE", "")).strip()
    if not app_id and app_id_file and Path(app_id_file).is_file():
        app_id = Path(app_id_file).read_text(encoding="utf-8").strip()
    if not app_secret and app_secret_file and Path(app_secret_file).is_file():
        app_secret = Path(app_secret_file).read_text(encoding="utf-8").strip()
    if not app_id:
        app_id = _read_docker_secret(directory, ("feishu_app_id", "FEISHU_APP_ID", "lark_app_id"))
    if not app_secret:
        app_secret = _read_docker_secret(
            directory,
            ("feishu_app_secret", "FEISHU_APP_SECRET", "lark_app_secret"),
        )
    missing = []
    if not app_id:
        missing.append("FEISHU_APP_ID")
    if not app_secret:
        missing.append("FEISHU_APP_SECRET")
    if missing:
        raise FeishuConfigurationError("缺少飞书凭据：" + "、".join(missing))
    return FeishuCredentials(app_id=app_id, app_secret=app_secret)


def load_feishu_webhook(
    environ: Optional[Mapping[str, str]] = None,
    secret_dir: Optional[Path] = None,
) -> Optional[str]:
    """Load an optional group-bot webhook URL.

    The webhook is the low-friction notification path: it needs no app
    credentials, no event subscription and no binding.  Delivery goes to the
    group where the bot was added.
    """
    env = os.environ if environ is None else environ
    directory = Path("/run/secrets") if secret_dir is None else Path(secret_dir)
    url = str(env.get("FEISHU_WEBHOOK_URL", "") or env.get("LARK_WEBHOOK_URL", "")).strip()
    url_file = str(env.get("FEISHU_WEBHOOK_URL_FILE", "")).strip()
    if not url and url_file and Path(url_file).is_file():
        url = Path(url_file).read_text(encoding="utf-8").strip()
    if not url:
        url = _read_docker_secret(directory, ("feishu_webhook", "FEISHU_WEBHOOK_URL", "lark_webhook"))
    if url and not url.startswith(("https://", "http://")):
        return None
    return url or None


@dataclass
class FeishuCallbacks:
    """Business operations injected into the transport layer."""

    today_recommendations: Callable[[], Any]
    positions: Callable[[], Any]
    add_watchlist: Callable[[str], Any]
    remove_watchlist: Callable[[str], Any]
    explain_rank: Callable[[int], Any]
    free_chat: Callable[[str], Any]
    sync_futu: Optional[Callable[[], Any]] = None
    system_status: Optional[Callable[[], Any]] = None


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    argument: Any = None


_ADD_RE = re.compile(r"^添加自选\s+([A-Za-z0-9][A-Za-z0-9._-]{0,31})$")
_REMOVE_RE = re.compile(r"^删除自选\s+([A-Za-z0-9][A-Za-z0-9._-]{0,31})$")
_EXPLAIN_RE = re.compile(r"^(?:为什么推荐|解释)第\s*([1-9]\d*)\s*名$")
_MENTION_RE = re.compile(r"(?:@_user_\d+|<at[^>]*>.*?</at>)\s*", re.IGNORECASE)


def parse_command(text: str) -> ParsedCommand:
    """Parse the stable Chinese command vocabulary.

    Text outside the vocabulary is intentionally passed through to free-form
    Q&A instead of guessing English aliases.
    """

    normalized = _MENTION_RE.sub("", str(text or "")).strip()
    if normalized in {COMMAND_BIND, "bind"}:
        return ParsedCommand("bind")
    if normalized == COMMAND_TODAY:
        return ParsedCommand("today")
    if normalized in {COMMAND_POSITIONS, "持仓"}:
        return ParsedCommand("positions")
    match = _ADD_RE.fullmatch(normalized)
    if match:
        return ParsedCommand("add_watchlist", match.group(1).upper())
    match = _REMOVE_RE.fullmatch(normalized)
    if match:
        return ParsedCommand("remove_watchlist", match.group(1).upper())
    match = _EXPLAIN_RE.fullmatch(normalized)
    if match:
        return ParsedCommand("explain_rank", int(match.group(1)))
    if normalized in {"帮助", "命令"}:
        return ParsedCommand("help")
    if normalized == COMMAND_SYNC_FUTU:
        return ParsedCommand("sync_futu")
    if normalized == COMMAND_SYSTEM_STATUS:
        return ParsedCommand("system_status")
    return ParsedCommand("free_chat", normalized)


def build_card(
    title: str,
    markdown: str,
    template: str = "blue",
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a Feishu interactive card using SDK-independent dictionaries."""

    elements: List[Dict[str, Any]] = [{"tag": "markdown", "content": str(markdown or "-")}]
    if note:
        elements.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": str(note)}],
            }
        )
    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": str(title)},
        },
        "elements": elements,
    }


def _mapping_lines(item: Mapping[str, Any]) -> str:
    preferred = (
        "symbol",
        "contract_key",
        "grade",
        "score",
        "direction",
        "final_direction",
        "quantity",
        "market_value",
        "pnl",
        "reason",
    )
    labels = {
        "symbol": "标的",
        "contract_key": "合约",
        "grade": "等级",
        "score": "评分",
        "direction": "方向",
        "final_direction": "方向",
        "quantity": "数量",
        "market_value": "市值",
        "pnl": "盈亏",
        "reason": "说明",
    }
    parts = []
    used = set()
    for key in preferred:
        if key in item and item[key] is not None:
            parts.append("**{}** {}".format(labels[key], item[key]))
            used.add(key)
    for key, value in item.items():
        if key not in used and value is not None and not isinstance(value, (dict, list, tuple)):
            parts.append("**{}** {}".format(key, value))
    return " · ".join(parts) or "-"


def _render_value(value: Any, empty: str = "暂无数据") -> str:
    if value is None:
        return empty
    if isinstance(value, str):
        return value or empty
    if isinstance(value, Mapping):
        return _mapping_lines(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if not value:
            return empty
        lines = []
        for index, item in enumerate(value, 1):
            if isinstance(item, Mapping):
                body = _mapping_lines(item)
            else:
                body = str(item)
            lines.append("**{}.** {}".format(index, body))
        return "\n".join(lines)
    return str(value)


def build_recommendations_card(recommendations: Any) -> Dict[str, Any]:
    return build_card("今日推荐", _render_value(recommendations, "今日暂无推荐"), "blue")


def build_positions_card(positions: Any) -> Dict[str, Any]:
    return build_card("当前持仓", _render_value(positions, "当前暂无持仓"), "turquoise")


def build_watchlist_card(symbol: str, added: bool, result: Any = None) -> Dict[str, Any]:
    action = "添加" if added else "删除"
    detail = _render_value(result, "已完成")
    status = str(result.get("status", "ready")) if isinstance(result, dict) else "ready"
    completed = status in {"ready", "unchanged", "ok"}
    title = "自选{}完成".format(action) if completed else "自选{}待处理".format(action)
    return build_card(title, "**{}**\n{}".format(symbol, detail), "green" if completed else "orange")


def build_explanation_card(rank: int, explanation: Any) -> Dict[str, Any]:
    return build_card("第{}名解释".format(rank), _render_value(explanation), "purple")


def build_answer_card(answer: Any) -> Dict[str, Any]:
    return build_card("雷达问答", _render_value(answer), "blue")


def build_error_card(message: str = "请求处理失败，请稍后重试") -> Dict[str, Any]:
    return build_card("处理提示", message, "red")


def _is_card(value: Any) -> bool:
    return isinstance(value, Mapping) and "header" in value and "elements" in value


@dataclass(frozen=True)
class InboxMessage:
    message_id: str
    chat_id: str
    sender_id: str
    text: str
    payload: Dict[str, Any]


@dataclass(frozen=True)
class OutboxMessage:
    id: int
    message_uuid: str
    source_message_id: str
    receive_id: str
    receive_id_type: str
    msg_type: str
    content: str
    attempts: int
    next_attempt_at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS feishu_inbox (
    message_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    sender_id TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    received_at REAL NOT NULL,
    processed_at REAL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_feishu_inbox_status
    ON feishu_inbox(status, received_at);

CREATE TABLE IF NOT EXISTS feishu_outbox (
    id INTEGER PRIMARY KEY,
    message_uuid TEXT NOT NULL UNIQUE,
    source_message_id TEXT NOT NULL,
    receive_id TEXT NOT NULL,
    receive_id_type TEXT NOT NULL DEFAULT 'chat_id',
    msg_type TEXT NOT NULL DEFAULT 'interactive',
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    created_at REAL NOT NULL,
    sent_at REAL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_feishu_outbox_due
    ON feishu_outbox(status, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS feishu_runtime (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class FeishuStore:
    """Thread-safe SQLite inbox, deduplication ledger and persistent outbox."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self.clock = clock
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA busy_timeout=5000")
            if str(self.path) != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(_SCHEMA)
            # A process may stop between claim and completion.  Requeue both
            # durable work types on the next start.
            self._connection.execute("UPDATE feishu_inbox SET status='PENDING' WHERE status='PROCESSING'")
            self._connection.execute("UPDATE feishu_outbox SET status='PENDING' WHERE status='SENDING'")
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def latest_chat_id(self) -> Optional[str]:
        with self._lock:
            row = self._connection.execute(
                "SELECT chat_id FROM feishu_inbox ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
        return str(row["chat_id"]) if row else None

    def bind_sender(self, sender_id: str, chat_id: str) -> None:
        """Persist the Feishu user/chat used for private reports.

        Binding is deliberately stored in the existing runtime key/value table
        so no migration is needed for databases created by earlier versions.
        Only opaque Feishu IDs are retained; credentials never enter SQLite.
        """

        with self._lock:
            self._connection.executemany(
                "INSERT OR REPLACE INTO feishu_runtime(key, value) VALUES (?, ?)",
                (("bound_sender_id", str(sender_id or "")), ("bound_chat_id", str(chat_id or ""))),
            )
            self._connection.commit()

    def bound_sender_id(self) -> Optional[str]:
        return self.runtime_value("bound_sender_id")

    def bound_chat_id(self) -> Optional[str]:
        return self.runtime_value("bound_chat_id")

    def enqueue_inbox(
        self,
        message_id: str,
        chat_id: str,
        text: str,
        sender_id: str = "",
        payload: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Persist an event and return ``False`` for a duplicate message ID."""

        now = float(self.clock())
        encoded = json.dumps(dict(payload or {}), ensure_ascii=False, default=str)
        with self._lock:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO feishu_inbox
                   (message_id, chat_id, sender_id, text, payload_json, received_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(message_id), str(chat_id), str(sender_id), str(text), encoded, now),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO feishu_runtime(key, value) VALUES ('last_event_at', ?)",
                (str(now),),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    # Friendly alias for event adapters/tests.
    enqueue_event = enqueue_inbox

    def claim_inbox(self) -> Optional[InboxMessage]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM feishu_inbox WHERE status='PENDING' ORDER BY received_at, message_id LIMIT 1"
            ).fetchone()
            if row is None:
                self._connection.commit()
                return None
            changed = self._connection.execute(
                "UPDATE feishu_inbox SET status='PROCESSING', attempts=attempts+1 "
                "WHERE message_id=? AND status='PENDING'",
                (row["message_id"],),
            ).rowcount
            self._connection.commit()
            if changed != 1:
                return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError):
            payload = {}
        return InboxMessage(
            message_id=str(row["message_id"]),
            chat_id=str(row["chat_id"]),
            sender_id=str(row["sender_id"]),
            text=str(row["text"]),
            payload=payload,
        )

    @staticmethod
    def stable_uuid(source_message_id: str, ordinal: int = 0) -> str:
        return str(uuid.uuid5(OUTBOX_UUID_NAMESPACE, "{}:{}".format(source_message_id, ordinal)))

    def complete_inbox(self, message: InboxMessage, card: Mapping[str, Any], ordinal: int = 0) -> str:
        """Atomically complete an inbox item and create its durable response."""

        now = float(self.clock())
        message_uuid = self.stable_uuid(message.message_id, ordinal)
        content = json.dumps(dict(card), ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """INSERT OR IGNORE INTO feishu_outbox
                   (message_uuid, source_message_id, receive_id, content, next_attempt_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (message_uuid, message.message_id, message.chat_id, content, now, now),
            )
            self._connection.execute(
                "UPDATE feishu_inbox SET status='DONE', processed_at=?, last_error=NULL WHERE message_id=?",
                (now, message.message_id),
            )
            self._connection.commit()
        return message_uuid

    def fail_inbox(self, message_id: str, error: BaseException) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE feishu_inbox SET status='FAILED', processed_at=?, last_error=? WHERE message_id=?",
                (float(self.clock()), "{}: {}".format(type(error).__name__, error)[:1000], message_id),
            )
            self._connection.commit()

    def enqueue_outbox(
        self,
        source_message_id: str,
        receive_id: str,
        card: Mapping[str, Any],
        ordinal: int = 0,
    ) -> str:
        now = float(self.clock())
        message_uuid = self.stable_uuid(source_message_id, ordinal)
        content = json.dumps(dict(card), ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock:
            self._connection.execute(
                """INSERT OR IGNORE INTO feishu_outbox
                   (message_uuid, source_message_id, receive_id, content, next_attempt_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (message_uuid, source_message_id, receive_id, content, now, now),
            )
            self._connection.commit()
        return message_uuid

    def claim_outbox(self, now: Optional[float] = None) -> Optional[OutboxMessage]:
        current = float(self.clock() if now is None else now)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                """SELECT * FROM feishu_outbox
                   WHERE status='PENDING' AND next_attempt_at<=?
                   ORDER BY next_attempt_at, id LIMIT 1""",
                (current,),
            ).fetchone()
            if row is None:
                self._connection.commit()
                return None
            changed = self._connection.execute(
                "UPDATE feishu_outbox SET status='SENDING' WHERE id=? AND status='PENDING'",
                (row["id"],),
            ).rowcount
            self._connection.commit()
            if changed != 1:
                return None
        return OutboxMessage(
            id=int(row["id"]),
            message_uuid=str(row["message_uuid"]),
            source_message_id=str(row["source_message_id"]),
            receive_id=str(row["receive_id"]),
            receive_id_type=str(row["receive_id_type"]),
            msg_type=str(row["msg_type"]),
            content=str(row["content"]),
            attempts=int(row["attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
        )

    def mark_sent(self, item: OutboxMessage) -> None:
        now = float(self.clock())
        with self._lock:
            self._connection.execute(
                """UPDATE feishu_outbox SET status='SENT', attempts=attempts+1,
                   sent_at=?, last_error=NULL WHERE id=?""",
                (now, item.id),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO feishu_runtime(key, value) VALUES ('last_sent_at', ?)",
                (str(now),),
            )
            self._connection.commit()

    def mark_delivery_failed(self, item: OutboxMessage, error: BaseException) -> Optional[int]:
        """Schedule 1/5/15 minute retries; return delay or ``None`` when dead."""

        failure_number = item.attempts + 1
        now = float(self.clock())
        description = "{}: {}".format(type(error).__name__, error)[:1000]
        with self._lock:
            if failure_number <= len(RETRY_DELAYS_SECONDS):
                delay = RETRY_DELAYS_SECONDS[failure_number - 1]
                self._connection.execute(
                    """UPDATE feishu_outbox SET status='PENDING', attempts=?,
                       next_attempt_at=?, last_error=? WHERE id=?""",
                    (failure_number, now + delay, description, item.id),
                )
            else:
                delay = None
                self._connection.execute(
                    """UPDATE feishu_outbox SET status='DEAD', attempts=?,
                       last_error=? WHERE id=?""",
                    (failure_number, description, item.id),
                )
            self._connection.commit()
        return delay

    def counts(self) -> Dict[str, int]:
        with self._lock:
            inbox = self._connection.execute(
                "SELECT status, COUNT(*) AS n FROM feishu_inbox GROUP BY status"
            ).fetchall()
            outbox = self._connection.execute(
                "SELECT status, COUNT(*) AS n FROM feishu_outbox GROUP BY status"
            ).fetchall()
        result: Dict[str, int] = {}
        for row in inbox:
            result["inbox_{}".format(str(row["status"]).lower())] = int(row["n"])
        for row in outbox:
            result["outbox_{}".format(str(row["status"]).lower())] = int(row["n"])
        for key in ("inbox_pending", "inbox_processing", "inbox_failed", "outbox_pending", "outbox_sending", "outbox_dead"):
            result.setdefault(key, 0)
        return result

    def runtime_value(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM feishu_runtime WHERE key=?", (key,)
            ).fetchone()
        return None if row is None else str(row["value"])

    def outbox_row(self, message_uuid: str) -> Optional[Dict[str, Any]]:
        """Small observability helper useful for support tools and tests."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM feishu_outbox WHERE message_uuid=?", (message_uuid,)
            ).fetchone()
        return None if row is None else dict(row)


class LarkMessageSender:
    """Thin adapter around lark-oapi, imported only when instantiated."""

    def __init__(self, credentials: Optional[FeishuCredentials] = None):
        self.credentials = credentials or load_feishu_credentials()
        self._lark = importlib.import_module("lark_oapi")
        self._im = importlib.import_module("lark_oapi.api.im.v1")
        builder = self._lark.Client.builder().app_id(self.credentials.app_id).app_secret(
            self.credentials.app_secret
        )
        self._client = builder.build()

    def __call__(self, item: OutboxMessage) -> None:
        body_builder = (
            self._im.CreateMessageRequestBody.builder()
            .receive_id(item.receive_id)
            .msg_type(item.msg_type)
            .content(item.content)
            .uuid(item.message_uuid)
        )
        request = (
            self._im.CreateMessageRequest.builder()
            .receive_id_type(item.receive_id_type)
            .request_body(body_builder.build())
            .build()
        )
        response = self._client.im.v1.message.create(request)
        success = getattr(response, "success", None)
        if callable(success) and not success():
            code = getattr(response, "code", "unknown")
            message = getattr(response, "msg", "unknown")
            raise FeishuDeliveryError("Feishu API {}: {}".format(code, message))


class FeishuWebhookSender:
    """Send interactive cards through a Feishu group-bot webhook.

    The webhook needs no SDK, no app credentials and no binding, which makes it
    the fastest path to a working notification loop.
    """

    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout

    def __call__(self, item: OutboxMessage) -> None:
        try:
            card = json.loads(str(item.content))
        except (TypeError, ValueError) as exc:
            raise FeishuDeliveryError("Feishu invalid card") from exc
        payload = {"msg_type": "interactive", "card": card}
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "options-radar/0.2"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            raw_code = body.get("code") if isinstance(body, Mapping) else None
            code = int(raw_code) if raw_code is not None else -1
            if code != 0:
                message = str(body.get("msg", "api_error"))[:160] if isinstance(body, Mapping) else "api_error"
                raise FeishuDeliveryError("Feishu webhook {}: {}".format(code, message))
        except urllib.error.HTTPError as exc:
            raise FeishuDeliveryError("Feishu webhook http_{}".format(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FeishuDeliveryError("Feishu webhook network_error") from exc


WEBHOOK_RECEIVE_ID = "webhook"


def default_feishu_sender() -> Optional[Callable[[OutboxMessage], None]]:
    """Return a working sender for the configured mode, or ``None``.

    Priority: group-bot webhook, then app credentials (lark-oapi).  A webhook
    also implies a fixed destination so binding is never required.
    """
    webhook_url = load_feishu_webhook()
    if webhook_url:
        return FeishuWebhookSender(webhook_url)
    try:
        credentials = load_feishu_credentials()
    except FeishuConfigurationError:
        return None
    return LarkMessageSender(credentials)


def _attribute(value: Any, *names: str) -> Any:
    current = value
    for name in names:
        if current is None:
            return None
        if isinstance(current, Mapping):
            current = current.get(name)
        else:
            current = getattr(current, name, None)
    return current


def _event_payload(event: Any) -> Dict[str, Any]:
    if isinstance(event, Mapping):
        return dict(event)
    raw = getattr(event, "raw", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    # SDK objects are not always JSON serializable.  Keeping their useful
    # identifiers in the typed inbox columns is enough; payload is diagnostic.
    return {}


def extract_text_event(event: Any) -> Optional[InboxMessage]:
    """Extract a text message from either a raw event dict or an SDK object."""

    root = _attribute(event, "event") or event
    message = _attribute(root, "message")
    if message is None:
        return None
    message_id = _attribute(message, "message_id")
    chat_id = _attribute(message, "chat_id")
    message_type = _attribute(message, "message_type") or "text"
    if not message_id or not chat_id or message_type != "text":
        return None
    content = _attribute(message, "content")
    if isinstance(content, str):
        try:
            content_data = json.loads(content)
        except (TypeError, ValueError):
            content_data = {"text": content}
    elif isinstance(content, Mapping):
        content_data = content
    else:
        content_data = {}
    text = str(content_data.get("text", ""))
    sender_id = (
        _attribute(root, "sender", "sender_id", "open_id")
        or _attribute(root, "sender", "sender_id", "user_id")
        or ""
    )
    return InboxMessage(
        message_id=str(message_id),
        chat_id=str(chat_id),
        sender_id=str(sender_id),
        text=text,
        payload=_event_payload(event),
    )


class FeishuBot:
    """Durable Feishu command service.

    ``handle_event`` is the SDK callback and performs only extraction plus one
    short SQLite transaction.  ``process_one`` and ``deliver_one`` belong in a
    worker thread/process.
    """

    def __init__(
        self,
        callbacks: FeishuCallbacks,
        database_path: Path,
        sender: Optional[Callable[[OutboxMessage], None]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.callbacks = callbacks
        self.store = FeishuStore(database_path, clock=clock)
        self.sender = sender
        self.clock = clock
        self.started_at = float(clock())
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._worker_error: Optional[str] = None
        self._receiver_running = False

    def close(self) -> None:
        self.stop()
        self.store.close()

    def handle_event(self, event: Any) -> bool:
        """Fast event callback: validate, deduplicate and enqueue."""

        message = extract_text_event(event)
        if message is None:
            return False
        return self.store.enqueue_inbox(
            message.message_id,
            message.chat_id,
            message.text,
            sender_id=message.sender_id,
            payload=message.payload,
        )

    def _webhook_mode(self) -> bool:
        if isinstance(self.sender, FeishuWebhookSender):
            return True
        if self.sender is None and load_feishu_webhook():
            return True
        return False

    def enqueue_card(
        self, card: Mapping[str, Any], receive_id: Optional[str] = None,
        source_message_id: Optional[str] = None
    ) -> Optional[str]:
        if self._webhook_mode():
            destination = receive_id or WEBHOOK_RECEIVE_ID
        else:
            destination = receive_id or self.store.bound_chat_id() or self.store.latest_chat_id()
        if not destination:
            return None
        source = source_message_id or "system-{}".format(int(self.clock()))
        return self.store.enqueue_outbox(source, destination, card)

    # Explicit name for wiring into callback routers.
    on_message = handle_event

    def test_binding(self) -> Dict[str, Any]:
        """Queue a small card to the bound user, without doing network I/O.

        The regular outbox worker performs delivery and records any API error;
        this method is therefore safe for the dashboard's synchronous action.
        """
        if self._webhook_mode():
            queue_id = self.enqueue_card(
                build_card("飞书连接测试", "绑定成功，日报将发送到此对话。", "green"),
                receive_id=WEBHOOK_RECEIVE_ID,
                source_message_id="feishu-test",
            )
            return {"status": "queued", "queue_id": queue_id, "mode": "webhook"}
        chat_id = self.store.bound_chat_id() or self.store.latest_chat_id()
        if not chat_id:
            return {"status": "error", "message": "请先在飞书私聊机器人发送“绑定”。"}
        queue_id = self.enqueue_card(
            build_card("飞书连接测试", "绑定成功，日报将发送到此对话。", "green"),
            receive_id=chat_id,
            source_message_id="feishu-test",
        )
        return {"status": "queued", "queue_id": queue_id, "chat_id": chat_id}

    def test_credentials(self, timeout: float = 10.0) -> Dict[str, Any]:
        """Validate app credentials and, when bound, queue a test card."""
        if load_feishu_webhook():
            binding = self.test_binding()
            return {"provider": "feishu", "mode": "webhook", "status": "ok", "binding": binding}
        result = probe_feishu_credentials(timeout=timeout)
        if result.get("status") != "ok":
            return result
        binding = self.test_binding()
        result["binding"] = binding
        return result

    def _dispatch(self, command: ParsedCommand) -> Dict[str, Any]:
        if command.name == "bind":
            return build_card("绑定成功", "已记录此飞书私聊，日报和A级提醒将发送到这里。", "green")
        if command.name == "today":
            result = self.callbacks.today_recommendations()
            return dict(result) if _is_card(result) else build_recommendations_card(result)
        if command.name == "positions":
            result = self.callbacks.positions()
            return dict(result) if _is_card(result) else build_positions_card(result)
        if command.name == "add_watchlist":
            result = self.callbacks.add_watchlist(str(command.argument))
            return dict(result) if _is_card(result) else build_watchlist_card(str(command.argument), True, result)
        if command.name == "remove_watchlist":
            result = self.callbacks.remove_watchlist(str(command.argument))
            return dict(result) if _is_card(result) else build_watchlist_card(str(command.argument), False, result)
        if command.name == "explain_rank":
            result = self.callbacks.explain_rank(int(command.argument))
            return dict(result) if _is_card(result) else build_explanation_card(int(command.argument), result)
        if command.name == "help":
            return build_card("使用帮助", COMMAND_HELP, "blue")
        if command.name == "sync_futu" and self.callbacks.sync_futu:
            return build_card("富途同步", _render_value(self.callbacks.sync_futu()), "blue")
        if command.name == "system_status" and self.callbacks.system_status:
            return build_card("系统状态", _render_value(self.callbacks.system_status()), "blue")
        result = self.callbacks.free_chat(str(command.argument))
        return dict(result) if _is_card(result) else build_answer_card(result)

    def process_one(self) -> bool:
        message = self.store.claim_inbox()
        if message is None:
            return False
        try:
            command = parse_command(message.text)
            if command.name == "bind":
                self.store.bind_sender(message.sender_id, message.chat_id)
            card = self._dispatch(command)
        except Exception as exc:
            # The failure is visible in health/SQLite.  A compact reply is also
            # persisted so users do not wait indefinitely.
            self.store.fail_inbox(message.message_id, exc)
            self.store.enqueue_outbox(message.message_id, message.chat_id, build_error_card())
            return True
        self.store.complete_inbox(message, card)
        return True

    def process_pending(self, limit: int = 100) -> int:
        count = 0
        while count < limit and self.process_one():
            count += 1
        return count

    def deliver_one(self) -> bool:
        if self.sender is None:
            return False
        item = self.store.claim_outbox()
        if item is None:
            return False
        try:
            self.sender(item)
        except Exception as exc:
            self.store.mark_delivery_failed(item, exc)
        else:
            self.store.mark_sent(item)
        return True

    def deliver_pending(self, limit: int = 100) -> int:
        count = 0
        while count < limit and self.deliver_one():
            count += 1
        return count

    def work_once(self) -> bool:
        processed = self.process_one()
        delivered = self.deliver_one()
        return processed or delivered

    def _worker_loop(self, poll_seconds: float) -> None:
        try:
            while not self._stop.is_set():
                if not self.work_once():
                    self._stop.wait(poll_seconds)
        except Exception as exc:  # pragma: no cover - last-resort observability
            self._worker_error = "{}: {}".format(type(exc).__name__, exc)

    def start_worker(self, poll_seconds: float = 0.25) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker_error = None
        self._worker = threading.Thread(
            target=self._worker_loop,
            args=(poll_seconds,),
            name="feishu-outbox-worker",
            daemon=True,
        )
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout)

    @staticmethod
    def _iso_timestamp(raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return None

    def health(self) -> Dict[str, Any]:
        counts = self.store.counts()
        worker_alive = bool(self._worker and self._worker.is_alive())
        degraded = bool(counts["inbox_failed"] or counts["outbox_dead"] or self._worker_error)
        if degraded:
            status = "degraded"
        elif worker_alive or self._receiver_running:
            status = "ok"
        else:
            status = "stopped"
        result: Dict[str, Any] = {
            "status": status,
            "worker_alive": worker_alive,
            "receiver_running": self._receiver_running,
            "uptime_seconds": max(0.0, float(self.clock()) - self.started_at),
            "last_event_at": self._iso_timestamp(self.store.runtime_value("last_event_at")),
            "last_sent_at": self._iso_timestamp(self.store.runtime_value("last_sent_at")),
            "worker_error": self._worker_error,
        }
        result.update(counts)
        return result

    health_status = health

    def run_forever(
        self,
        verification_token: str = "",
        encrypt_key: str = "",
        log_level: Any = None,
    ) -> None:
        """Start delivery and, for app credentials, the lark-oapi receiver."""
        if self.sender is None:
            self.sender = default_feishu_sender()
        self.start_worker()
        if self.sender is None or isinstance(self.sender, FeishuWebhookSender):
            # Webhook mode only needs the outbox worker; no websocket receiver.
            self._receiver_running = self.sender is not None
            try:
                while not self._stop.wait(5.0):
                    pass
            finally:
                self._receiver_running = False
                self.stop()
            return
        credentials = load_feishu_credentials()
        lark = importlib.import_module("lark_oapi")

        def receive(data: Any) -> None:
            self.handle_event(data)

        handler = (
            lark.EventDispatcherHandler.builder(verification_token, encrypt_key)
            .register_p2_im_message_receive_v1(receive)
            .build()
        )
        level = log_level if log_level is not None else getattr(getattr(lark, "LogLevel", object), "INFO", None)
        kwargs: Dict[str, Any] = {"event_handler": handler}
        if level is not None:
            kwargs["log_level"] = level
        client = lark.ws.Client(credentials.app_id, credentials.app_secret, **kwargs)
        self.start_worker()
        self._receiver_running = True
        try:
            client.start()
        finally:
            self._receiver_running = False
            self.stop()


# A service-oriented alias keeps call sites readable without duplicating state.
FeishuService = FeishuBot


__all__ = [
    "COMMAND_HELP",
    "COMMAND_TODAY",
    "COMMAND_POSITIONS",
    "COMMAND_ADD_WATCHLIST",
    "COMMAND_REMOVE_WATCHLIST",
    "COMMAND_EXPLAIN",
    "COMMAND_BIND",
    "RETRY_DELAYS_SECONDS",
    "FeishuBot",
    "FeishuService",
    "FeishuCallbacks",
    "FeishuCredentials",
    "FeishuStore",
    "FeishuConfigurationError",
    "FeishuDeliveryError",
    "InboxMessage",
    "OutboxMessage",
    "LarkMessageSender",
    "ParsedCommand",
    "build_answer_card",
    "build_card",
    "build_error_card",
    "build_explanation_card",
    "build_positions_card",
    "build_recommendations_card",
    "build_watchlist_card",
    "extract_text_event",
    "load_feishu_credentials",
    "probe_feishu_credentials",
    "parse_command",
]
