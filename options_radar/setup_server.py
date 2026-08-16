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
            "fpd": "fpd分析师",
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
        "market_priority": ["futu", "alpaca", "massive"],
        "enabled": {"futu": True, "alpaca": True, "massive": False},
        "portfolio": {"aggregate_enabled_accounts": True},
        "execution": {"accepted_quality": ["realtime"], "max_quote_age_seconds": 60,
                      "conflict_threshold_pct": 15},
    },
    "market": {"provider": "futu"},
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
        "massive_api_key": "/data/secrets/massive_api_key",
        "alpaca_api_key": "/data/secrets/alpaca_api_key",
        "alpaca_api_secret": "/data/secrets/alpaca_api_secret",
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
    "Discord 用户 Token": "DISCORD_USER_TOKEN_FILE",
}

SECRET_FORM_FIELDS = {
    "deepseek_api_key": ("DeepSeek API Key", "deepseek_api_key"),
    "feishu_app_secret": ("飞书 App Secret", "feishu_app_secret"),
    "feishu_webhook": ("飞书 Webhook URL", "feishu_webhook"),
    "massive_api_key": ("Massive API Key", "massive_api_key"),
    "alpaca_api_key": ("Alpaca API Key", "alpaca_api_key"),
    "alpaca_api_secret": ("Alpaca API Secret", "alpaca_api_secret"),
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
    Field("pa_channel", "discord.channel_names.pa", "PA分析师频道", _plain),
    Field("mr_channel", "discord.channel_names.mr", "MR分析师频道", _plain),
    Field("qmr_channel", "discord.channel_names.qmr", "QMR分析师频道", _plain),
    Field("fpd_channel", "discord.channel_names.fpd", "FPD分析师频道", _plain),
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
                for source in ("flow", "pa", "mr", "qmr", "fpd")
                if source in channel_names
            }
            data["market"]["provider"] = "futu"
            data["providers"]["market_priority"] = ["futu", "alpaca", "massive"]
            data["providers"]["enabled"]["futu"] = True
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
    "/system": ("系统诊断", "status", "富途 OpenD、Discord、DeepSeek、飞书与持仓同步状态"),
    "/setup": ("设置", "status", "首次配置和连接测试"),
    "/providers": ("数据源诊断", "providers", "富途主源、Alpaca/Massive 历史复核"),
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
:root{{--ink:#1d1d1f;--muted:#86868b;--bg:#f5f5f7;--card:#fff;--line:#e8e8ed;--up:#d70015;--down:#00a651;--accent:#0071e3}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Microsoft YaHei",sans-serif;font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}}
header{{position:sticky;top:0;z-index:10;background:rgba(245,245,247,.82);backdrop-filter:saturate(180%) blur(20px);border-bottom:1px solid var(--line);padding:12px 24px;display:flex;align-items:center;gap:20px;flex-wrap:wrap}}
.logo{{font-size:17px;font-weight:700;letter-spacing:-.01em;white-space:nowrap}}header small{{color:var(--muted);font-size:12px}}
nav{{display:flex;gap:2px;margin-left:auto}}nav a{{color:var(--ink);text-decoration:none;padding:6px 12px;border-radius:20px;font-size:13px;white-space:nowrap}}nav a:hover{{background:#e5e5ea}}nav a.active{{background:var(--ink);color:#fff}}
main{{max-width:1280px;margin:0 auto;padding:28px 20px 60px}}
h1{{font-size:28px;font-weight:700;letter-spacing:-.02em;margin:0 0 4px}}h2{{font-size:20px;font-weight:600;margin:26px 0 10px}}.sub{{color:var(--muted);margin:0 0 18px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}}
.table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}} .table-wrap table{{min-width:640px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
.metric{{font-size:26px;font-weight:700;letter-spacing:-.02em}}
.muted{{color:var(--muted)}}small{{color:var(--muted)}}
label{{display:block;margin:13px 0 5px;font-weight:600}}input{{width:100%;padding:10px 12px;border:1px solid #d2d2d7;border-radius:10px;font-size:14px}}
button,.button{{display:inline-block;margin:9px 6px 0 0;padding:9px 18px;border:0;border-radius:20px;background:var(--accent);color:#fff;font-weight:600;font-size:14px;text-decoration:none;cursor:pointer}}button:hover{{opacity:.88}}button.secondary{{background:#e5e5ea;color:var(--ink)}}
.ok,.error{{padding:10px 14px;border-radius:10px}} .ok{{background:#e9f9ef}} .error{{background:#fdecec}}
pre{{white-space:pre-wrap;word-break:break-word;background:#f0f0f2;color:#3a3a3c;padding:14px;border-radius:12px;max-height:520px;overflow:auto;font-size:12px}}code{{background:#eef0f3;padding:2px 6px;border-radius:5px}}
.status{{display:grid;grid-template-columns:1fr auto;gap:8px}}
table{{width:100%;border-collapse:collapse}}th{{color:var(--muted);font-weight:500;text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}th,td{{padding:11px 12px;border-bottom:1px solid var(--line)}}tr:hover td{{background:#fafafa}}
summary{{cursor:pointer;font-weight:600}}
.badge{{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600}}.badge.up{{background:#fdecec;color:#d70015}}.badge.down{{background:#e9f9ef;color:#00a651}}.badge.watch{{background:#eef2ff;color:#0037c1}}
ul.analyst-votes{{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-wrap:wrap;gap:6px}}ul.analyst-votes li{{background:#f0f0f2;border:1px solid var(--line);border-radius:8px;padding:4px 10px;font-size:13px}}
.market-banner{{background:#eef2ff;color:#0037c1;border:1px solid #d6e0f5;border-radius:10px;padding:8px 14px;font-size:13px;margin-bottom:14px}}
.risk-inline{{margin:6px 0 0;font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:6px;align-items:center}}.risk-inline strong{{color:#b25000;font-size:12px}}.risk-chip{{background:#fff8f0;border:1px solid #f0dcc8;color:#8a5a2b;border-radius:6px;padding:2px 8px;font-size:11.5px;white-space:nowrap}}
@media(max-width:900px){{.grid{{grid-template-columns:repeat(auto-fill,minmax(200px,1fr))}}h1{{font-size:24px}}}}
@media(max-width:640px){{header{{padding:10px 14px}}nav{{width:100%;margin-left:0;overflow-x:auto}}h1{{font-size:22px}}.grid{{grid-template-columns:1fr}}main{{padding:16px 12px 48px}}.card{{padding:14px}}}}
</style></head><body><header><span class="logo">Options Radar</span><small>版本 {html.escape(BUILD_VERSION)} · {html.escape(BUILD_SHA[:12])}</small><nav>{nav}</nav></header><main>{content}</main>
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
function attachTableFilters(opts) {{
  const search=document.getElementById(opts.search);
  const filters=[];
  for (const id of (opts.selects||[])) {{
    const el=document.getElementById(id); if (el) filters.push(el);
  }}
  const tables=[];
  document.querySelectorAll('tr[data-filter]').forEach(function(row){{
    if (!tables.includes(row.closest('table'))) tables.push(row.closest('table'));
  }});
  function apply(){{
    const q=search?search.value.trim().toLowerCase():'';
    const tokens={{}};
    filters.forEach(function(el){{ tokens[el.getAttribute('data-key')||el.id]=el.value; }});
    document.querySelectorAll('tr[data-filter]').forEach(function(row){{
      const text=row.getAttribute('data-filter').toLowerCase();
      let ok=!q||text.indexOf(q)>=0;
      filters.forEach(function(el){{
        const v=el.value; if (v&&v!=='all'&&text.indexOf(v)<0) ok=false;
      }});
      row.style.display=ok?'':'none';
      const next=row.nextElementSibling;
      if (next&&(next.classList.contains('signal-detail')||next.classList.contains('flow-detail'))) {{
        next.style.display=ok?'':'none';
      }}
    }});
  }}
  if (search) search.addEventListener('input',apply);
  filters.forEach(function(el){{ el.addEventListener('change',apply); }});
  const expand=document.getElementById(opts.expand);
  const collapse=document.getElementById(opts.collapse);
  if (expand) expand.addEventListener('click',function(){{
    document.querySelectorAll('details').forEach(function(d){{ d.setAttribute('open',''); }});
  }});
  if (collapse) collapse.addEventListener('click',function(){{
    document.querySelectorAll('details').forEach(function(d){{ d.removeAttribute('open'); }});
  }});
}}
attachTableFilters({{search:'signal-search',selects:['signal-dir','signal-status'],expand:'signal-expand',collapse:'signal-collapse'}});
attachTableFilters({{search:'portfolio-search',selects:['portfolio-hold'],expand:'portfolio-expand',collapse:'portfolio-collapse'}});
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
                return '<section class="card"><h2>今日暂无候选</h2><p class="muted">点击“立即采集并生成推荐”，或等待新的异常期权事件。</p></section>'
            from options_radar.timeutil import us_cash_session_label
            session_label = us_cash_session_label()
            market_banner = ""
            if session_label != "交易中":
                market_banner = (
                    f'<div class="market-banner">当前<b>{html.escape(session_label)}</b>，'
                    '行情为最近收盘快照，bid/ask 与 OI 暂不新鲜，开市后自动刷新为实时数据。</div>'
                )
            cards = []
            def format_price(value: Any) -> str:
                if value is None:
                    return "待行情"
                try:
                    return f"{float(value):.2f}"
                except (TypeError, ValueError):
                    return "待行情"
            def format_premium(value: Any) -> str:
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
            for item in data[:10]:
                key = html.escape(str(item.get("contract_key", "")))
                bid = format_price(item.get("bid"))
                ask = format_price(item.get("ask"))
                entry = format_price(item.get("max_entry_price"))
                take_profit = format_price(item.get("take_profit"))
                stop_loss = format_price(item.get("stop_loss"))
                direction = str(item.get("direction", "-"))
                badge_cls = "up" if direction in {"BULL", "看多", "多"} else "down" if direction in {"BEAR", "看空", "空"} else "watch"
                badge = f'<span class="badge {badge_cls}">{html.escape(direction)}</span>'
                flow_only = str(item.get("data_quality", "")).startswith("仅") or item.get("analyst_count") is not None
                source_badge = '<span class="badge watch">仅flow</span>' if flow_only else f'<span class="badge watch">分析师{n_html if (n_html := html.escape(str(item.get("analyst_count", ""))) if item.get("analyst_count") is not None else "") else ""}</span>'
                score = item.get("score")
                score_html = f'{float(score):.1f}' if isinstance(score, (int, float)) else html.escape(str(score))
                grade = html.escape(str(item.get("grade", "-")))
                premium_text = format_premium(item.get("premium"))
                data_quality = str(item.get("data_quality", item.get("market_status", "待行情")))
                # 休市时行情时间戳必然过期，直接标注休市而不是 missing。
                market_state = "休市"
                if data_quality in {"native", "realtime", "ok"}:
                    market_state = "实时"
                elif data_quality in {"仅flow", "flow"}:
                    market_state = "待采集"
                quote_line = f'bid/ask {bid} / {ask} · 数据 {market_state} · 执行 {html.escape(str(item.get("execution_status", "待行情")))}'
                # 分析师判断拆成可读的多行，而不是逗号拼接的方块字。
                reason = str(item.get("reason", ""))
                vote_segment = ""
                if "分析师判断" in reason:
                    vote_part = reason.split("评分组成", 1)[0].replace("分析师判断：", "").strip()
                    vote_lines = []
                    for vote in [part.strip().rstrip("；") for part in vote_part.split("、") if part.strip()]:
                        vote_lines.append(f'<li>{html.escape(vote)}</li>')
                    if vote_lines:
                        vote_segment = '<ul class="analyst-votes">' + "".join(vote_lines) + '</ul>'
                # 风险提示：休市类统一在页面顶部备注，这里只保留具体合约风险。
                # 休市/过期/数据源延迟属于全市场状态，不是该合约独有。
                HOLDING_PATTERNS = ("行情时间戳已过期", "盘口与OI为过期数据", "暂未取到实时行情", "等待数据源恢复")
                risk_segment = ""
                if "风险提示" in reason:
                    risks = reason.split("风险提示：", 1)[1].split("；执行字段", 1)[0].split("；")
                    real_risks = [r.strip() for r in risks if r.strip() and not any(
                        pattern in r for pattern in HOLDING_PATTERNS
                    )]
                    if real_risks:
                        risk_segment = '<div class="risk-inline"><strong>注意</strong>' + "".join(
                            f'<span class="risk-chip">{html.escape(r)}</span>' for r in real_risks
                        ) + '</div>'
                remaining = ""
                if "评分组成" in reason:
                    remaining = "评分组成：" + reason.split("评分组成：", 1)[1].split("风险提示", 1)[0]
                cards.append(
                    '<section class="card">'
                    f'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><h2 style="margin:0;font-size:18px">{key}</h2><span class="muted" style="font-size:13px">交易量 {premium_text}</span>{badge}{source_badge}</div>'
                    f'<div style="margin:8px 0 4px"><span style="font-size:24px;font-weight:700">{score_html}</span>'
                    f'<span class="muted" style="margin-left:8px">{grade}级</span></div>'
                    f'<p style="margin:2px 0" class="muted">{quote_line}</p>'
                    f'{vote_segment}'
                    f'<p style="margin:2px 0" class="muted">入场 {entry} · 止盈 {take_profit} · 止损 {stop_loss}</p>'
                    f'{risk_segment}'
                    f'<p style="margin:6px 0 0" class="muted">{html.escape(remaining) if remaining else ""}</p>'
                    '</section>'
                )
            return market_banner + '<div class="grid">' + "".join(cards) + '</div><details class="card" style="margin-top:16px"><summary>评分体系说明（参考国际常用期权分析框架）</summary><p>候选从 Discord 异常期权/分析师频道采集（解析成交额、持仓、分析师卡片），再叠加以下五维评分。每个维度都用<strong>可验证的数据</strong>计算，不是主观打分：</p><table><tr><th>维度</th><th>权重</th><th>依据</th></tr><tr><td>方向共识</td><td>40%</td><td>四个分析家族（价格行为/均值回归/量化均值回归/流价背离）独立观点的方向与决策（TRADE/WATCH/NO_TRADE）加权一致度；家族间方向冲突直接扣分</td></tr><tr><td>历史胜率</td><td>20%</td><td>各分析师历史推荐的模拟盘盈亏（1/3/5日结算）动态校准权重，类似国际组合的“因子回测”权重</td></tr><tr><td>信号完整度</td><td>15%</td><td>分析师卡片是否给出方向、置信度、入场/止盈/止损、理由等字段的完整程度</td></tr><tr><td>行情质量</td><td>15%</td><td>富途实时 bid/ask、买卖价差、Open Interest、成交量、隐含波动率（IV）——对应流动性检验（类似 CBOE 盘口校验）</td></tr><tr><td>组合适配</td><td>10%</td><td>标的是否已在持仓/自选、仓位集中度是否过高——对应风控的集中度限制</td></tr></table><p>评级：A≥80（飞书提醒）｜B 65-79（合格）｜C 50-64（观察榜）｜D&lt;50（过滤）。仅flow=异常期权事件但分析师尚未确认。</p><p class="muted">数据来源：Discord 频道文本（成交额/分析师观点）、富途 OpenD 实时行情（盘口/OI/IV/希腊字母）、Alpaca/Massive 历史行情（回测）、DeepSeek 仅做翻译与文字整理，不参与任何数值决策。</p></details>'
        if path == "/signals" and isinstance(data, list):
            def format_premium(value: Any) -> str:
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

            family_names = {
                "price_action": "价格行为", "mean_reversion": "均值回归",
                "quant_mean_reversion": "量化均值回归", "flow_price_divergence": "流价背离",
                "pa": "价格行为", "mr": "均值回归",
                "qmr": "量化均值回归", "fpd": "流价背离",
            }
            decision_names = {"TRADE": "交易", "WATCH": "观察", "NO_TRADE": "不交易"}
            rows = []
            for item in data:
                signals = item.get("signals", [])
                n = len(signals)
                premium_text = format_premium(item.get("premium"))
                bull_count = sum(1 for signal in signals if str(signal.get("direction", "")).upper() == "BULL")
                bear_count = sum(1 for signal in signals if str(signal.get("direction", "")).upper() == "BEAR")
                if signals and bull_count > bear_count:
                    direction_html = '<span class="badge up">看多</span>'
                elif signals and bear_count > bull_count:
                    direction_html = '<span class="badge down">看空</span>'
                elif signals:
                    direction_html = '<span class="badge watch">中性</span>'
                else:
                    direction_html = '<span class="badge watch">—</span>'
                badge = f'<span class="badge watch">分析师{n}</span>' if n else '<span class="badge watch">仅flow</span>'
                detail_rows = []
                for signal in signals:
                    family = str(signal.get("analyst_family", "") or signal.get("analyst", ""))
                    family_name = family_names.get(family, html.escape(str(family or signal.get("analyst", ""))))
                    direction = str(signal.get("direction", "")).upper()
                    if direction == "BULL":
                        dir_html = '<span class="badge up">看多</span>'
                    elif direction == "BEAR":
                        dir_html = '<span class="badge down">看空</span>'
                    elif direction == "NEUTRAL":
                        dir_html = '<span class="badge watch">中性</span>'
                    else:
                        dir_html = '<span class="badge watch">未知</span>'
                    decision = decision_names.get(str(signal.get("decision", "")).upper(), str(signal.get("decision", "")))
                    confidence = signal.get("confidence")
                    confidence_text = f"{float(confidence) * 100:.0f}%" if isinstance(confidence, (int, float)) else ""
                    rationale = signal.get("rationale") or []
                    reason = html.escape(str(rationale[0])) if rationale else ""
                    detail_rows.append(
                        f'<tr><td>{family_name}</td><td>{dir_html}</td>'
                        f'<td>{html.escape(str(decision))}</td><td>{confidence_text}</td>'
                        f'<td class="muted">{reason}</td></tr>'
                    )
                if detail_rows:
                    detail_html = (
                        '<details class="card" style="margin-top:6px"><summary>分析师明细</summary>'
                        '<table><tr><th>分析家族</th><th>方向</th><th>决策</th><th>置信度</th><th>理由</th></tr>'
                        + "".join(detail_rows) + '</table></details>'
                    )
                else:
                    detail_html = '<p class="muted" style="margin:6px 0 0">暂无分析师解读</p>'
                direction_token = "bull" if (signals and bull_count > bear_count) else "bear" if (signals and bear_count > bull_count) else "neutral" if signals else "none"
                status_token = "has-analyst" if n else "flow-only"
                filter_text = "|".join([
                    str(item.get("symbol", "")).lower(), str(item.get("contract_key", "")).lower(),
                    direction_token, status_token,
                ])
                rows.append(
                    '<tr data-filter="{0}"><td>{1}</td><td>{2}</td><td class="muted">{3}</td><td class="muted">{4}</td>'
                    '<td>{5}</td><td>{6}</td></tr>'
                    '<tr class="signal-detail"><td colspan="6">{7}</td></tr>'.format(
                        html.escape(filter_text),
                        html.escape(str(item.get("symbol", ""))),
                        html.escape(str(item.get("contract_key", ""))),
                        premium_text,
                        html.escape(str(item.get("observed_at", ""))),
                        direction_html,
                        badge,
                        detail_html,
                    )
                )
            return (
                '<div class="card toolbar">'
                '<input type="search" id="signal-search" placeholder="搜索标的或合约…" style="max-width:320px;display:inline-block">'
                '<select id="signal-dir" style="width:auto;display:inline-block;margin-left:8px">'
                '<option value="all">方向：全部</option><option value="bull">看多</option>'
                '<option value="bear">看空</option><option value="neutral">中性</option></select>'
                '<select id="signal-status" style="width:auto;display:inline-block;margin-left:8px">'
                '<option value="all">状态：全部</option><option value="has-analyst">分析师确认</option>'
                '<option value="flow-only">仅flow</option></select>'
                '<button type="button" class="secondary" id="signal-expand">全部展开</button>'
                '<button type="button" class="secondary" id="signal-collapse">全部折叠</button>'
                '</div>'
                '<section class="card" id="signal-table"><div class="table-wrap"><table>'
                '<tr><th>标的</th><th>合约</th><th>交易量</th><th>时间</th><th>方向</th><th>状态</th></tr>'
                + "".join(rows) + '</table></div></section>'
            )
        if path == "/portfolio" and isinstance(data, dict):
            def render_row(symbol, item):
                if not isinstance(item, Mapping):
                    return f'<tr><td>{html.escape(str(symbol))}</td><td colspan="8">{html.escape(str(item))}</td></tr>'
                name = html.escape(str(item.get("company_name", "") or ""))
                name_zh = html.escape(str(item.get("company_name_zh", "") or ""))
                group = html.escape(str(item.get("group_name", "") or ""))
                industry = group or html.escape(str(item.get("industry", "") or ""))
                price = item.get("current_price")
                change = item.get("change_pct")
                updated = html.escape(str((item.get("updated_at") or "")[:19]))
                price_str = f"{float(price):.2f}" if price is not None else "-"
                change_cls = ""
                change_str = "-"
                if change is not None:
                    change_cls = "up" if float(change) >= 0 else "down"
                    change_str = f"{float(change)*100:+.2f}%"
                display_name = f"{name_zh}（{name}）" if name_zh else name
                flag = '<span class="badge up">异常期权</span>' if item.get("has_flow") else ""
                held = float(item.get("held_quantity", 0) or 0)
                held_token = "held" if held else "no-hold"
                filter_text = "|".join([
                    str(symbol).lower(), str(item.get("company_name", "") or "").lower(),
                    str(item.get("group_name", "") or "").lower(), held_token,
                ])
                row = '<tr data-filter="{0}"><td>{1}{2}</td><td class="muted">{3}</td><td class="muted">{4}</td><td>{5}</td><td>{6}</td><td class="{7}">{8}</td><td>{9}</td><td>{10}</td></tr>'.format(
                    html.escape(filter_text),
                    html.escape(str(symbol)), flag, display_name, industry,
                    html.escape(str(item.get("held_quantity", 0))),
                    price_str, change_cls, change_str, float(item.get("concentration", 0) or 0), updated,
                )
                contracts = item.get("flow_contracts") or []
                if contracts:
                    detail = "".join(f'<li>{html.escape(str(c))}</li>' for c in contracts)
                    row += f'<tr class="flow-detail"><td colspan="8"><details><summary>相关异常期权事件</summary><ul>{detail}</ul></details></td></tr>'
                return row
            items = list(data.items())
            flow_rows = [render_row(s, v) for s, v in items if isinstance(v, Mapping) and v.get("has_flow")]
            other_rows = [render_row(s, v) for s, v in items if not (isinstance(v, Mapping) and v.get("has_flow"))]
            toolbar = (
                '<div class="card toolbar">'
                '<input type="search" id="portfolio-search" placeholder="搜索标的或公司名…" style="max-width:320px;display:inline-block">'
                '<select id="portfolio-hold" style="width:auto;display:inline-block;margin-left:8px">'
                '<option value="all">持仓：全部</option><option value="held">有持仓</option>'
                '<option value="no-hold">无持仓</option></select>'
                '<button type="button" class="secondary" id="portfolio-expand">全部展开</button>'
                '<button type="button" class="secondary" id="portfolio-collapse">全部折叠</button>'
                '</div>'
            )
            sections = [toolbar]
            if flow_rows:
                sections.append('<section class="card" id="portfolio-table"><h2>异常期权相关</h2><div class="table-wrap"><table><tr><th>标的</th><th>公司名称</th><th>分组</th><th>持仓</th><th>最新价</th><th>涨跌</th><th>集中度</th><th>更新</th></tr>' + "".join(flow_rows) + '</table></div></section>')
            sections.append('<section class="card" id="portfolio-table"><h2>全部自选</h2><div class="table-wrap"><table><tr><th>标的</th><th>公司名称</th><th>分组</th><th>持仓</th><th>最新价</th><th>涨跌</th><th>集中度</th><th>更新</th></tr>' + ("".join(other_rows) or '<tr><td colspan="8">暂无组合快照</td></tr>') + '</table></div></section>')
            return "".join(sections)
        if path == "/backtest" and isinstance(data, dict):
            replay = data.get("replay") or {}
            ranges = data.get("historical_range") or {}
            paper = data.get("paper") or {}
            breakdown = data.get("analyst_breakdown") if isinstance(data.get("analyst_breakdown"), list) else []
            parts = []
            if ranges.get("start"):
                filled = replay.get("filled", 0)
                no_fill = replay.get("no_fill", 0)
                avg = round(float(replay.get("avg_net_return", 0) or 0) * 100, 2)
                dd = round(float(replay.get("max_drawdown", 0) or 0) * 100, 2)
                total = filled + no_fill
                win_rate = "—"
                if filled:
                    wins = sum(1 for item in (replay.get("outcomes") or []) if item.get("status") == "filled" and float(item.get("pnl_pct") or 0) > 0)
                    win_rate = f"{round(wins / filled * 100, 1)}%"
                parts.append((
                    '<section class="card"><h2>共识推荐回放（{} ~ {}，{} 个交易日）</h2>'
                    '<div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(150px,1fr))">'
                    f'<div class="card"><div class="muted">结算笔数</div><div class="metric">{filled}</div><div class="muted">未成交 {no_fill}</div></div>'
                    f'<div class="card"><div class="muted">胜率</div><div class="metric">{html.escape(win_rate)}</div></div>'
                    f'<div class="card"><div class="muted">平均收益率</div><div class="metric">{avg}%</div></div>'
                    f'<div class="card"><div class="muted">最大回撤</div><div class="metric">{dd}%</div></div>'
                    '</div><p class="muted">基于共识推荐记录（推荐按合约覆盖式更新，仅保留最近交易日）；5 日结算需交易日满 5 天，样本随运行自动积累。</p></section>'
                ).format(html.escape(str(ranges["start"])), html.escape(str(ranges["end"])), data.get("sessions_available", 0)))
                outcomes = replay.get("outcomes") or []
                if outcomes:
                    exit_names = {
                        "stop-loss": "止损", "take-profit": "止盈", "holding-limit": "持有到期",
                        "no-complete-bar": "无K线", "limit-exceeded": "超限价", "no-fill": "未成交",
                    }
                    detail_rows = []
                    for item in outcomes:
                        pnl = float(item.get("pnl_pct") or 0.0) if item.get("status") == "filled" else None
                        pnl_html = "—"
                        if pnl is not None:
                            cls = "up" if pnl >= 0 else "down"
                            pnl_html = f'<span class="{cls}">{pnl * 100:+.2f}%</span>'
                        status_label = {"filled": "成交", "no-fill": "未成交"}.get(str(item.get("status")), str(item.get("status")))
                        exit_label = exit_names.get(str(item.get("exit_reason")), str(item.get("exit_reason") or "—"))
                        detail_rows.append(
                            '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
                                html.escape(str(item.get("session_date", ""))),
                                html.escape(str(item.get("contract_key", ""))),
                                item.get("horizon_days", ""),
                                html.escape(status_label),
                                pnl_html,
                                html.escape(exit_label),
                            )
                        )
                    parts.append(
                        '<section class="card"><h2>逐笔结算明细</h2><div class="table-wrap"><table>'
                        '<tr><th>交易日</th><th>合约</th><th>持有(日)</th><th>状态</th><th>收益率</th><th>退出</th></tr>'
                        + "".join(detail_rows) + '</table></div></section>'
                    )
            accuracy = data.get("analyst_accuracy") or {}
            acc_summary = accuracy.get("summary") if isinstance(accuracy.get("summary"), list) else []
            if acc_summary:
                acc_rows = []
                for item in acc_summary:
                    trades = int(item.get("trades", 0) or 0)
                    filled = int(item.get("filled", 0) or 0)
                    direction_hits = int(item.get("direction_hits", 0) or 0)
                    strategy_wins = int(item.get("strategy_wins", 0) or 0)
                    direction_rate = f"{direction_hits / trades * 100:.1f}%" if trades else "—"
                    strategy_rate = f"{strategy_wins / filled * 100:.1f}%" if filled else "—"
                    avg_pnl = float(item.get("avg_pnl", 0) or 0)
                    avg_stock = float(item.get("avg_stock_pnl", 0) or 0)
                    pnl_cls = "up" if avg_pnl >= 0 else "down"
                    stock_cls = "up" if avg_stock >= 0 else "down"
                    acc_rows.append(
                        '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td class="{}">{:+.2f}%</td><td>{}</td><td class="{}">{:+.2f}%</td></tr>'.format(
                            html.escape(str(item.get("analyst", ""))),
                            item.get("horizon_days", ""),
                            trades,
                            direction_rate,
                            stock_cls,
                            avg_stock * 100,
                            strategy_rate,
                            pnl_cls,
                            avg_pnl * 100,
                        )
                    )
                parts.append(
                    '<section class="card"><h2>分析师信号回测（TRADE 信号，双口径）</h2>'
                    '<div class="table-wrap"><table>'
                    '<tr><th>分析师</th><th>持有(日)</th><th>信号数</th><th>方向正确率</th><th>正股盈亏</th><th>卖方策略胜率</th><th>卖方平均盈亏</th></tr>'
                    + "".join(acc_rows) + '</table></div>'
                    '<p class="muted">每行 = 分析师 × 持有期。方向正确率基于正股涨跌；正股策略 = BULL 次日买正股持 N 日 / BEAR 空仓；'
                    '卖方策略 = BULL 卖平值 PUT / BEAR 卖平值 CALL（次日开盘成交，3% 滑点，无止盈，权利金涨 50% 止损，收益率按 25% 保证金口径）。'
                    '四个分析家族中当前有 TRADE 信号的都会纳入。</p></section>'
                )
            elif accuracy.get("running"):
                parts.append('<section class="card"><h2>分析师信号回测</h2><p class="muted">正在回测 TRADE 信号（拉取期权历史行情），稍后刷新查看。</p></section>')

            daily = accuracy.get("daily") if isinstance(accuracy.get("daily"), list) else []
            if daily:
                cal_rows = []
                for item in daily:
                    signals = int(item.get("signals", 0) or 0)
                    direction_rated = int(item.get("direction_rated", 0) or 0)
                    direction_hits = int(item.get("direction_hits", 0) or 0)
                    filled = int(item.get("filled", 0) or 0)
                    strategy_wins = int(item.get("strategy_wins", 0) or 0)
                    direction_rate = f"{direction_hits / direction_rated * 100:.0f}%" if direction_rated else "—"
                    strategy_rate = f"{strategy_wins / filled * 100:.0f}%" if filled else "—"
                    avg_pnl = float(item.get("avg_pnl", 0) or 0)
                    avg_stock = float(item.get("avg_stock_pnl", 0) or 0)
                    pnl_cls = "up" if avg_pnl >= 0 else "down"
                    stock_cls = "up" if avg_stock >= 0 else "down"
                    cal_rows.append(
                        '<tr><td>{}</td><td>{}</td><td>{}</td><td class="{}">{:+.2f}%</td><td>{}</td><td class="{}">{:+.2f}%</td></tr>'.format(
                            html.escape(str(item.get("session_date", ""))),
                            signals,
                            direction_rate,
                            stock_cls,
                            avg_stock * 100,
                            strategy_rate,
                            pnl_cls,
                            avg_pnl * 100,
                        )
                    )
                parts.append(
                    '<section class="card"><h2>信号日历（5 日结算，双口径）</h2><div class="table-wrap"><table>'
                    '<tr><th>交易日</th><th>信号数</th><th>方向正确率</th><th>正股盈亏</th><th>卖方策略胜率</th><th>卖方平均盈亏</th></tr>'
                    + "".join(cal_rows) + '</table></div>'
                    '<p class="muted">按信号产生日聚合；正股 = BULL 买正股 / BEAR 空仓，卖方 = 卖平值期权（25% 保证金口径）。</p></section>'
                )

            if breakdown:
                br_rows = "".join(
                    '<tr><td>{}</td><td>{}</td><td>{}</td><td>{:.1f}%</td><td>{:+.2f}%</td></tr>'.format(
                        html.escape(str(item.get("analyst", ""))),
                        item.get("trades", 0), item.get("wins", 0),
                        float(item.get("win_rate", 0) or 0) * 100,
                        float(item.get("avg_return", 0) or 0) * 100,
                    ) for item in breakdown
                )
                parts.append(
                    '<section class="card"><h2>共识推荐分析师胜率（5日结算）</h2><div class="table-wrap"><table>'
                    '<tr><th>分析师</th><th>成交笔数</th><th>盈利笔数</th><th>胜率</th><th>平均收益</th></tr>'
                    + br_rows + '</table></div>'
                    '<p class="muted">基于共识推荐中的分析师投票（覆盖式更新，仅最近交易日）。</p></section>'
                )
            if paper:
                parts.append(f'<section class="card"><h2>模拟交易统计</h2><p>平仓: {paper.get("closed",0)} 笔　胜率: {round(paper.get("win_rate",0)*100,1)}%　损益: ${paper.get("realized_pnl",0):,.2f}</p></section>')
            return "".join(parts) if parts else '<section class="card"><p>暂无回测数据。等待系统积累足够交易日后再查看。</p></section>'
        if path == "/system" and isinstance(data, dict):
            from options_radar.timeutil import us_cash_session_label
            parts = []
            overall = str(data.get("status", "unknown"))
            session_label = us_cash_session_label()
            parts.append(
                '<section class="card"><div class="status">'
                f'<div>系统状态</div><div class="metric">{html.escape(overall)}</div>'
                f'<div>美股时段</div><div class="metric">{html.escape(session_label)}</div>'
                f'<div>最近采集</div><div>{html.escape(str((data.get("last_collection") or "从未采集")[:19]))}</div>'
                f'<div>最近同步</div><div>{html.escape(str((data.get("last_sync") or "从未同步")[:19]))}</div>'
                '</div></section>'
            )
            if data.get("last_error"):
                parts.append(f'<section class="card error"><h2>最近错误</h2><p>{html.escape(str(data["last_error"]))}</p></section>')
            def provider_card(name, label, value):
                if not isinstance(value, dict):
                    state = "未知"
                else:
                    note = str(value.get("note", "") or "")
                    if note:
                        state = str(value.get("status", "未知")) + "（" + note + "）"
                    else:
                        state = str(value.get("status", value.get("connected", "未知")))
                        quality = str(value.get("quality", ""))
                        if quality and quality not in ("missing", "unknown"):
                            state = f"{state}（{quality}）"
                return f'<section class="card"><h2>{label}</h2><div class="metric">{html.escape(state)}</div></section>'
            grid = [provider_card("futu", "富途 OpenD", data.get("futu")),
                    provider_card("ibkr", "IBKR", data.get("ibkr")),
                    provider_card("discord", "Discord", data.get("discord")),
                    provider_card("ai", "DeepSeek", data.get("ai")),
                    provider_card("feishu", "飞书", data.get("feishu"))]
            parts.append('<section class="card"><h2>数据源</h2><div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">' + "".join(grid) + '</div></section>')
            portfolio = data.get("portfolio") or {}
            parts.append(
                '<section class="card"><div class="status">'
                f'<div>持仓来源</div><div>{html.escape(str(portfolio.get("source", "无"))) }</div>'
                f'<div>持仓数</div><div>{portfolio.get("positions", 0)}</div>'
                f'<div>自选数</div><div>{data.get("watchlist_count", 0)}</div>'
                f'<div>净值可用</div><div>{"是" if portfolio.get("nav_present") else "否"}</div>'
                '</div></section>'
            )
            return "".join(parts)
        return '<section class="card"><p>数据已加载。展开下方“查看原始数据”查看完整内容。</p></section>'

    @staticmethod
    def _provider_actions(csrf: str) -> str:
        labels = (
            ("futu", "富途 OpenD"),
            ("alpaca", "Alpaca"),
            ("massive", "Massive"),
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
            csrf, (("/api/ibkr/sync", "同步 IBKR 持仓"),),
        )
        return '<div class="card"><b>自动选择主数据源，或逐个调整优先级</b></div><div class="grid">' + "".join(cards) + "</div>" + ibkr

    @staticmethod
    def _action_forms(csrf: str, actions: Tuple[Tuple[str, str], ...]) -> str:
        labels = {"/api/actions/collect":"立即采集并生成推荐","/api/actions/report":"生成日报","/api/actions/backup":"创建备份","/api/actions/feishu-test":"测试飞书","/api/actions/discord-login":"打开Discord登录","/api/actions/deepseek-test":"测试DeepSeek","/api/ibkr/sync":"同步IBKR持仓","/api/providers/massive/test":"测试Massive","/futu/import-watchlist":"从富途导入自选"}
        return '<div class="card"><b>快捷操作</b><div>' + "".join(f'<form data-ajax="1" style="display:inline" method="post" action="{path}"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><button>{html.escape(labels.get(path, label))}</button></form>' for path, label in actions) + "</div></div>"

    def _setup_page(self, csrf: str, message: str = "") -> str:
        display_labels = {"timezone":"\u65f6\u533a","discord_server":"Discord\u670d\u52a1\u5668","flow_channel":"\u5f02\u5e38\u671f\u6743\u9891\u9053","pa_channel":"PA\u5206\u6790\u5e08\u9891\u9053","mr_channel":"MR\u5206\u6790\u5e08\u9891\u9053","qmr_channel":"QMR\u5206\u6790\u5e08\u9891\u9053","fpd_channel":"FPD\u5206\u6790\u5e08\u9891\u9053","feishu_app_id":"\u98de\u4e66 App ID","flash_model":"DeepSeek\u65e5\u5e38\u6a21\u578b","pro_model":"DeepSeek\u590d\u6838\u6a21\u578b","report_delay":"\u6536\u76d8\u540e\u65e5\u62a5\u5ef6\u8fdf\uff08\u5206\u949f\uff09"}
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
