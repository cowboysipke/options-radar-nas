"""Lightweight authenticated Chinese dashboard for the NAS container.

The module intentionally uses only the Python standard library.  Runtime
services are connected through named callbacks so the HTTP layer has no broker,
database, or scheduler dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

import yaml


SESSION_COOKIE = "radar_setup_session"
BUILD_VERSION = os.getenv("OPTIONS_RADAR_BUILD_VERSION", "v2-local")
BUILD_SHA = os.getenv("OPTIONS_RADAR_GIT_SHA", "working-tree")
_dashboard_date_cache: List[str] = []
SYMBOLIC_NAME = re.compile(r"^[A-Za-z0-9_.:/ @\-\u4e00-\u9fff]{0,160}$")
MODEL_NAME = re.compile(r"^[A-Za-z0-9._:/-]{1,100}$")
DashboardCallback = Callable[[Mapping[str, Any]], Any]

DEFAULT_CONFIG: Dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "database_path": "/data/options_radar.db",
    "evidence_dir": "/data/evidence",
    "discord": {
        "source_server": "",
        "channel_names": {
            "flow": "异常期权",
            "pa": "pa分析师",
            "mr": "mr分析师",
            "qmr": "qmr分析师",
            "fqd": "fqd分析师",
            "guide": "使用指南",
            "subscriptions": "分析师订阅面板",
        },
    },
    "notifications": {"provider": "feishu", "feishu_app_id": ""},
    "futu": {
        "host": "127.0.0.1",
        "port": 11111,
        "user_id": "",
        "security_firm": "NONE",
        "watchlist_group": "Options Radar",
        "max_quote_age_seconds": 300,
        "auto_hold_quote_right": 1,
    },
    "ibkr": {"enabled": True, "host": "127.0.0.1", "port": 0, "client_id": 71,
             "readonly": True, "market_data_type": 3, "flex_query_id": ""},
    "providers": {
        "market_priority": ["ibkr", "massive", "futu", "tradier", "alpaca", "marketdata_app"],
        "enabled": {"futu": False, "ibkr": True, "tradier": False, "alpaca": False,
                    "massive": True, "marketdata_app": False},
        "portfolio": {"aggregate_enabled_accounts": True},
        "execution": {"accepted_quality": ["realtime"], "max_quote_age_seconds": 60,
                      "conflict_threshold_pct": 15},
    },
    "market": {"provider": "ibkr"},
    "ai": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "flash_model": "deepseek-v4-flash",
        "pro_model": "deepseek-v4-pro",
    },
    "scoring": {
        "recommendation_threshold": 65,
        "alert_threshold": 80,
        "disagreement_threshold": 0.25,
        "min_dte": 14,
        "max_dte": 60,
        "min_abs_delta": 0.30,
        "max_abs_delta": 0.65,
        "max_spread_pct": 0.12,
        "min_open_interest": 100,
    },
    "paper": {
        "starting_cash": 100000,
        "risk_per_trade": 0.01,
        "max_open_positions": 5,
        "take_profit_pct": 0.35,
        "stop_loss_pct": 0.25,
        "max_holding_business_days": 5,
        "exit_before_expiry_days": 3,
    },
    "backtest": {"use_synthetic_when_unavailable": True},
    "schedule": {"report_delay_minutes": 75},
    "secret_refs": {
        "deepseek_api_key": "/data/secrets/deepseek_api_key",
        "feishu_app_secret": "/data/secrets/feishu_app_secret",
        "feishu_webhook": "/data/secrets/feishu_webhook",
        "futu_login_password_md5": "/data/secrets/futu_login_password_md5",
        "ibkr_flex_token": "/data/secrets/ibkr_flex_token",
        "massive_api_key": "/data/secrets/massive_api_key",
        "alpaca_api_key": "/data/secrets/alpaca_api_key",
        "alpaca_api_secret": "/data/secrets/alpaca_api_secret",
        "marketdata_api_key": "/data/secrets/marketdata_api_key",
        "tradier_token": "/data/secrets/tradier_token",
        "discord_user_token": "/data/secrets/discord_user_token",
    },
    "setup_completed": False,
}

SECRET_ENV_FILES = {
    "DeepSeek API Key": "DEEPSEEK_API_KEY_FILE",
    "飞书 App Secret": "FEISHU_APP_SECRET_FILE",
    "飞书 Webhook URL": "FEISHU_WEBHOOK_URL_FILE",
    "Massive API Key": "MASSIVE_API_KEY_FILE",
    "Alpaca API Key": "ALPACA_API_KEY_FILE",
    "Alpaca API Secret": "ALPACA_API_SECRET_FILE",
    "MarketData.app API Key": "MARKETDATA_API_KEY_FILE",
    "Tradier Token": "TRADIER_TOKEN_FILE",
    "Discord 用户 Token": "DISCORD_USER_TOKEN_FILE",
}

SECRET_FORM_FIELDS = {
    "deepseek_api_key": ("DeepSeek API Key", "deepseek_api_key"),
    "feishu_app_secret": ("飞书 App Secret", "feishu_app_secret"),
    "feishu_webhook": ("飞书 Webhook URL", "feishu_webhook"),
    "massive_api_key": ("Massive API Key", "massive_api_key"),
    "alpaca_api_key": ("Alpaca API Key", "alpaca_api_key"),
    "alpaca_api_secret": ("Alpaca API Secret", "alpaca_api_secret"),
    "marketdata_api_key": ("MarketData.app API Key", "marketdata_api_key"),
    "tradier_token": ("Tradier Token", "tradier_token"),
    "discord_user_token": ("Discord 用户 Token", "discord_user_token"),
}


def _deep_merge(base: Dict[str, Any], incoming: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in incoming.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(dict(result[key]), value)
        else:
            result[key] = value
    return result


def _get_path(data: Mapping[str, Any], path: str, default: Any = "") -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _set_path(data: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = data
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


@dataclass(frozen=True)
class Field:
    form_name: str
    config_path: str
    label: str
    parser: Callable[[str], Any]


def _plain(value: str) -> str:
    value = value.strip()
    if not SYMBOLIC_NAME.fullmatch(value):
        raise ValueError("含有不支持的字符")
    return value


def _model(value: str) -> str:
    value = value.strip()
    if not MODEL_NAME.fullmatch(value):
        raise ValueError("模型名称格式错误")
    return value


def _delay(value: str) -> int:
    result = int(value)
    if not 15 <= result <= 180:
        raise ValueError("日报延迟需为15至180分钟")
    return result


FIELDS = (
    Field("timezone", "timezone", "时区", _plain),
    Field("discord_server", "discord.source_server", "Discord服务器", _plain),
    Field("flow_channel", "discord.channel_names.flow", "异常期权频道", _plain),
    Field("pa_channel", "discord.channel_names.pa", "PA频道", _plain),
    Field("mr_channel", "discord.channel_names.mr", "MR频道", _plain),
    Field("qmr_channel", "discord.channel_names.qmr", "QMR频道", _plain),
    Field("fqd_channel", "discord.channel_names.fqd", "FQD频道", _plain),
    Field("guide_channel", "discord.channel_names.guide", "使用指南频道", _plain),
    Field("subscriptions_channel", "discord.channel_names.subscriptions", "分析师订阅面板频道", _plain),
    Field("feishu_app_id", "notifications.feishu_app_id", "飞书 App ID", _plain),
    Field("flash_model", "ai.flash_model", "DeepSeek日常模型", _model),
    Field("pro_model", "ai.pro_model", "DeepSeek复核模型", _model),
    Field("report_delay", "schedule.report_delay_minutes", "收盘后日报延迟（分钟）", _delay),
)


class SetupConfigStore:
    """Atomic storage for whitelisted non-secret setup fields."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return _deep_merge({}, DEFAULT_CONFIG)
            with self.path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
            if not isinstance(data, dict):
                raise ValueError("config.yaml must contain a mapping")
            discord = data.get("discord", {})
            if isinstance(discord, dict) and "channel_names" not in discord:
                source_channels = discord.get("source_channels", {})
                if isinstance(source_channels, dict):
                    discord["channel_names"] = {
                        analyst: channel for channel, analyst in source_channels.items()
                        if analyst in {"flow", "pa", "mr", "qmr", "fpd", "fqd", "guide", "subscriptions"}
                    }
            return _deep_merge(DEFAULT_CONFIG, data)

    def initialize(self) -> Dict[str, Any]:
        with self._lock:
            data = self.load()
            expected = self.fixed_secret_refs()
            old_refs = data.get("secret_refs", {}) if isinstance(data.get("secret_refs"), dict) else {}
            for name, destination in expected.items():
                source = Path(str(old_refs.get(name, "")))
                target = Path(destination)
                if not target.is_file() and source.is_file() and source.resolve() != target.resolve():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
            data["secret_refs"] = expected
            if not self.path.exists() or old_refs != expected:
                self.save(data)
            return self.load()

    def fixed_secret_refs(self) -> Dict[str, str]:
        data_dir = Path(os.getenv("DATA_DIR", str(self.path.parent)))
        secret_dir = data_dir / "secrets"
        return {name: str(secret_dir / name) for name in DEFAULT_CONFIG["secret_refs"]}

    def secret_presence(self) -> Dict[str, bool]:
        refs = self.load().get("secret_refs", self.fixed_secret_refs())
        ref_by_label = {
            "DeepSeek API Key": "deepseek_api_key",
            "飞书 App Secret": "feishu_app_secret",
            "飞书 Webhook URL": "feishu_webhook",
            "富途登录凭据": "futu_login_password_md5",
            "Massive API Key": "massive_api_key",
            "Alpaca API Key": "alpaca_api_key",
            "Alpaca API Secret": "alpaca_api_secret",
            "MarketData.app API Key": "marketdata_api_key",
            "Tradier Token": "tradier_token",
            "Discord 用户 Token": "discord_user_token",
        }
        result: Dict[str, bool] = {}
        for label, env_name in SECRET_ENV_FILES.items():
            path = Path(str(refs.get(ref_by_label[label], "")))
            result[label] = bool(_read_secret(env_name) or (
                path.read_text(encoding="utf-8").strip() if path.is_file() else ""
            ))
        return result

    def update_from_form(self, values: Mapping[str, str]) -> Dict[str, Any]:
        with self._lock:
            data = self.load()
            errors = []
            for field in FIELDS:
                try:
                    _set_path(data, field.config_path, field.parser(values.get(field.form_name, "")))
                except (TypeError, ValueError) as exc:
                    errors.append(f"{field.label}: {exc}")
            if errors:
                raise ValueError("；".join(errors))
            channel_names = data["discord"]["channel_names"]
            if len(set(channel_names.values())) != len(channel_names):
                raise ValueError("Discord频道名称不可重复")
            data["discord"]["source_channels"] = {
                channel_names[source]: source
                for source in ("flow", "pa", "mr", "qmr", "fqd", "fpd", "guide", "subscriptions")
                if source in channel_names
            }
            data["market"]["provider"] = "ibkr"
            data["ibkr"]["enabled"] = True
            data["providers"]["market_priority"] = ["ibkr", "massive", "futu", "tradier", "alpaca", "marketdata_app"]
            data["providers"]["enabled"]["ibkr"] = True
            data["providers"]["enabled"]["futu"] = False
            data["setup_completed"] = True
            data["secret_refs"] = self.fixed_secret_refs()
            self._save_secrets(values, data["secret_refs"])
            self.save(data)
            return data

    @staticmethod
    def _save_secrets(values: Mapping[str, str], refs: Mapping[str, str]) -> None:
        pending = dict(SECRET_FORM_FIELDS)
        # Password exists only in the request buffer, then only its MD5 form is persisted.
        futu_password = str(values.get("futu_login_password", ""))
        if futu_password:
            if len(futu_password) > 4096 or any(c in futu_password for c in "\x00\n\r"):
                raise ValueError("富途登录密码: 格式错误")
            values = dict(values)
            # OpenD's login protocol requires an MD5-form credential.  This is
            # protocol formatting rather than a password-verification hash.
            values["futu_login_password_md5"] = hashlib.md5(futu_password.encode("utf-8")).hexdigest()
            pending["futu_login_password_md5"] = ("富途登录凭据", "futu_login_password_md5")
        for form_name, (label, ref_name) in pending.items():
            value = str(values.get(form_name, "")).strip()
            if not value:
                continue
            if len(value) > 4096 or any(c in value for c in "\x00\n\r"):
                raise ValueError(f"{label}: 格式错误")
            destination = Path(str(refs[ref_name]))
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{ref_name}-", dir=str(destination.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(value)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(temp_name, 0o600)
                except OSError:
                    pass
                os.replace(temp_name, destination)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)

    def save(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False)
        fd, temp_name = tempfile.mkstemp(prefix=".config-", suffix=".yaml", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp_name, 0o640)
            except OSError:
                pass
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


class SetupApplication:
    def __init__(
        self,
        store: SetupConfigStore,
        token: str,
        health: Optional[Callable[[], Mapping[str, Any]]] = None,
        callbacks: Optional[Mapping[str, DashboardCallback]] = None,
        session_ttl: int = 3600,
        local_mode: bool = False,
    ):
        if not local_mode and len(token) < 16:
            raise ValueError("SETUP_TOKEN must contain at least 16 characters")
        self.store = store
        self._token_digest = hashlib.sha256(token.encode("utf-8")).digest()
        self.health = health or (lambda: {"status": "ok"})
        self.callbacks = dict(callbacks or {})
        self.session_ttl = session_ttl
        self.local_mode = bool(local_mode)
        self._sessions: Dict[str, Tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._local_session: Optional[Tuple[str, str]] = self.new_session() if self.local_mode else None

    def build_info(self) -> Dict[str, str]:
        return {"version": BUILD_VERSION, "git_sha": BUILD_SHA,
                "mode": "local" if self.local_mode else "nas"}

    def local_session(self) -> Optional[Tuple[str, str]]:
        if not self.local_mode:
            return None
        current = self._local_session
        if current is None:
            return None
        with self._lock:
            record = self._sessions.get(current[0])
            if not record or record[0] <= time.time():
                # Avoid recursively taking the lock through new_session.
                session_id = secrets.token_urlsafe(32)
                csrf = secrets.token_urlsafe(24)
                self._sessions[session_id] = (time.time() + self.session_ttl, csrf)
                self._local_session = (session_id, csrf)
            return self._local_session

    def authenticate_token(self, token: str) -> bool:
        candidate = hashlib.sha256(token.encode("utf-8")).digest()
        return hmac.compare_digest(candidate, self._token_digest)

    def new_session(self) -> Tuple[str, str]:
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with self._lock:
            self._sessions[session_id] = (time.time() + self.session_ttl, csrf)
        return session_id, csrf

    def session(self, cookie_header: str) -> Optional[Tuple[str, str]]:
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header or "")
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        if not morsel:
            return None
        session_id = morsel.value
        with self._lock:
            record = self._sessions.get(session_id)
            if not record or record[0] <= time.time():
                self._sessions.pop(session_id, None)
                return None
        return session_id, record[1]

    def logout(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def invoke(self, name: str, payload: Optional[Mapping[str, Any]] = None) -> Any:
        callback = self.callbacks.get(name)
        if callback is None:
            return {"status": "not_configured", "callback": name}
        return callback(dict(payload or {}))


def _read_secret(path_env: str, direct_env: Optional[str] = None) -> str:
    path = os.getenv(path_env, "")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.getenv(direct_env or "", "").strip() if direct_env else ""


def secret_presence() -> Dict[str, bool]:
    refs = DEFAULT_CONFIG["secret_refs"]
    by_label = {
        "DeepSeek API Key": "deepseek_api_key",
        "飞书 App Secret": "feishu_app_secret",
        "富途登录凭据": "futu_login_password_md5",
        "Massive API Key": "massive_api_key",
        "Alpaca API Key": "alpaca_api_key",
        "Alpaca API Secret": "alpaca_api_secret",
        "MarketData.app API Key": "marketdata_api_key",
        "Tradier Token": "tradier_token",
    }
    return {
        label: bool(_read_secret(env_name) or (
            Path(str(refs[by_label[label]])).read_text(encoding="utf-8").strip()
            if Path(str(refs[by_label[label]])).is_file() else ""
        ))
        for label, env_name in SECRET_ENV_FILES.items()
    }


PAGE_INFO = {
    "/": ("今日推荐", "recommendations", "按评分排序的前5张候选合约"),
    "/signals": ("信号明细", "signals", "Discord原文、分析师意见和融合过程"),
    "/portfolio": ("自选与持仓", "portfolio", "IBKR组合与富途一次性导入自选"),
    "/backtest": ("回测", "backtest", "模拟盘、回测结算与策略优化"),
    "/system": ("系统诊断", "status", "IB Gateway、Discord、Massive、DeepSeek和飞书"),
    "/setup": ("设置", "status", "首次配置和连接测试"),
    "/providers": ("数据源诊断", "providers", "IBKR主源与Massive历史复核"),
}

GET_APIS = {
    "/api/status": "status",
    "/api/futu/status": "futu_status",
    "/api/recommendations": "recommendations",
    "/api/portfolio": "portfolio",
    "/api/contracts": "contracts",
    "/api/rules": "rules",
    "/api/analysts": "analysts",
    "/api/backtest": "backtest",
    "/api/providers": "providers",
    "/api/signals": "signals",
}

POST_APIS = {
    "/api/futu/send-verification": "futu_send_verification",
    "/api/futu/submit-verification": "futu_submit_verification",
    "/api/futu/relogin": "futu_relogin",
    "/api/futu/sync": "futu_sync",
    "/api/actions/collect": "collect",
    "/api/actions/report": "report",
    "/api/actions/backup": "backup",
    "/api/actions/feishu-test": "feishu_test",
    "/api/actions/discord-login": "discord_login",
    "/api/actions/deepseek-test": "deepseek_test",
    "/api/ibkr/discover": "ibkr_discover",
    "/api/ibkr/sync": "ibkr_sync",
    "/api/portfolio/refresh": "portfolio_refresh",
}

PROVIDER_STATUS_ROUTE = re.compile(r"^/api/providers/([a-z0-9_-]+)/status$")
PROVIDER_ACTION_ROUTE = re.compile(r"^/api/providers/([a-z0-9_-]+)/(test|enable|disable|priority)$")
MARKET_PROVENANCE_PREFIX = "/api/market/provenance/"
MARKET_COMPARE_PREFIX = "/api/market/compare/"

# Browser-facing action URLs deliberately avoid API-looking navigation. Some
# privacy extensions block form navigation to paths containing words such as
# ``send-verification`` and show ERR_BLOCKED_BY_CLIENT before rendering the
# valid server response.
FORM_ACTIONS = {
    "/futu/send-code": "futu_send_verification",
    "/futu/submit-code": "futu_submit_verification",
    "/futu/relogin": "futu_relogin",
    "/futu/sync": "futu_sync",
    "/futu/import-watchlist": "futu_import_watchlist",
}


class SetupRequestHandler(BaseHTTPRequestHandler):
    server_version = "OptionsRadarDashboard/2"

    @property
    def app(self) -> SetupApplication:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _headers(self, status: int, content_type: str, length: int, extra: Optional[Mapping[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8", extra=None) -> None:
        payload = body.encode("utf-8")
        self._headers(status, content_type, len(payload), extra)
        self.wfile.write(payload)

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str), "application/json; charset=utf-8")

    def _redirect(self, path: str, extra=None) -> None:
        headers = {"Location": path}
        headers.update(extra or {})
        self._headers(HTTPStatus.SEE_OTHER, "text/plain; charset=utf-8", 0, headers)

    def _body(self) -> Dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if size < 0 or size > 65536:
            raise ValueError("request body is too large")
        raw = self.rfile.read(size).decode("utf-8")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() == "application/json":
            value = json.loads(raw or "{}")
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object")
            return value
        parsed = parse_qs(raw, keep_blank_values=True, max_num_fields=64)
        return {key: values[-1] for key, values in parsed.items()}

    def _session(self) -> Optional[Tuple[str, str]]:
        session = self.app.session(self.headers.get("Cookie", ""))
        if session:
            return session
        if self.app.local_mode and self.client_address[0] in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}:
            return self.app.local_session()
        return None

    def _callback(self, name: str, payload: Mapping[str, Any]) -> Any:
        try:
            return self.app.invoke(name, payload)
        except Exception as exc:  # boundary: runtime errors become stable HTTP responses
            message = str(exc) or type(exc).__name__
            return {"status": "error", "message": message}

    @staticmethod
    def _contract_key(path: str, prefix: str) -> Optional[str]:
        """Return one decoded contract identifier without accepting sub-paths."""
        if not path.startswith(prefix):
            return None
        value = unquote(path[len(prefix):]).strip()
        if not value or len(value) > 160 or "/" in value or "\\" in value:
            return None
        if any(ord(character) < 32 for character in value):
            return None
        return value

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        if path == "/health":
            if self.client_address[0] not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}:
                self._send(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")
                return
            data = dict(self.app.health())
            data["configured"] = bool(self.app.store.load().get("setup_completed"))
            data["build"] = self.app.build_info()
            self._json(HTTPStatus.OK, data)
            return
        session = self._session()
        if path in {"/discord-login.png", "/futu-captcha.png"}:
            if not session:
                self._send(HTTPStatus.UNAUTHORIZED, "Login required", "text/plain; charset=utf-8")
                return
            root = Path(os.getenv("DATA_DIR", "/data"))
            image_path = (
                root / "evidence" / "discord-login.png"
                if path == "/discord-login.png"
                else root / "opend-profile" / ".com.futunn.FutuOpenD" / "F3CNN" / "PicVerifyCode.png"
            )
            if not image_path.is_file():
                self._send(HTTPStatus.NOT_FOUND, "QR image not ready", "text/plain; charset=utf-8")
                return
            payload = image_path.read_bytes()
            self._headers(HTTPStatus.OK, "image/png", len(payload))
            self.wfile.write(payload)
            return
        provider_match = PROVIDER_STATUS_ROUTE.fullmatch(path)
        contract_key = self._contract_key(path, MARKET_PROVENANCE_PREFIX)
        if path in GET_APIS or provider_match or contract_key is not None:
            if not session:
                self._json(HTTPStatus.UNAUTHORIZED, {"status": "unauthorized"})
                return
            if provider_match:
                name = "provider_status"
                query["provider"] = provider_match.group(1)
            elif contract_key is not None:
                name = "market_provenance"
                query["contract_key"] = contract_key
            else:
                name = GET_APIS[path]
            if name == "status" and name not in self.app.callbacks:
                result = dict(self.app.health())
            else:
                result = self._callback(name, query)
            if name == "status" and isinstance(result, Mapping):
                result = dict(result)
                result.setdefault("build", self.app.build_info())
            self._json(HTTPStatus.OK, result)
            return
        if path == "/setup" or path in PAGE_INFO:
            if not session:
                self._send(HTTPStatus.OK, self._login_page())
                return
            _, csrf = session
            if path == "/setup":
                self._send(HTTPStatus.OK, self._setup_page(csrf))
            else:
                self._send(HTTPStatus.OK, self._dashboard_page(path, csrf, query))
            return
        self._send(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            values = self._body()
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, html.escape(str(exc)), "text/plain; charset=utf-8")
            return
        if path == "/login":
            if self.app.local_mode:
                # Compatibility with an old cached login page.  Local mode
                # does not validate or persist a management token.
                self._redirect("/")
                return
            if not self.app.authenticate_token(str(values.get("token", ""))):
                time.sleep(0.2)
                self._send(HTTPStatus.UNAUTHORIZED, self._login_page("管理口令不正确"))
                return
            session_id, _ = self.app.new_session()
            cookie = f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={self.app.session_ttl}"
            self._redirect("/", {"Set-Cookie": cookie})
            return
        session = self._session()
        api_request = path.startswith("/api/")
        if not session:
            if api_request:
                self._json(HTTPStatus.UNAUTHORIZED, {"status": "unauthorized"})
            else:
                self._send(HTTPStatus.UNAUTHORIZED, self._login_page("请先登录"))
            return
        session_id, csrf = session
        supplied_csrf = str(values.get("csrf", "") or self.headers.get("X-CSRF-Token", ""))
        if not hmac.compare_digest(supplied_csrf, csrf):
            if api_request:
                self._json(HTTPStatus.FORBIDDEN, {"status": "forbidden", "message": "CSRF validation failed"})
            else:
                self._send(HTTPStatus.FORBIDDEN, "CSRF validation failed", "text/plain; charset=utf-8")
            return
        if path == "/save":
            try:
                self.app.store.update_from_form({str(k): str(v) for k, v in values.items()})
            except ValueError as exc:
                self._send(HTTPStatus.BAD_REQUEST, self._setup_page(csrf, str(exc)))
                return
            reload_result = self._callback("reload_service", {})
            message = str(reload_result.get("message", "配置已保存。")) if isinstance(reload_result, Mapping) else "配置已保存。"
            self._send(HTTPStatus.OK, self._setup_page(csrf, message))
            return
        if path == "/logout":
            self.app.logout(session_id)
            self._redirect("/", {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
            return
        if path in FORM_ACTIONS:
            callback_payload = {key: value for key, value in values.items() if key != "csrf"}
            result = self._callback(FORM_ACTIONS[path], callback_payload)
            if isinstance(result, Mapping):
                state = str(result.get("status", "ok"))
                message = str(result.get("message", "")).strip()
                if not message:
                    message = "操作已完成。" if state != "error" else "操作出错，请查看系统页。"
                payload: Dict[str, Any] = {"status": state, "message": message, "data": result}
            else:
                payload = {"status": "ok", "message": "操作已完成。"}
            status = HTTPStatus.INTERNAL_SERVER_ERROR if isinstance(result, Mapping) and result.get("status") == "error" else HTTPStatus.OK
            self._json(status, payload)
            return
        provider_match = PROVIDER_ACTION_ROUTE.fullmatch(path)
        contract_key = self._contract_key(path, MARKET_COMPARE_PREFIX)
        if path in POST_APIS or provider_match or contract_key is not None:
            callback_payload = {key: value for key, value in values.items() if key != "csrf"}
            if provider_match:
                callback_name = "provider_action"
                callback_payload["provider"] = provider_match.group(1)
                callback_payload["action"] = provider_match.group(2)
            elif contract_key is not None:
                callback_name = "market_compare"
                callback_payload["contract_key"] = contract_key
            else:
                callback_name = POST_APIS[path]
            result = self._callback(callback_name, callback_payload)
            status = HTTPStatus.INTERNAL_SERVER_ERROR if isinstance(result, Mapping) and result.get("status") == "error" else HTTPStatus.OK
            self._json(status, result)
            return
        self._send(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")

    @staticmethod
    def _shell(title: str, content: str, active: str = "") -> str:
        links = (("/", "今日推荐"), ("/signals", "信号明细"),
                  ("/portfolio", "自选与持仓"), ("/backtest", "回测"),
                  ("/system", "系统诊断"), ("/setup", "设置"))
        nav = "".join(
            f'<a class="{"active" if path == active else ""}" href="{path}">{label}</a>' for path, label in links
        )
        return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root{{--ink:#172033;--muted:#64748b;--blue:#2563eb;--bg:#f3f6fb;--card:#fff;--line:#dbe3ef}}
*{{box-sizing:border-box}}body{{font:15px system-ui,-apple-system,"Microsoft YaHei",sans-serif;margin:0;background:var(--bg);color:var(--ink)}}
header{{background:#101827;color:white;padding:14px 20px;display:flex;align-items:center;gap:22px;overflow:auto}}header b{{white-space:nowrap}}nav{{display:flex;gap:5px}}nav a{{color:#cbd5e1;text-decoration:none;padding:8px 10px;border-radius:7px;white-space:nowrap}}nav a.active,nav a:hover{{color:white;background:#263754}}
main{{max-width:1120px;margin:22px auto;padding:0 16px}}h1{{margin:0 0 6px}}h2{{margin-top:26px}}.sub{{color:var(--muted);margin-top:0}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}.card{{background:var(--card);border:1px solid var(--line);padding:17px;border-radius:12px;box-shadow:0 5px 18px #1020400b}}.metric{{font-size:25px;font-weight:750}}.muted,small{{color:var(--muted)}}
label{{display:block;margin:13px 0 5px;font-weight:650}}input{{width:100%;padding:10px;border:1px solid #c9d2e1;border-radius:7px}}button,.button{{display:inline-block;margin:9px 6px 0 0;padding:10px 15px;border:0;border-radius:7px;background:var(--blue);color:white;font-weight:700;text-decoration:none}}button.secondary{{background:#475569}}.ok,.error{{padding:10px;border-radius:7px}}.ok{{background:#e7f8ed}}.error{{background:#feecec}}pre{{white-space:pre-wrap;word-break:break-word;background:#0f172a;color:#dbeafe;padding:14px;border-radius:9px;max-height:520px;overflow:auto}}code{{background:#eef2f8;padding:2px 5px;border-radius:4px}}.status{{display:grid;grid-template-columns:1fr auto;gap:8px}}@media(max-width:620px){{header{{display:block}}nav{{margin-top:9px}}}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left}}summary{{cursor:pointer;font-weight:700}}
</style></head><body><header><b>Options Radar</b><small>版本 {html.escape(BUILD_VERSION)} · {html.escape(BUILD_SHA[:12])}</small><nav>{nav}</nav></header><main>{content}</main>
<script>
document.querySelectorAll('form[data-ajax="1"]').forEach(function(form){{
  form.addEventListener('submit', async function(event){{
    event.preventDefault();
    const button=form.querySelector('button');
    const panel=document.getElementById('action-result');
    button.disabled=true; panel.className='card'; panel.textContent='正在执行：'+button.textContent+'…';
    try {{
      const response=await fetch(form.action,{{method:'POST',body:new URLSearchParams(new FormData(form))}});
      const text=await response.text(); let data;
      try {{data=JSON.parse(text)}} catch(_) {{data={{message:text}}}}
      panel.textContent=(response.ok?'完成：':'失败：')+(data.message||data.status||JSON.stringify(data));
      panel.className=response.ok?'card ok':'card error';
    }} catch(error) {{panel.textContent='请求失败：'+error; panel.className='card error'}}
    finally {{button.disabled=false}}
  }});
}});
</script></body></html>'''

    def _login_page(self, message: str = "") -> str:
        notice = f'<p class="error">{html.escape(message)}</p>' if message else ""
        local = os.getenv("OPTIONS_RADAR_LOCAL") == "1"
        hint = (
            "管理口令显示在启动窗口，也保存在 <code>data-local/setup-token</code>。"
            if local else
            "输入容器日志中的 <code>SETUP CODE</code>，也可在 <code>/data/setup-token</code> 查看。"
        )
        content = (
            f"<h1>异常期权助手</h1><p class=\"sub\">{hint}</p>"
            f'{notice}<div class="card"><form method="post" action="/login"><label>管理口令</label>'
            '<input name="token" type="password" required autofocus><button>进入管理面板</button></form></div>'
        )
        return self._shell("Options Radar 登录", content)

    def _dashboard_page(self, path: str, csrf: str, query: Optional[Mapping[str, str]] = None) -> str:
        params = dict(query or {})
        title, callback, description = PAGE_INFO[path]
        use_default = callback == "status" and callback not in self.app.callbacks
        data = dict(self.app.health()) if use_default else self._callback(callback, params)
        # Populate the date-selector cache from live data.
        if path in {"/", "/signals"}:
            dates = self.app.invoke("dashboard_dates", {})
            if isinstance(dates, list) and dates:
                _dashboard_date_cache.clear()
                _dashboard_date_cache.extend(dates)
        encoded = html.escape(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        encoded = html.escape(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        action_cards = ""
        if path == "/":
            action_cards = self._action_forms(csrf, (("/api/actions/collect", "立即采集并生成推荐"), ("/api/actions/report", "生成飞书日报")))
        elif path == "/portfolio":
            action_cards = self._action_forms(csrf, (("/futu/import-watchlist", "从富途导入自选"), ("/api/portfolio/refresh", "手动刷新持仓")))
        elif path == "/system":
            action_cards = self._action_forms(csrf, (
                ("/api/ibkr/discover", "检测IB Gateway"),
                ("/api/providers/massive/test", "测试Massive"),
                ("/api/actions/deepseek-test", "测试DeepSeek"),
                ("/api/actions/feishu-test", "测试飞书"),
                ("/api/actions/backup", "创建备份"),
            ))
        elif path == "/providers":
            action_cards = self._provider_actions(csrf)
        content = f'<h1>{html.escape(title)}</h1><p class="sub">{html.escape(description)}</p>{self._date_selector(path, params)}<div id="action-result"></div>{action_cards}{self._visual_summary(path, data)}<details class="card"><summary>查看原始数据</summary><pre>{encoded}</pre></details>'
        return self._shell(f"{title} - Options Radar", content, path)

    @staticmethod
    def _date_selector(path: str, params: Mapping[str, str]) -> str:
        if path not in {"/", "/signals"}:
            return ""
        if not _dashboard_date_cache:
            return ""
        selected = str(params.get("date", "")).strip()
        options = [f'<option value="">最近（自动选择）</option>']
        for d in _dashboard_date_cache:
            sel = ' selected' if d == selected else ''
            options.append(f'<option value="{html.escape(d)}"{sel}>{html.escape(d)}</option>')
        return (
            '<form method="get" class="card" style="display:flex;align-items:center;gap:8px">'
            f'<label style="white-space:nowrap">交易日</label>'
            f'<select name="date" onchange="this.form.submit()">{"".join(options)}</select>'
            '<noscript><button>查看</button></noscript>'
            '</form>'
        )

    @staticmethod
    def _visual_summary(path: str, data: Any) -> str:
        if path == "/" and isinstance(data, list):
            if not data:
                return '<section class="card"><h2>今日暂无合格推荐</h2><p class="muted">点击“立即采集并生成推荐”，或先完成Discord与IBKR配置。</p></section>'
            cards = []
            for item in data[:5]:
                key = html.escape(str(item.get("contract_key", "")))
                bid = item.get("bid") if item.get("bid") is not None else "待行情"
                ask = item.get("ask") if item.get("ask") is not None else "待行情"
                entry = item.get("max_entry_price") if item.get("max_entry_price") is not None else "待行情"
                take_profit = item.get("take_profit") if item.get("take_profit") is not None else "待行情"
                stop_loss = item.get("stop_loss") if item.get("stop_loss") is not None else "待行情"
                cards.append('<section class="card"><h2>{} · {}分</h2><p><b>{}</b> · {} · {}</p><p>bid/ask: {} / {}　数据: {}　执行: {}</p><p>入场: {}　止盈: {}　止损: {}</p><p class="muted">理由：{}</p></section>'.format(
                    html.escape(str(item.get("grade", "-"))), html.escape(str(item.get("score", "-"))), key,
                    html.escape(str(item.get("direction", "-"))), html.escape(str(item.get("market_status", "-"))),
                    html.escape(str(bid)), html.escape(str(ask)),
                    html.escape(str(item.get("data_quality", item.get("market_status", "待行情")))),
                    html.escape(str(item.get("execution_status", "待行情"))),
                    html.escape(str(entry)), html.escape(str(take_profit)), html.escape(str(stop_loss)),
                    html.escape(str(item.get("reason", "暂无理由"))),
                ))
            return '<div class="grid">' + "".join(cards) + '</div><details class="card" style="margin-top:16px"><summary>评价标准说明</summary><table><tr><th>维度</th><th>权重</th><th>说明</th></tr><tr><td>共识</td><td>40%</td><td>各分析家族（价格行为/动量反转/资金流向）方向一致性，跨家族冲突扣分封顶64</td></tr><tr><td>历史</td><td>20%</td><td>分析师过去推荐的盈亏表现（基于回测结果动态调整）</td></tr><tr><td>信号质量</td><td>15%</td><td>信号完整性×置信度×时效性</td></tr><tr><td>行情质量</td><td>15%</td><td>实时bid/ask、价差、Open Interest验证</td></tr><tr><td>组合适配</td><td>10%</td><td>标的是否在持仓/自选中、仓位集中度</td></tr></table><p>A级≥80分（飞书提醒）｜B级65-79（合格）｜C级50-64（观察榜）｜D级&lt;50（过滤）</p></details>'
        if path == "/signals" and isinstance(data, list):
            rows = []
            for item in data:
                rows.append('<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
                    html.escape(str(item.get("symbol", ""))), html.escape(str(item.get("contract_key", ""))),
                    html.escape(str(item.get("observed_at", ""))), len(item.get("signals", [])),
                ))
            return '<section class="card"><table><tr><th>标的</th><th>合约</th><th>时间</th><th>分析师意见数</th></tr>{}</table></section>'.format("".join(rows) or '<tr><td colspan="4">暂无信号</td></tr>')
        if path == "/portfolio" and isinstance(data, dict):
            rows = []
            for symbol, item in data.items():
                if not isinstance(item, Mapping):
                    rows.append(f'<tr><td>{html.escape(str(symbol))}</td><td colspan="5">{html.escape(str(item))}</td></tr>')
                    continue
                name = html.escape(str(item.get("company_name", "") or ""))
                price = item.get("current_price")
                change = item.get("change_pct")
                price_str = f"{float(price):.2f}" if price is not None else "-"
                change_str = f"{float(change)*100:+.2f}%" if change is not None else "-"
                rows.append('<tr><td>{}</td><td class="muted">{}</td><td>{}</td><td>{}</td><td>{}</td><td>{:.2%}</td></tr>'.format(
                    html.escape(str(symbol)), name, html.escape(str(item.get("held_quantity", 0))),
                    price_str, change_str, float(item.get("concentration", 0) or 0),
                ))
            return '<section class="card"><table><tr><th>标的</th><th>公司名称</th><th>持仓</th><th>最新价</th><th>涨跌</th><th>集中度</th></tr>{}</table></section>'.format("".join(rows) or '<tr><td colspan="6">暂无组合快照</td></tr>')
        if path == "/backtest" and isinstance(data, dict):
            replay = data.get("replay") or {}
            ranges = data.get("historical_range") or {}
            paper = data.get("paper") or {}
            parts = []
            if ranges.get("start"):
                parts.append(f'<section class="card"><h2>历史回放（{ranges["start"]} ~ {ranges["end"]}，共 {data.get("sessions_available",0)} 个交易日）</h2>')
                parts.append(f'<p>结算笔数: {replay.get("filled",0)} 成交 / {replay.get("no_fill",0)} 未成交</p>')
                parts.append(f'<p>平均收益率: <b>{round(replay.get("avg_net_return",0)*100,2)}%</b>　最大回撤: <b>{round(replay.get("max_drawdown",0)*100,2)}%</b></p>')
                parts.append('<p class="muted">注：基于当前数据库中的信号（不含完整 30 天历史），只反映已有数据。</p>')
                parts.append(f'<details><summary>逐笔明细</summary><pre>{html.escape(json.dumps(replay.get("outcomes",[])[-30:], ensure_ascii=False, indent=2, default=str))}</pre></details>')
                parts.append('</section>')
            if paper:
                parts.append(f'<section class="card"><h2>模拟交易统计</h2><p>平仓: {paper.get("closed",0)} 笔　胜率: {round(paper.get("win_rate",0)*100,1)}%　损益: ${paper.get("realized_pnl",0):,.2f}</p></section>')
            return "".join(parts) if parts else '<section class="card"><p>暂无回测数据。等待系统积累足够交易日后再查看。</p></section>'
        if path == "/system" and isinstance(data, dict):
            statuses = []
            for name in ("ibkr", "discord", "ai", "feishu"):
                value = data.get(name, {})
                if isinstance(value, dict):
                    state = value.get("status", value.get("connected", "unknown"))
                else:
                    state = value
                statuses.append('<section class="card"><h2>{}</h2><div class="metric">{}</div></section>'.format(html.escape(name.upper()), html.escape(str(state))))
            return '<div class="grid">' + "".join(statuses) + '</div>'
        return '<section class="card"><p>数据已加载。展开下方“查看原始数据”查看完整内容。</p></section>'

    @staticmethod
    def _provider_actions(csrf: str) -> str:
        labels = (
            ("futu", "富途 OpenD"),
            ("ibkr", "IBKR TWS/Gateway"),
            ("ibkr_flex", "IBKR Flex"),
            ("massive", "Massive"),
            ("alpaca", "Alpaca"),
            ("marketdata_app", "MarketData.app"),
            ("tradier", "Tradier"),
        )
        cards = []
        for provider, label in labels:
            base = f"/api/providers/{provider}"
            actions = SetupRequestHandler._action_forms(
                csrf,
                ((f"{base}/test", "一键检测"), (f"{base}/enable", "启用"), (f"{base}/disable", "停用")),
            )
            priority = (
                f'<form method="post" action="{base}/priority">'
                f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
                '<label>优先级</label><input type="number" min="1" max="99" name="priority" required>'
                '<button class="secondary">调整优先级</button></form>'
            )
            cards.append(f'<section class="card"><h2>{html.escape(label)}</h2>{actions}{priority}</section>')
        ibkr = SetupRequestHandler._action_forms(
            csrf, (("/api/ibkr/discover", "扫描本机 TWS/Gateway"), ("/api/ibkr/sync", "同步 IBKR")),
        )
        return '<div class="card"><b>自动选择主数据源，或逐个调整优先级</b></div><div class="grid">' + "".join(cards) + "</div>" + ibkr

    @staticmethod
    def _action_forms(csrf: str, actions: Tuple[Tuple[str, str], ...]) -> str:
        labels = {"/api/actions/collect":"立即采集并生成推荐","/api/actions/report":"生成日报","/api/actions/backup":"创建备份","/api/actions/feishu-test":"测试飞书","/api/actions/discord-login":"打开Discord登录","/api/actions/deepseek-test":"测试DeepSeek","/api/ibkr/discover":"检测IB Gateway","/api/ibkr/sync":"同步IBKR持仓","/api/providers/massive/test":"测试Massive","/futu/import-watchlist":"从富途导入自选"}
        return '<div class="card"><b>快捷操作</b><div>' + "".join(f'<form data-ajax="1" style="display:inline" method="post" action="{path}"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><button>{html.escape(labels.get(path, label))}</button></form>' for path, label in actions) + "</div></div>"

    def _setup_page(self, csrf: str, message: str = "") -> str:
        display_labels = {"timezone":"\u65f6\u533a","discord_server":"Discord\u670d\u52a1\u5668","flow_channel":"\u5f02\u5e38\u671f\u6743\u9891\u9053","pa_channel":"PA\u5206\u6790\u5e08\u9891\u9053","mr_channel":"MR\u5206\u6790\u5e08\u9891\u9053","qmr_channel":"QMR\u5206\u6790\u5e08\u9891\u9053","fqd_channel":"FQD\u5206\u6790\u5e08\u9891\u9053","guide_channel":"\u4f7f\u7528\u6307\u5357\u9891\u9053","subscriptions_channel":"\u5206\u6790\u5e08\u8ba2\u9605\u9762\u677f\u9891\u9053","feishu_app_id":"\u98de\u4e66 App ID","flash_model":"DeepSeek\u65e5\u5e38\u6a21\u578b","pro_model":"DeepSeek\u590d\u6838\u6a21\u578b","report_delay":"\u6536\u76d8\u540e\u65e5\u62a5\u5ef6\u8fdf\uff08\u5206\u949f\uff09"}
        data = self.app.store.load()
        inputs = []
        for field in FIELDS:
            value = _get_path(data, field.config_path, "")
            kind = "number" if field.form_name == "report_delay" else "text"
            inputs.append(
                f'<label for="{field.form_name}">{html.escape(display_labels.get(field.form_name, field.label))}</label>'
                f'<input id="{field.form_name}" name="{field.form_name}" type="{kind}" value="{html.escape(str(value))}" required>'
            )
        statuses = self.app.store.secret_presence()
        secret_inputs = []
        for form_name, (label, _ref_name) in SECRET_FORM_FIELDS.items():
            placeholder = "已保存，留空保持原值" if statuses.get(label) else "粘贴到这里"
            secret_inputs.append(
                f'<label for="{form_name}">{html.escape(label)}</label><input id="{form_name}" name="{form_name}" '
                f'type="password" autocomplete="new-password" placeholder="{placeholder}">'
            )
        status_html = "".join(
            f'<span>{html.escape(label)}</span><strong>{"已保存" if present else "待配置"}</strong>'
            for label, present in statuses.items()
        )
        notice = f'<p class="{"error" if ("出错" in message or "失败" in message) else "ok"}">{html.escape(message)}</p>' if message else ""
        qr_path = Path(os.getenv("DATA_DIR", "/data")) / "evidence" / "discord-login.png"
        qr_html = '<h2>Discord扫码登录</h2><img src="/discord-login.png" alt="Discord登录二维码" style="max-width:360px;width:100%">' if qr_path.is_file() else ""
        captcha_path = Path(os.getenv("DATA_DIR", "/data")) / "opend-profile" / ".com.futunn.FutuOpenD" / "F3CNN" / "PicVerifyCode.png"
        captcha_html = '<h3>富途图形验证码</h3><img src="/futu-captcha.png" alt="富途图形验证码" style="max-width:360px;width:100%">' if captcha_path.is_file() else ""
        local = os.getenv("OPTIONS_RADAR_LOCAL") == "1"
        location = "本机的 <code>data-local/secrets</code>" if local else "NAS的 <code>/data/secrets</code>"
        content = (
            f'<h1>一次性配置</h1><p class="sub">密钥只保存在{location}；交易解锁保持关闭。</p>'
            '<div id="action-result"></div>'
            f'{notice}<div class="card"><div class="status">{status_html}</div></div>'
            '<form method="post" action="/save"><input type="hidden" name="csrf" value="' + html.escape(csrf) + '">'
            '<div class="grid"><section class="card"><h2>基础配置</h2>' + "".join(inputs) + '</section>'
            '<section class="card"><h2>API配置</h2>' + "".join(secret_inputs) + '</section></div>'
            '<button>保存并启用</button></form>'
            '<section class="card"><h2>Discord登录</h2><p>点击后会打开独立Discord浏览器窗口；登录资料保存在本机专用目录。</p>'
            + self._action_forms(csrf, (("/api/actions/discord-login", "打开Discord登录"),))
            + qr_html + '</section>'
            '<section class="card"><h2>\u5bcc\u9014\u81ea\u9009\u8fc1\u79fb</h2><p>V2\u65e5\u5e38\u884c\u60c5\u4e0e\u6301\u4ed3\u4f7f\u7528IBKR\u3002\u5bcc\u9014OpenD\u4ec5\u5728\u9700\u8981\u65f6\u5bfc\u5165\u4e00\u6b21\u81ea\u9009\uff0c\u4e0d\u5728\u6b64\u9875\u9762\u4fdd\u5b58\u767b\u5f55\u5bc6\u7801\u6216\u9a8c\u8bc1\u7801\u3002</p>'
            + self._action_forms(csrf, (("/futu/import-watchlist", "\u4ece\u5bcc\u9014\u5bfc\u5165\u81ea\u9009"),))
            + '<p class="muted">\u8bf7\u5148\u5728\u672c\u673a\u542f\u52a8\u5e76\u767b\u5f55\u5bcc\u9014OpenD\uff0c\u5bfc\u5165\u5b8c\u6210\u540e\u5373\u53ef\u5173\u95edOpenD\u3002</p></section>'
            f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><button class="secondary">退出登录</button></form>'
        )
        return self._shell("本地配置" if local else "本地配置 / NAS部署", content, "/setup")


def create_setup_server(
    host: str,
    port: int,
    store: SetupConfigStore,
    token: str,
    health: Optional[Callable[[], Mapping[str, Any]]] = None,
    callbacks: Optional[Mapping[str, DashboardCallback]] = None,
    local_mode: bool = False,
) -> ThreadingHTTPServer:
    app = SetupApplication(store, token, health=health, callbacks=callbacks, local_mode=local_mode)
    server = ThreadingHTTPServer((host, port), SetupRequestHandler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    return server


def setup_token_from_environment() -> str:
    return _read_secret("SETUP_TOKEN_FILE", "SETUP_TOKEN")
