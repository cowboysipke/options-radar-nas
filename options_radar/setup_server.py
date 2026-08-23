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
from urllib.parse import parse_qs, quote, unquote, urlsplit

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
    "schedule": {"report_delay_minutes": 75, "discord_poll_minutes": 6,
                 "session_start_hour": 9, "session_end_hour": 16},
    "alerts": {"rules": [
        {"type": "min_score", "threshold": 65, "enabled": True},
        {"type": "min_premium", "threshold": 500000, "enabled": True},
        {"type": "direction", "direction": "", "enabled": False},
        {"type": "watchlist_only", "symbols": "", "enabled": False},
        {"type": "dte_range", "min_dte": 7, "max_dte": 60, "enabled": False},
        {"type": "wall_proximity", "threshold_pct": 1.0, "enabled": True},
        {"type": "regime_flip", "enabled": True},
        {"type": "iv_rank", "threshold": 80, "enabled": True},
        {"type": "discord_down", "max_failures": 3, "enabled": True},
        {"type": "opend_down", "max_failures": 3, "enabled": True},
    ]},
    "secret_refs": {
        "deepseek_api_key": "/data/secrets/deepseek_api_key",
        "feishu_app_secret": "/data/secrets/feishu_app_secret",
        "feishu_webhook": "/data/secrets/feishu_webhook",
        "futu_login_password_md5": "/data/secrets/futu_login_password_md5",
        "massive_api_key": "/data/secrets/massive_api_key",
        "alpaca_api_key": "/data/secrets/alpaca_api_key",
        "alpaca_api_secret": "/data/secrets/alpaca_api_secret",
        "discord_user_token": "/data/secrets/discord_user_token",
        "ibkr_flex_token": "/data/secrets/ibkr_flex_token",
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
    "ibkr_flex_token": ("IBKR Flex Token", "ibkr_flex_token"),
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


def _minutes(value: str) -> int:
    result = int(value)
    if not 1 <= result <= 60:
        raise ValueError("间隔需为1至60分钟")
    return result


def _hour(value: str) -> int:
    result = int(value)
    if not 0 <= result <= 23:
        raise ValueError("小时需为0至23")
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
    Field("futu_user_id", "futu.user_id", "富途账号（手机号/邮箱/ID）", _plain),
    Field("flex_query_id", "ibkr.flex_query_id", "IBKR Flex Query ID（持仓同步）", _plain),
    Field("flash_model", "ai.flash_model", "DeepSeek日常模型", _model),
    Field("pro_model", "ai.pro_model", "DeepSeek复核模型", _model),
    Field("report_delay", "schedule.report_delay_minutes", "收盘后日报延迟（分钟）", _delay),
    Field("poll_minutes", "schedule.discord_poll_minutes", "Discord采集间隔（分钟）", _minutes),
    Field("session_start_hour", "schedule.session_start_hour", "采集开始小时（美东0-23）", _hour),
    Field("session_end_hour", "schedule.session_end_hour", "采集结束小时（美东0-23）", _hour),
)


def _alerts_from_form(values: Mapping[str, str]) -> List[Dict[str, Any]]:
    """Build the ``alerts.rules`` list from the /setup 提醒设置 form."""
    def _flag(name: str) -> bool:
        return str(values.get(name, "")).strip() in {"1", "on", "true", "yes"}

    def _num(name: str, default: float, minimum: float = 0.0) -> float:
        raw = str(values.get(name, "")).strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{name} 必须是数字")
        if value < minimum:
            raise ValueError(f"{name} 不能小于 {minimum:g}")
        return value

    def _int(name: str, default: int, minimum: int = 1) -> int:
        return int(round(_num(name, float(default), float(minimum))))

    direction = str(values.get("alerts_direction_value", "")).strip().upper()
    if direction not in {"", "BULL", "BEAR", "BOTH"}:
        raise ValueError("方向只能是 BULL / BEAR / BOTH 或留空")
    dte_min = _int("alerts_dte_min", 7)
    dte_max = _int("alerts_dte_max", 60)
    if dte_min > dte_max:
        raise ValueError("DTE 最小值不能大于最大值")
    return [
        {"type": "min_score", "threshold": _num("alerts_min_score_threshold", 65), "enabled": _flag("alerts_min_score_enabled")},
        {"type": "min_premium", "threshold": _num("alerts_min_premium_threshold", 500000), "enabled": _flag("alerts_min_premium_enabled")},
        {"type": "direction", "direction": direction, "enabled": _flag("alerts_direction_enabled")},
        {"type": "watchlist_only", "symbols": str(values.get("alerts_watchlist_symbols", "")).strip()[:2000], "enabled": _flag("alerts_watchlist_only_enabled")},
        {"type": "dte_range", "min_dte": dte_min, "max_dte": dte_max, "enabled": _flag("alerts_dte_range_enabled")},
        {"type": "wall_proximity", "threshold_pct": _num("alerts_wall_proximity_pct", 1.0), "enabled": _flag("alerts_wall_proximity_enabled")},
        {"type": "regime_flip", "enabled": _flag("alerts_regime_flip_enabled")},
        {"type": "iv_rank", "threshold": _num("alerts_iv_rank_threshold", 80), "enabled": _flag("alerts_iv_rank_enabled")},
        {"type": "discord_down", "max_failures": _int("alerts_discord_max_failures", 3), "enabled": _flag("alerts_discord_down_enabled")},
        {"type": "opend_down", "max_failures": _int("alerts_opend_max_failures", 3), "enabled": _flag("alerts_opend_down_enabled")},
    ]


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

    # ---- 管理员账户（用户名 + 密码，管理口令降级为备用）----
    def admin_configured(self) -> bool:
        data = self.store.load()
        admin = data.get("admin") if isinstance(data.get("admin"), Mapping) else {}
        return bool(str(admin.get("username", "")).strip() and str(admin.get("password_hash", "")).strip())

    def admin_username(self) -> str:
        data = self.store.load()
        admin = data.get("admin") if isinstance(data.get("admin"), Mapping) else {}
        return str(admin.get("username", "")).strip()

    def set_admin(self, username: str, password: str) -> None:
        username = str(username).strip()
        password = str(password)
        if not username or len(username) > 64 or not SYMBOLIC_NAME.fullmatch(username):
            raise ValueError("用户名需为字母数字/中文，长度不超过64")
        if len(password) < 8:
            raise ValueError("密码至少8位")
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), 200_000).hex()
        data = self.store.load()
        data["admin"] = {"username": username, "password_hash": digest, "salt": salt, "configured": True}
        self.store.save(data)

    def authenticate_admin(self, username: str, password: str) -> bool:
        data = self.store.load()
        admin = data.get("admin") if isinstance(data.get("admin"), Mapping) else {}
        if str(admin.get("username", "")).strip() != str(username).strip():
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", str(password).encode("utf-8"), str(admin.get("salt", "")).encode("ascii"), 200_000,
        ).hex()
        return hmac.compare_digest(digest, str(admin.get("password_hash", "")))

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
    "/audit": ("数据复核", "status", "抽样复核原文本、解析方向与回测结果"),
    "/system": ("系统诊断", "status", "富途 OpenD、Discord、DeepSeek、飞书与持仓同步状态"),
    "/setup": ("设置", "status", "首次配置和连接测试"),
    "/providers": ("数据源诊断", "providers", "富途主源、Alpaca/Massive 历史复核"),
}

GET_APIS = {
    "/api/status": "status",
    "/api/futu/status": "futu_status",
    "/api/discord-status": "discord_status",
    "/api/recommendations": "recommendations",
    "/api/portfolio": "portfolio",
    "/api/contracts": "contracts",
    "/api/rules": "rules",
    "/api/analysts": "analysts",
    "/api/backtest": "backtest",
    "/api/analyst-backtest-detail": "analyst_backtest_detail",
    "/api/audit-sample": "audit_sample",
    "/api/audit-review": "audit_review",
    "/api/providers": "providers",
    "/api/signals": "signals",
    "/api/gex": "gex",
    "/api/dealer": "dealer",
    "/api/spark": "spark",
}

POST_APIS = {
    "/api/futu/send-verification": "futu_send_verification",
    "/api/futu/submit-verification": "futu_submit_verification",
    "/api/futu/relogin": "futu_relogin",
    "/api/futu/sync": "futu_sync",
    "/api/actions/collect": "collect",
    "/api/actions/reevaluate": "reevaluate",
    "/api/actions/report": "report",
    "/api/actions/backup": "backup",
    "/api/actions/gex-snapshot": "gex_snapshot",
    "/api/actions/alerts-check": "alerts_check",
    "/api/actions/flow-classify": "flow_classify",
    "/api/actions/feishu-test": "feishu_test",
    "/api/actions/discord-login": "discord_login",
    "/api/actions/discord-refresh-qr": "discord_refresh_qr",
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

    def _read_raw_body(self, max_bytes: int) -> bytes:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if size <= 0 or size > max_bytes:
            raise ValueError("文件大小无效或超出上限")
        return self.rfile.read(size)

    def _export_bundle(self) -> Dict[str, Any]:
        config = self.app.store.load()
        refs = config.get("secret_refs", {}) if isinstance(config.get("secret_refs"), Mapping) else {}
        secrets: Dict[str, str] = {}
        for name, path in refs.items():
            try:
                value = Path(str(path)).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                value = ""
            secrets[str(name)] = value
        return {"config": config, "secrets": secrets}

    def _import_bundle(self, bundle: Mapping[str, Any]) -> Dict[str, Any]:
        config = bundle.get("config")
        if not isinstance(config, dict):
            raise ValueError("配置包缺少 config 对象")
        data_dir = Path(os.getenv("DATA_DIR", "/data"))
        config["database_path"] = str(data_dir / "options_radar.db")
        config["evidence_dir"] = str(data_dir / "evidence")
        refs = self.app.store.fixed_secret_refs()
        config["secret_refs"] = refs
        self.app.store.save(config)
        secrets = bundle.get("secrets")
        if isinstance(secrets, dict):
            for name, value in secrets.items():
                target = refs.get(str(name))
                if target and isinstance(value, str) and value:
                    Path(target).parent.mkdir(parents=True, exist_ok=True)
                    Path(target).write_text(value, encoding="utf-8")
        return {"status": "ok", "message": "配置已导入"}

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
        if path == "/api/config/export":
            if not session:
                self._json(HTTPStatus.UNAUTHORIZED, {"status": "unauthorized"})
                return
            self._json(HTTPStatus.OK, self._export_bundle())
            return
        if path == "/api/data/export":
            if not session:
                self._json(HTTPStatus.UNAUTHORIZED, {"status": "unauthorized"})
                return
            db_path = Path(os.getenv("DATA_DIR", "/data")) / "options_radar.db"
            if not db_path.is_file():
                self._json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "数据文件不存在"})
                return
            payload = db_path.read_bytes()
            self._headers(HTTPStatus.OK, "application/octet-stream", len(payload), {
                "Content-Disposition": 'attachment; filename="options_radar.db"',
            })
            self.wfile.write(payload)
            return
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
        if path == "/api/data/import":
            session = self._session()
            if not session:
                self._json(HTTPStatus.UNAUTHORIZED, {"status": "unauthorized"})
                return
            _, csrf = session
            if not hmac.compare_digest(str(self.headers.get("X-CSRF-Token", "")), csrf):
                self._json(HTTPStatus.FORBIDDEN, {"status": "forbidden", "message": "CSRF validation failed"})
                return
            try:
                raw = self._read_raw_body(512 * 1024 * 1024)
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})
                return
            data_dir = Path(os.getenv("DATA_DIR", "/data"))
            db_path = data_dir / "options_radar.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.write_bytes(raw)
            self._callback("reload_service", {})
            self._json(HTTPStatus.OK, {"status": "ok", "message": "数据已导入，服务已重启"})
            return
        try:
            values = self._body()
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, html.escape(str(exc)), "text/plain; charset=utf-8")
            return
        if path == "/admin-init":
            if self.app.admin_configured():
                self._send(HTTPStatus.FORBIDDEN, "管理员账户已存在，请直接登录", "text/plain; charset=utf-8")
                return
            if str(values.get("password", "")) != str(values.get("confirm", "")):
                self._send(HTTPStatus.BAD_REQUEST, self._login_page("两次输入的密码不一致"))
                return
            try:
                self.app.set_admin(str(values.get("username", "")), str(values.get("password", "")))
            except ValueError as exc:
                self._send(HTTPStatus.BAD_REQUEST, self._login_page(str(exc)))
                return
            session_id, _ = self.app.new_session()
            cookie = f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={self.app.session_ttl}"
            self._redirect("/", {"Set-Cookie": cookie})
            return
        if path == "/login":
            if self.app.local_mode:
                # Compatibility with an old cached login page.  Local mode
                # does not validate or persist a management token.
                self._redirect("/")
                return
            authenticated = False
            username = str(values.get("username", "")).strip()
            password = str(values.get("password", ""))
            if username and password:
                authenticated = self.app.authenticate_admin(username, password)
            if not authenticated and str(values.get("token", "")).strip():
                authenticated = self.app.authenticate_token(str(values.get("token", "")).strip())
            if not authenticated:
                time.sleep(0.2)
                self._send(HTTPStatus.UNAUTHORIZED, self._login_page("用户名或密码不正确"))
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
        if path == "/api/config/import":
            try:
                result = self._import_bundle(values)
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})
                return
            self._callback("reload_service", {})
            self._json(HTTPStatus.OK, result)
            return
        if path == "/admin-password":
            old_password = str(values.get("old_password", ""))
            new_password = str(values.get("new_password", ""))
            confirm = str(values.get("confirm_password", ""))
            admin_user = self.app.admin_username()
            if not admin_user or not self.app.authenticate_admin(admin_user, old_password):
                self._json(HTTPStatus.FORBIDDEN, {"status": "error", "message": "原密码不正确"})
                return
            if new_password != confirm:
                self._json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": "两次输入的新密码不一致"})
                return
            try:
                self.app.set_admin(admin_user, new_password)
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})
                return
            self._json(HTTPStatus.OK, {"status": "ok", "message": "管理员密码已更新"})
            return
        if path == "/save-alerts":
            try:
                rules = _alerts_from_form(values)
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})
                return
            data = self.app.store.load()
            data["alerts"] = {"rules": rules}
            self.app.store.save(data)
            self._callback("reload_service", {})
            self._json(HTTPStatus.OK, {"status": "ok", "message": "提醒设置已保存"})
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
                  ("/audit", "数据复核"), ("/system", "系统诊断"), ("/setup", "设置"))
        nav = "".join(
            f'<a class="{"active" if path == active else ""}" href="{path}">{label}</a>' for path, label in links
        )
        return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root{{--ink:#1d1d1f;--muted:#86868b;--bg:#f5f5f7;--card:#fff;--line:#e8e8ed;--up:#d70015;--down:#00a651;--accent:#0071e3;--control-h:36px}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}}
header{{position:sticky;top:0;z-index:10;background:rgba(245,245,247,.82);backdrop-filter:saturate(180%) blur(20px);border-bottom:1px solid var(--line);padding:10px 24px;display:flex;align-items:center;gap:20px;flex-wrap:wrap}}
.logo{{font-size:16px;font-weight:700;letter-spacing:-.01em;white-space:nowrap}}header small{{color:var(--muted);font-size:12px}}
nav{{display:flex;gap:2px;margin-left:auto}}nav a{{color:var(--ink);text-decoration:none;padding:6px 12px;border-radius:18px;font-size:13px;white-space:nowrap}}nav a:hover{{background:#e5e5ea}}nav a.active{{background:var(--ink);color:#fff}}
main{{max-width:1440px;margin:0 auto;padding:24px 20px 60px}}
h1{{font-size:26px;font-weight:700;letter-spacing:-.02em;margin:0 0 4px}}h2{{font-size:16px;font-weight:600;margin:0 0 10px}}.sub{{color:var(--muted);margin:0 0 14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px 14px}}
.tile b{{display:block;font-size:11px;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}}
.tile span{{display:block;font-size:17px;font-weight:600;margin-top:3px}}
.table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}} .table-wrap table{{min-width:640px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
.metric{{font-size:26px;font-weight:700;letter-spacing:-.02em}}
.muted{{color:var(--muted)}}small{{color:var(--muted)}}
label{{display:block;margin:13px 0 5px;font-weight:600}}input{{width:100%;padding:10px 12px;border:1px solid #d2d2d7;border-radius:10px;font-size:14px}}
button,.button{{display:inline-block;margin:9px 6px 0 0;padding:9px 18px;border:0;border-radius:20px;background:var(--accent);color:#fff;font-weight:600;font-size:14px;text-decoration:none;cursor:pointer}}button:hover{{opacity:.88}}button.secondary{{background:#e5e5ea;color:var(--ink)}}
.toolbar{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 14px}}
.toolbar input,.toolbar select{{height:var(--control-h);padding:0 12px;border:1px solid #d2d2d7;border-radius:10px;font-size:13px;margin:0}}
.toolbar button{{height:var(--control-h);padding:0 16px;border-radius:10px;margin:0;font-size:13px}}
.toolbar label{{margin:0;font-size:13px;font-weight:600;white-space:nowrap}}
.form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:0 20px}}
.form-grid label{{margin:10px 0 4px}}
.ok,.error{{padding:10px 14px;border-radius:10px}} .ok{{background:#e9f9ef}} .error{{background:#fdecec}}
pre{{white-space:pre-wrap;word-break:break-word;background:#f0f0f2;color:#3a3a3c;padding:14px;border-radius:12px;max-height:520px;overflow:auto;font-size:12px}}code{{background:#eef0f3;padding:2px 6px;border-radius:5px}}
.status{{display:grid;grid-template-columns:1fr auto;gap:8px}}
table{{width:100%;border-collapse:collapse}}th{{color:var(--muted);font-weight:500;text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}th,td{{padding:11px 12px;border-bottom:1px solid var(--line)}}tr:hover td{{background:#fafafa}}
summary{{cursor:pointer;font-weight:600}}
.badge{{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600}}.badge.up{{background:#fdecec;color:#d70015}}.badge.down{{background:#e9f9ef;color:#00a651}}.badge.watch{{background:#eef2ff;color:#0037c1}}
ul.analyst-votes{{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-wrap:wrap;gap:6px}}ul.analyst-votes li{{background:#f0f0f2;border:1px solid var(--line);border-radius:8px;padding:4px 10px;font-size:13px}}
.market-banner{{background:#eef2ff;color:#0037c1;border:1px solid #d6e0f5;border-radius:10px;padding:8px 14px;font-size:13px;margin-bottom:14px}}
.risk-inline{{margin:6px 0 0;font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:6px;align-items:center}}.risk-inline strong{{color:#b25000;font-size:12px}}.risk-chip{{background:#fff8f0;border:1px solid #f0dcc8;color:#8a5a2b;border-radius:6px;padding:2px 8px;font-size:11.5px;white-space:nowrap}}
.rec .rec-top{{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}}
.rec-sym{{font-size:15px;font-weight:700}}
.rec-leg{{font-size:12px;color:var(--muted);margin-left:6px}}
.rec-score{{display:flex;align-items:baseline;gap:6px}}
.rec-score .score-num{{font-size:26px;font-weight:700;letter-spacing:-.02em}}
.rec-score .grade{{color:var(--muted);font-size:13px}}
.rec-meta{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0 10px;font-size:12px}}
.rec-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}}
.rec-kv{{background:#f7f7fa;border-radius:10px;padding:6px 10px;min-width:0}}
.rec-kv.bidask{{grid-column:1/-1}}
.rec-kv b{{display:block;font-size:10.5px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em}}
.rec-kv span{{display:block;font-size:13.5px;font-weight:600;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.rec-state{{padding:2px 8px;border-radius:8px;font-size:11px;font-weight:600}}
.rec-state.ok{{background:#e9f9ef;color:#00a651}}
.rec-state.flow{{background:#eef2ff;color:#0037c1}}
.rec-state.warn{{background:#fff8f0;color:#b25000}}
.rec-strategy{{margin-bottom:10px;padding:9px 11px;background:#eef7ef;border-radius:10px;font-size:13px}}
.rec-strategy .rec-hint{{margin-top:4px;font-size:12.5px}}
.rec-strategy .rec-warn{{color:#d70015;font-weight:600;margin-top:4px;font-size:12.5px}}
.rec-gex{{padding:9px 11px;background:#f7f7fb;border-radius:10px;font-size:13px}}
.rec-gex-head{{display:flex;align-items:center;gap:8px}}
.rec-gex-head .gex-btn{{margin:0 0 0 auto;padding:3px 10px;font-size:12px}}
.rec-walls{{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}}
.wall-chip{{background:#fff;border:1px solid var(--line);border-radius:8px;padding:1px 8px;font-size:11.5px;font-weight:600}}
.info{{position:relative;display:inline-block;cursor:help;color:var(--accent);font-size:14px;font-weight:700;padding:0 3px}}
.info .tip{{display:none;position:absolute;left:30px;top:-8px;z-index:60;width:340px;max-width:78vw;background:#1d1d1f;color:#f5f5f7;border-radius:10px;padding:12px 14px;font-size:12.5px;line-height:1.65;text-align:left;box-shadow:0 10px 28px rgba(0,0,0,.3)}}
.info:hover .tip{{display:block}}
.spark svg{{display:block}}
.layout{{display:grid;grid-template-columns:minmax(0,1fr) 292px;gap:18px;align-items:start}}
.main-col{{min-width:0}}
.sidebar{{display:flex;flex-direction:column;gap:12px;position:sticky;top:70px}}
.sidebar #action-result:empty{{display:none}}
.sidebar .card{{padding:14px;margin-bottom:0}}
.sidebar form{{display:block;margin:0}}
.sidebar form button{{width:100%;margin:5px 0 0;padding:8px 12px;font-size:13px;border-radius:14px}}
.sidebar label{{margin:0 0 4px;font-size:13px}}
.sidebar select{{width:100%;margin:0;padding:8px 10px}}
.toolbar{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:12px 14px}}
.grid .card:hover{{box-shadow:0 6px 16px rgba(0,0,0,.08)}}
.card .grid .card{{margin-bottom:0}}
@media(max-width:1080px){{.layout{{grid-template-columns:1fr}}.sidebar{{position:static;flex-direction:row;flex-wrap:wrap;align-items:flex-start}}.sidebar .card{{flex:1 1 240px}}}}
@media(max-width:900px){{.grid{{grid-template-columns:repeat(auto-fill,minmax(200px,1fr))}}h1{{font-size:24px}}}}
@media(max-width:640px){{header{{padding:10px 14px}}nav{{width:100%;margin-left:0;overflow-x:auto}}h1{{font-size:22px}}.grid{{grid-template-columns:1fr}}main{{padding:16px 12px 48px}}.card{{padding:14px}}.form-grid{{grid-template-columns:1fr}}}}
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
      if (form.dataset.reload && response.ok) {{ setTimeout(function(){{ location.reload(); }}, 700); }}
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
document.querySelectorAll('select[data-goto]').forEach(function(sel) {{
  sel.addEventListener('change', function() {{
    location.href = sel.getAttribute('data-goto') + encodeURIComponent(sel.value);
  }});
}});
let gexZoom = 1;
let gexLayers = {{ma20: true, ma50: true, put_wall: true, call_wall: true, flip: true, spot: true, support: true, resistance: true, flow: true}};
function gexCumulative(gex) {{
  const cum = []; let acc = 0;
  for (let i = 0; i < gex.length; i++) {{ acc += gex[i]; cum.push(acc); }}
  return cum;
}}
function renderGexChart(container, data) {{
  if (!data || !data.strikes || !data.strikes.length) {{
    container.innerHTML = '<span class="muted">暂无 GEX 数据</span>'; return;
  }}
  const strikes = data.strikes, gex = data.net_gex;
  const cum = gexCumulative(gex);
  const W = 800, H = 320, padL = 56, padR = 20, padT = 30, padB = 32;
  const minS = Math.min.apply(null, strikes), maxS = Math.max.apply(null, strikes);
  const span = (maxS - minS) || 1;
  const maxAbs = Math.max.apply(null, gex.map(function(g){{ return Math.abs(g); }}).concat([1e-9]));
  const maxCum = Math.max.apply(null, cum.map(function(c){{ return Math.abs(c); }}).concat([1e-9]));
  const x = function(s){{ return padL + (s - minS) / span * (W - padL - padR); }};
  const yBar = function(g){{ return padT + (H - padT - padB) / 2 - (g / maxAbs) * ((H - padT - padB) / 2); }};
  const yCum = function(c){{ return padT + (H - padT - padB) / 2 - (c / maxCum) * ((H - padT - padB) / 2); }};
  let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:' + (gexZoom * 100) + '%;background:#fff;border:1px solid #e8e8ed;border-radius:8px">';
  svg += '<line x1="' + padL + '" y1="' + yBar(0) + '" x2="' + (W - padR) + '" y2="' + yBar(0) + '" stroke="#c9c9cf" stroke-width="1"/>';
  const barWidth = Math.max(2, (W - padL - padR) / strikes.length * 0.7);
  const gexHits = [];
  for (let i = 0; i < strikes.length; i++) {{
    const g = gex[i];
    const zeroY = yBar(0), barY = yBar(g);
    const top = Math.min(zeroY, barY), h = Math.abs(zeroY - barY);
    const color = g >= 0 ? '#00a651' : '#d70015';
    if (h > 0.5) {{
      svg += '<rect x="' + (x(strikes[i]) - barWidth / 2) + '" y="' + top + '" width="' + barWidth + '" height="' + h + '" fill="' + color + '" opacity="0.85"/>';
    }}
    gexHits.push({{x: x(strikes[i]), strike: strikes[i], gex: g, cum: cum[i]}});
  }}
  let pts = '';
  for (let i = 0; i < strikes.length; i++) {{
    pts += (i ? ' ' : '') + x(strikes[i]).toFixed(1) + ',' + yCum(cum[i]).toFixed(1);
  }}
  svg += '<polyline points="' + pts + '" fill="none" stroke="#0071e3" stroke-width="2"/>';
  if (data.spot) {{
    const sx = x(data.spot);
    svg += '<line x1="' + sx + '" y1="' + padT + '" x2="' + sx + '" y2="' + (H - padB) + '" stroke="#86868b" stroke-width="1.5" stroke-dasharray="4,3"/>';
    svg += '<text x="' + sx + '" y="' + (H - padB + 14) + '" text-anchor="middle" fill="#86868b" font-size="11">' + data.spot + '</text>';
  }}
  if (data.zero_gamma != null) {{
    const zx = x(data.zero_gamma);
    svg += '<line x1="' + zx + '" y1="' + padT + '" x2="' + zx + '" y2="' + (H - padB) + '" stroke="#f0a500" stroke-width="2"/>';
    const zlabel = (data.gamma_flip != null) ? ('Flip ' + data.gamma_flip) : ('≈零 Gamma ' + data.zero_gamma);
    svg += '<text x="' + zx + '" y="' + (padT + 13) + '" text-anchor="middle" fill="#f0a500" font-size="10">' + zlabel + '</text>';
  }}
  function mark(strike, color, label) {{
    if (strike == null) return;
    const sx = x(strike);
    svg += '<line x1="' + sx + '" y1="' + padT + '" x2="' + sx + '" y2="' + (H - padB) + '" stroke="' + color + '" stroke-width="1" stroke-dasharray="2,2"/>';
    svg += '<text x="' + sx + '" y="' + (padT + 26) + '" text-anchor="middle" fill="' + color + '" font-size="9">' + label + '</text>';
  }}
  mark(data.call_wall, '#00a651', 'Call Wall');
  mark(data.put_wall, '#d70015', 'Put Wall');
  svg += '<text x="' + padL + '" y="' + (H - padB + 14) + '" fill="#86868b" font-size="10">' + minS + '</text>';
  svg += '<text x="' + (W - padR) + '" y="' + (H - padB + 14) + '" text-anchor="end" fill="#86868b" font-size="10">' + maxS + '</text>';
  svg += '</svg>';
  window.gexCurveHits = gexHits;
  container.innerHTML = '<div style="position:relative">' + svg
    + '<div id="gex-curve-tip" style="display:none;position:absolute;z-index:20;background:#1d1d1f;color:#fff;font-size:11.5px;line-height:1.5;padding:6px 10px;border-radius:8px;white-space:nowrap;pointer-events:none;box-shadow:0 4px 14px rgba(0,0,0,.25)"></div>'
    + '</div>';
}}
function dealerFetch(symbol) {{
  return fetch('/api/dealer?symbol=' + encodeURIComponent(symbol)).then(function(r){{ return r.json(); }});
}}
function escText(s) {{
  const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML;
}}
function renderCandleChart(container, data) {{
  const bars = (data && data.bars) || [];
  if (!bars.length) {{ container.innerHTML = '<span class="muted">暂无日K数据</span>'; return; }}
  const W = 800, H = 320, padL = 58, padR = 78, padT = 16, padB = 30;
  function eventTime(iso) {{
    let text = String(iso || '');
    if (text && !/[zZ]|[+-]\\d\\d:?\\d\\d$/.test(text)) text += 'Z';  // DB stores naive UTC
    try {{ return new Date(text).getTime(); }} catch(_) {{ return null; }}
  }}
  const events = (data.flow_events || []).map(function(ev) {{ return {{ev: ev, t: eventTime(ev.observed_at)}}; }})
    .filter(function(item) {{ return item.t != null; }});
  let minT = Infinity, maxT = -Infinity, minP = Infinity, maxP = -Infinity;
  bars.forEach(function(b) {{
    minT = Math.min(minT, b.t); maxT = Math.max(maxT, b.t);
    minP = Math.min(minP, b.l); maxP = Math.max(maxP, b.h);
  }});
  events.forEach(function(item) {{ minT = Math.min(minT, item.t); maxT = Math.max(maxT, item.t); }});
  if (data.spot != null) {{ minP = Math.min(minP, data.spot); maxP = Math.max(maxP, data.spot); }}
  const gx = data.gex || {{}};
  const ps = data.price_structure || {{}};
  [gx.put_wall, gx.call_wall, gx.gamma_flip].forEach(function(w) {{
    if (w != null) {{ minP = Math.min(minP, w); maxP = Math.max(maxP, w); }}
  }});
  [ps.support, ps.resistance].forEach(function(v) {{
    if (v != null) {{ minP = Math.min(minP, v); maxP = Math.max(maxP, v); }}
  }});
  (ps.ma20 || []).concat(ps.ma50 || []).forEach(function(v) {{
    if (v != null) {{ minP = Math.min(minP, v); maxP = Math.max(maxP, v); }}
  }});
  if (maxP === minP) maxP = minP + 1;
  const x = function(t) {{ return padL + (t - minT) / ((maxT - minT) || 1) * (W - padL - padR); }};
  const y = function(p) {{ return padT + (1 - (p - minP) / (maxP - minP)) * (H - padT - padB); }};
  let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:' + (gexZoom * 100) + '%;background:#fff;border:1px solid #e8e8ed;border-radius:8px">';
  const cw = Math.max(2, (W - padL - padR) / bars.length * 0.65);
  const hits = {{lines: [], maSeries: [], dots: [], candles: []}};
  bars.forEach(function(b) {{
    const up = b.c >= b.o;
    const color = up ? '#d70015' : '#00a651';
    const cx = x(b.t);
    svg += '<line x1="' + cx + '" y1="' + y(b.h) + '" x2="' + cx + '" y2="' + y(b.l) + '" stroke="' + color + '" stroke-width="1"/>';
    const bodyTop = y(Math.max(b.o, b.c));
    const bodyH = Math.max(1, Math.abs(y(b.o) - y(b.c)));
    svg += '<rect x="' + (cx - cw / 2) + '" y="' + bodyTop + '" width="' + cw + '" height="' + bodyH + '" fill="' + color + '"/>';
    hits.candles.push({{cx: cx, t: b.t, o: b.o, h: b.h, l: b.l, c: b.c}});
  }});
  const L = gexLayers || {{}};
  function hline(px, color, dash, key, label, explain) {{
    if (px == null) return;
    const py = y(px);
    svg += '<line class="gex-layer" data-key="' + key + '" x1="' + padL + '" y1="' + py + '" x2="' + (W - padR) + '" y2="' + py + '" stroke="' + color + '" stroke-width="1.2" stroke-dasharray="' + dash + '" opacity="0.85"/>';
    hits.lines.push({{key: key, y: py, text: label + ' $' + px + ' · ' + explain}});
  }}
  if (L.put_wall !== false) hline(gx.put_wall, '#d70015', '5,3', 'put_wall', 'Put Wall', '做市商下方支撑');
  if (L.call_wall !== false) hline(gx.call_wall, '#00a651', '5,3', 'call_wall', 'Call Wall', '做市商上方阻力');
  if (L.flip !== false) hline(gx.gamma_flip, '#f0a500', '3,3', 'flip', 'Gamma Flip', '净GEX由正转负，波动放大区');
  if (L.support !== false) hline(ps.support, '#b25000', '1,3', 'support', '支撑', '近20日低点');
  if (L.resistance !== false) hline(ps.resistance, '#8a6d2f', '1,3', 'resistance', '阻力', '近20日高点');
  if (L.spot !== false && data.spot != null) {{
    const spotPy = y(data.spot);
    svg += '<line class="gex-layer" data-key="spot" x1="' + padL + '" y1="' + spotPy + '" x2="' + (W - padR) + '" y2="' + spotPy + '" stroke="#86868b" stroke-width="1.2" stroke-dasharray="2,3"/>';
    hits.lines.push({{key: 'spot', y: spotPy, text: '现价 $' + Number(data.spot).toFixed(2)}});
    const lastBar = bars[bars.length - 1];
    const label = '$' + Number(data.spot).toFixed(2);
    const tw = label.length * 6.1 + 10;
    svg += '<rect x="' + (W - padR + 3) + '" y="' + (spotPy - 8) + '" width="' + tw + '" height="14" rx="3" fill="' + (lastBar && lastBar.c >= lastBar.o ? '#d70015' : '#00a651') + '" opacity="0.94"/>';
    svg += '<text x="' + (W - padR + 8) + '" y="' + (spotPy + 2.5) + '" fill="#fff" font-size="10">' + escText(label) + '</text>';
  }}
  function maPolyline(values, color, key, label) {{
    if (!values || !values.length) return;
    let d = '';
    const pts = [];
    values.forEach(function(v, i) {{
      if (v == null || bars[i] == null) {{ d = ''; return; }}
      const px = x(bars[i].t), py = y(v);
      d += (d ? ' ' : '') + px.toFixed(1) + ',' + py.toFixed(1);
      pts.push({{x: px, y: py, price: v}});
    }});
    if (d) svg += '<polyline points="' + d + '" fill="none" stroke="' + color + '" stroke-width="1.4" opacity="0.95"/>';
    hits.maSeries.push({{key: key, label: label, pts: pts}});
  }}
  if (L.ma20 !== false) maPolyline(ps.ma20, '#f0a500', 'ma20', 'MA20 均线');
  if (L.ma50 !== false) maPolyline(ps.ma50, '#0071e3', 'ma50', 'MA50 均线');
  if (L.flow !== false) {{
    (data.flow_events || []).forEach(function(ev) {{
      let t = eventTime(ev.observed_at);
      if (t == null) return;
      let closeP = null;
      for (let i = 0; i < bars.length; i++) {{ if (bars[i].t >= t) {{ closeP = bars[i].c; break; }} }}
      if (closeP == null) closeP = bars[bars.length - 1].c;
      const color = (ev.option_type === 'P') ? '#d70015' : '#00a651';
      const cx = x(t), cy = y(closeP);
      svg += '<circle cx="' + cx + '" cy="' + cy + '" r="3.2" fill="' + color + '" stroke="#fff" stroke-width="1"/>';
      hits.dots.push({{cx: cx, cy: cy, text: escText(ev.contract_key + ' · ' + (ev.option_type === 'P' ? 'PUT' : 'CALL') + ' · $' + ev.premium + ' · ' + String(ev.observed_at || '').slice(0, 16))}});
    }});
  }}
  const divisions = 5;
  for (let i = 0; i <= divisions; i++) {{
    const pv = minP + (maxP - minP) * i / divisions;
    const gy = y(pv);
    svg += '<line x1="' + padL + '" y1="' + gy + '" x2="' + (W - padR) + '" y2="' + gy + '" stroke="#ececf0" stroke-width="1"/>';
    svg += '<text x="' + (padL - 6) + '" y="' + (gy + 3.5) + '" text-anchor="end" fill="#86868b" font-size="10">' + Number(pv).toFixed(1) + '</text>';
  }}
  function dayLabel(t) {{
    const d = new Date(t); return (d.getMonth() + 1) + '/' + d.getDate();
  }}
  svg += '<text x="' + padL + '" y="' + (H - 8) + '" fill="#86868b" font-size="10">' + dayLabel(minT) + '</text>';
  const midT = minT + (maxT - minT) / 2;
  svg += '<text x="' + x(midT) + '" y="' + (H - 8) + '" text-anchor="middle" fill="#86868b" font-size="10">' + dayLabel(midT) + '</text>';
  svg += '<text x="' + (W - padR) + '" y="' + (H - 8) + '" text-anchor="end" fill="#86868b" font-size="10">' + dayLabel(maxT) + '</text>';
  svg += '<line id="gex-cross" x1="0" y1="0" x2="0" y2="0" stroke="#1d1d1f" stroke-width="0.8" stroke-dasharray="3,3" display="none" pointer-events="none"/>';
  svg += '</svg>';
  window.gexCandleHits = hits;
  function layerChip(key, color, label, value, dashed) {{
    const on = gexLayers[key] !== false;
    const shown = value == null ? '—' : (typeof value === 'number' ? '$' + value : value);
    const swatch = '<span style="display:inline-block;width:20px;height:0;border-top:2px ' + (dashed === false ? 'solid' : 'dashed') + ' ' + color + ';vertical-align:middle;margin-right:6px"></span>';
    return '<button type="button" data-layer="' + key + '" style="' + (on ? '' : 'opacity:.38;') + 'display:inline-flex;align-items:center;margin:2px 10px 2px 0;padding:3px 10px;border:1px solid ' + (on ? '#d2d2d7' : '#ececf0') + ';border-radius:14px;background:' + (on ? '#fafafa' : '#fff') + ';color:#1d1d1f;font-size:11.5px;cursor:pointer">' + swatch + escText(label) + ' ' + escText(shown) + '</button>';
  }}
  function lastValue(values) {{
    if (!values) return null;
    for (let i = values.length - 1; i >= 0; i--) {{ if (values[i] != null) return values[i]; }}
    return null;
  }}
  const legend = '<div style="margin:2px 0 6px;color:#86868b;font-size:11px">图层（点击开关）：</div><div style="margin:0 0 8px">'
    + layerChip('ma20', '#f0a500', 'MA20', lastValue(ps.ma20), false)
    + layerChip('ma50', '#0071e3', 'MA50', lastValue(ps.ma50), false)
    + layerChip('put_wall', '#d70015', 'Put Wall', gx.put_wall)
    + layerChip('call_wall', '#00a651', 'Call Wall', gx.call_wall)
    + layerChip('flip', '#f0a500', 'Flip', gx.gamma_flip)
    + layerChip('spot', '#86868b', '现价', data.spot)
    + layerChip('support', '#b25000', '支撑', ps.support)
    + layerChip('resistance', '#8a6d2f', '阻力', ps.resistance)
    + layerChip('flow', '#0071e3', 'Flow事件', (data.flow_events || []).length + ' 条', false)
    + '</div>';
  container.innerHTML = legend
    + '<div style="position:relative">' + svg
    + '<div id="gex-tip" style="display:none;position:absolute;z-index:20;background:#1d1d1f;color:#fff;font-size:11.5px;line-height:1.5;padding:6px 10px;border-radius:8px;white-space:nowrap;pointer-events:none;box-shadow:0 4px 14px rgba(0,0,0,.25)"></div>'
    + '</div>';
}}
function renderHeatmap(container, gexData) {{
  const cells = (gexData && gexData.heatmap) || [];
  if (!cells.length) {{ container.innerHTML = '<span class="muted">暂无数据</span>'; return; }}
  const expiries = [], strikes = [];
  cells.forEach(function(c) {{
    if (expiries.indexOf(c.expiry) < 0) expiries.push(c.expiry);
    if (strikes.indexOf(c.strike) < 0) strikes.push(c.strike);
  }});
  expiries.sort(); strikes.sort(function(a, b) {{ return a - b; }});
  const cellW = 36, cellH = 22, padL = 92, padT = 12, padR = 10, padB = 40;
  const W = padL + padR + strikes.length * cellW;
  const H = padT + padB + expiries.length * cellH;
  const map = {{}};
  cells.forEach(function(c) {{ map[c.expiry + '|' + c.strike] = c; }});
  let maxAbs = 0;
  cells.forEach(function(c) {{ maxAbs = Math.max(maxAbs, Math.abs(c.net_gex)); }});
  maxAbs = maxAbs || 1;
  let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:' + W + 'px;max-width:100%;background:#fff;border:1px solid #e8e8ed;border-radius:8px">';
  expiries.forEach(function(exp, ri) {{
    svg += '<text x="' + (padL - 6) + '" y="' + (padT + ri * cellH + cellH / 2 + 4) + '" text-anchor="end" fill="#86868b" font-size="10">' + escText(exp.slice(5).replace('-', '/')) + '</text>';
    strikes.forEach(function(strike, ci) {{
      const cell = map[exp + '|' + strike];
      const v = cell ? cell.net_gex : 0;
      const x = padL + ci * cellW, y = padT + ri * cellH;
      const color = v >= 0 ? '#00a651' : '#d70015';
      const opacity = 0.12 + 0.88 * Math.min(1, Math.abs(v) / maxAbs);
      svg += '<rect x="' + (x + 1) + '" y="' + (y + 1) + '" width="' + (cellW - 2) + '" height="' + (cellH - 2) + '" fill="' + color + '" opacity="' + opacity.toFixed(2) + '" rx="2">'
        + '<title>' + escText(exp + ' · $' + strike + ' · Net GEX ' + Number(v).toFixed(0)) + '</title></rect>';
    }});
  }});
  strikes.forEach(function(strike, ci) {{
    const x = padL + ci * cellW + cellW / 2;
    svg += '<text x="' + x + '" y="' + (H - padB + 16) + '" text-anchor="middle" fill="#86868b" font-size="9" transform="rotate(-60 ' + x + ' ' + (H - padB + 16) + ')">' + escText('$' + strike) + '</text>';
  }});
  svg += '</svg>';
  container.innerHTML = svg;
}}
function renderFlowList(container, data) {{
  const events = (data && data.flow_events) || [];
  if (!events.length) {{
    container.innerHTML = '<p class="muted" style="font-size:12px;margin-top:6px">近30天无异常期权事件</p>'; return;
  }}
  let rows = '';
  events.forEach(function(ev) {{
    const type = ev.option_type === 'P' ? 'PUT' : (ev.option_type === 'C' ? 'CALL' : escText(ev.option_type));
    rows += '<tr><td>' + escText(ev.contract_key) + '</td><td>' + type + '</td>'
      + '<td class="muted">' + escText(ev.expiry) + '</td><td class="muted">$' + Number(ev.premium || 0).toLocaleString() + '</td>'
      + '<td class="muted">' + escText(String(ev.observed_at || '').slice(0, 16)) + '</td></tr>';
  }});
  container.innerHTML = '<details><summary>近30天异常期权（' + events.length + ' 条）</summary>'
    + '<div class="table-wrap"><table><tr><th>合约</th><th>类型</th><th>到期</th><th>权利金</th><th>时间</th></tr>'
    + rows + '</table></div></details>';
}}
let gexModalSymbol = null;
let gexModalData = null;
function openGexModal(symbol) {{
  gexModalSymbol = symbol;
  let modal = document.getElementById('gex-modal');
  if (!modal) {{
    modal = document.createElement('div');
    modal.id = 'gex-modal';
    modal.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:1000;overflow:auto;padding:30px';
    modal.innerHTML = '<div style="background:#fff;margin:0 auto;max-width:980px;border-radius:14px;padding:20px">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:10px">'
      + '<b id="gex-modal-title" style="font-size:16px">Dealer Chart</b>'
      + '<div style="white-space:nowrap"><button type="button" class="secondary" data-zoom="-1" style="padding:4px 12px">−</button>'
      + '<button type="button" class="secondary" data-zoom="1" style="padding:4px 12px">＋</button>'
      + '<button type="button" id="gex-modal-close" style="padding:4px 14px;margin-left:6px">关闭</button></div></div>'
      + '<div><b>价格日K（近30日）</b><div id="gex-modal-candle" style="overflow-x:auto"></div></div>'
      + '<div style="margin-top:10px"><b>GEX 曲线</b><div id="gex-modal-body" style="overflow-x:auto"></div></div>'
      + '<div id="gex-modal-flows" style="margin-top:10px"></div>'
      + '<details style="margin-top:10px"><summary>GEX Heatmap（Strike × 到期，绿正红负，颜色越深 |GEX| 越大）</summary><div id="gex-modal-heat" style="overflow-x:auto"></div></details>'
      + '<p class="muted" style="font-size:11px;margin-top:8px">蜡烛：红涨绿跌｜K线图上圆点 = 异常期权事件（红 PUT / 绿 CALL，悬停看合约）｜绿柱 = Call GEX（上方墙）｜红柱 = Put GEX（下方墙）｜蓝线 = 累计总 GEX｜橙线 = 零 Gamma（Flip）｜灰虚线 = 当前价｜−/＋ 同时缩放 K 线与 GEX 曲线</p>'
      + '</div>';
    document.body.appendChild(modal);
    modal.querySelector('#gex-modal-close').addEventListener('click', function(){{ modal.style.display = 'none'; }});
    document.getElementById('gex-modal-candle').addEventListener('click', function(e) {{
      const btn = e.target.closest('button[data-layer]');
      if (!btn || !gexModalData) return;
      const key = btn.getAttribute('data-layer');
      gexLayers[key] = gexLayers[key] === false;
      renderCandleChart(document.getElementById('gex-modal-candle'), gexModalData);
    }});
    document.getElementById('gex-modal-candle').addEventListener('mousemove', function(e) {{
      const hits = window.gexCandleHits;
      const svgEl = this.querySelector('svg');
      const tip = this.querySelector('#gex-tip');
      const cross = this.querySelector('#gex-cross');
      if (!hits || !svgEl || !tip || !cross) return;
      const rect = svgEl.getBoundingClientRect();
      if (!rect.width) return;
      const vb = svgEl.viewBox.baseVal;
      const mx = (e.clientX - rect.left) / rect.width * vb.width;
      const my = (e.clientY - rect.top) / rect.height * vb.height;
      let text = null;
      for (let i = 0; i < hits.dots.length; i++) {{
        const d = hits.dots[i];
        if (Math.abs(d.cx - mx) <= 8 && Math.abs(d.cy - my) <= 8) {{ text = d.text; break; }}
      }}
      if (!text) for (let i = 0; i < hits.lines.length; i++) {{
        if (Math.abs(hits.lines[i].y - my) <= 7) {{ text = hits.lines[i].text; break; }}
      }}
      if (!text) for (let i = 0; i < hits.maSeries.length; i++) {{
        const s = hits.maSeries[i];
        for (let j = 0; j < s.pts.length; j++) {{
          const p = s.pts[j];
          if (Math.abs(p.x - mx) <= 8 && Math.abs(p.y - my) <= 8) {{
            text = s.label + ' $' + Number(p.price).toFixed(2); break;
          }}
        }}
        if (text) break;
      }}
      let candle = null;
      for (let i = 0; i < hits.candles.length; i++) {{
        if (Math.abs(hits.candles[i].cx - mx) <= 8) {{ candle = hits.candles[i]; break; }}
      }}
      if (candle) {{
        const d = new Date(candle.t);
        const up = candle.c >= candle.o;
        const chg = candle.o ? ((candle.c - candle.o) / candle.o * 100).toFixed(2) : '';
        const ohlc = (d.getMonth() + 1) + '/' + d.getDate() + ' 开 ' + candle.o + ' 高 ' + candle.h + ' 低 ' + candle.l + ' 收 ' + candle.c + (chg ? ' ' + (up ? '+' : '') + chg + '%' : '');
        text = text || ohlc;
        cross.setAttribute('x1', candle.cx); cross.setAttribute('x2', candle.cx);
        cross.setAttribute('y1', 16); cross.setAttribute('y2', vb.height - 30);
        cross.setAttribute('display', '');
      }} else {{
        cross.setAttribute('display', 'none');
      }}
      if (text) {{
        tip.textContent = text;
        const scale = rect.width / vb.width;
        let left = mx * scale + 14;
        const tipW = 250;
        if (left + tipW > rect.width) left = mx * scale - tipW - 14;
        tip.style.left = Math.max(0, left) + 'px';
        tip.style.top = Math.max(0, my * scale + 12) + 'px';
        tip.style.display = 'block';
      }} else {{
        tip.style.display = 'none';
      }}
    }});
    document.getElementById('gex-modal-candle').addEventListener('mouseleave', function() {{
      const tip = this.querySelector('#gex-tip');
      const cross = this.querySelector('#gex-cross');
      if (tip) tip.style.display = 'none';
      if (cross) cross.setAttribute('display', 'none');
    }});
    document.getElementById('gex-modal-body').addEventListener('mousemove', function(e) {{
      const hits = window.gexCurveHits;
      const svgEl = this.querySelector('svg');
      const tip = this.querySelector('#gex-curve-tip');
      if (!hits || !svgEl || !tip) return;
      const rect = svgEl.getBoundingClientRect();
      if (!rect.width) return;
      const vb = svgEl.viewBox.baseVal;
      const mx = (e.clientX - rect.left) / rect.width * vb.width;
      let best = null;
      for (let i = 0; i < hits.length; i++) {{
        if (Math.abs(hits[i].x - mx) <= 10) {{ best = hits[i]; break; }}
      }}
      if (best) {{
        tip.textContent = '行权价 $' + best.strike + ' · Net GEX ' + Number(best.gex).toFixed(1) + ' · 累计 ' + Number(best.cum).toFixed(1);
        const scale = rect.width / vb.width;
        let left = best.x * scale + 14;
        if (left + 250 > rect.width) left = best.x * scale - 264;
        tip.style.left = Math.max(0, left) + 'px';
        tip.style.top = Math.max(0, (e.clientY - rect.top) + 12) + 'px';
        tip.style.display = 'block';
      }} else {{
        tip.style.display = 'none';
      }}
    }});
    document.getElementById('gex-modal-body').addEventListener('mouseleave', function() {{
      const tip = this.querySelector('#gex-curve-tip');
      if (tip) tip.style.display = 'none';
    }});
    modal.querySelectorAll('button[data-zoom]').forEach(function(zb) {{
      zb.addEventListener('click', function() {{
        gexZoom = Math.max(0.5, Math.min(4, gexZoom + parseFloat(zb.getAttribute('data-zoom')) * 0.5));
        if (!gexModalData) return;
        renderCandleChart(document.getElementById('gex-modal-candle'), gexModalData);
        renderGexChart(document.getElementById('gex-modal-body'), gexModalData.gex || gexModalData);
      }});
    }});
    modal.addEventListener('click', function(e){{ if (e.target === modal) modal.style.display = 'none'; }});
  }}
  modal.style.display = 'block';
  document.getElementById('gex-modal-title').textContent = 'Dealer Chart - ' + symbol;
  const candleEl = document.getElementById('gex-modal-candle');
  const gexEl = document.getElementById('gex-modal-body');
  const flowEl = document.getElementById('gex-modal-flows');
  const heatEl = document.getElementById('gex-modal-heat');
  candleEl.innerHTML = '<span class="muted">加载中…</span>';
  gexEl.innerHTML = '<span class="muted">加载中…</span>';
  flowEl.innerHTML = '';
  heatEl.innerHTML = '';
  dealerFetch(symbol).then(function(data) {{
    gexModalData = data;
    renderCandleChart(candleEl, data);
    renderGexChart(gexEl, data.gex || data);
    renderFlowList(flowEl, data);
    renderHeatmap(heatEl, data.gex || data);
  }}).catch(function() {{
    candleEl.innerHTML = '<span class="muted">加载失败</span>';
    gexEl.innerHTML = '<span class="muted">加载失败</span>';
  }});
}}
document.querySelectorAll('button[data-gex]').forEach(function(btn) {{
  btn.addEventListener('click', function(){{ openGexModal(btn.getAttribute('data-gex')); }});
}});
(function(){{
  const cells = document.querySelectorAll('.spark[data-spark]');
  if (!cells.length || !('IntersectionObserver' in window)) return;
  function sparkSvg(bars) {{
    if (!bars || bars.length < 2) return '<span class="muted">—</span>';
    const W = 110, H = 30, pad = 3;
    const closes = bars.map(function(b){{ return Number(b.c); }});
    const min = Math.min.apply(null, closes), max = Math.max.apply(null, closes);
    const span = (max - min) || 1;
    const x = function(i){{ return pad + i / (closes.length - 1) * (W - pad * 2); }};
    const y = function(v){{ return pad + (1 - (v - min) / span) * (H - pad * 2); }};
    let pts = '';
    closes.forEach(function(v, i){{ pts += (i ? ' ' : '') + x(i).toFixed(1) + ',' + y(v).toFixed(1); }});
    const up = closes[closes.length - 1] >= closes[0];
    const color = up ? '#d70015' : '#00a651';
    const lastY = y(closes[closes.length - 1]).toFixed(1);
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:110px;height:30px">'
      + '<line x1="' + pad + '" y1="' + lastY + '" x2="' + (W - pad) + '" y2="' + lastY + '" stroke="#e8e8ed" stroke-width="0.6" stroke-dasharray="1,2"/>'
      + '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.5"/></svg>';
  }}
  const observer = new IntersectionObserver(function(entries) {{
    entries.forEach(function(entry) {{
      if (!entry.isIntersecting) return;
      const el = entry.target;
      observer.unobserve(el);
      const symbol = el.getAttribute('data-spark');
      el.textContent = '…';
      fetch('/api/spark?symbol=' + encodeURIComponent(symbol)).then(function(r) {{ return r.json(); }})
        .then(function(data) {{ el.innerHTML = sparkSvg(data.bars || []); }})
        .catch(function() {{ el.textContent = '—'; }});
    }});
  }}, {{ rootMargin: '120px' }});
  cells.forEach(function(el) {{ observer.observe(el); }});
}})();
(function(){{
  const rows=document.querySelectorAll('tr.analyst-row');
  if(!rows.length) return;
  function abtHorizon(){{
    const inp=document.querySelector('input[name="horizon_days"]');
    if(!inp) return 5;
    const v=inp.value.trim();
    return v===''?0:(parseInt(v,10)||5);
  }}
  const exitNames={{'stop-loss':'止损','take-profit':'止盈','holding-limit':'持有到期','no-complete-bar':'无K线','no-fill':'未成交'}};
  function esc(s){{ const d=document.createElement('div'); d.textContent=s==null?'':String(s); return d.innerHTML; }}
  function renderDetail(detail,data){{
    const items=(data&&data.outcomes)||[];
    if(!items.length){{ detail.querySelector('div').innerHTML='<span class="muted">暂无逐笔数据</span>'; return; }}
    let html='<div class="table-wrap"><table><tr><th>交易日</th><th>标的</th><th>信号合约</th><th>方向</th><th>ATM合约</th><th>正股盈亏</th><th>卖方盈亏</th><th>权利金</th><th>退出</th></tr>';
    items.forEach(function(o){{
      const sp=o.stock_pnl_pct; const pp=o.strategy_pnl_pct; const pr=o.strategy_premium_pct;
      function cls(v){{ return v==null?'muted':(v>=0?'up':'down'); }}
      function pct(v){{ return v==null?'—':'<span class="'+cls(v)+'">'+(v*100).toFixed(2)+'%</span>'; }}
      function prem(v){{ return v==null?'—':'<span class="'+cls(v)+'">'+(v*100).toFixed(0)+'%</span>'; }}
      const dir=o.direction==='BULL'?'<span class="badge up">看多</span>':(o.direction==='BEAR'?'<span class="badge down">看空</span>':esc(o.direction));
      const exit=exitNames[o.strategy_exit_reason]||esc(o.strategy_exit_reason||'—');
      html+='<tr><td>'+esc(o.session_date)+'</td><td>'+esc(o.symbol)+'</td><td class="muted">'+esc(o.contract_key)+'</td><td>'+dir+'</td><td class="muted">'+esc(o.atm_ticker)+'</td><td>'+pct(sp)+'</td><td>'+pct(pp)+'</td><td>'+prem(pr)+'</td><td>'+exit+'</td></tr>';
    }});
    html+='</table></div>';
    detail.querySelector('div').innerHTML=html;
  }}
  rows.forEach(function(row){{
    row.style.cursor='pointer';
    row.addEventListener('click',async function(){{
      const analyst=row.getAttribute('data-analyst');
      const detail=document.getElementById('detail-'+analyst);
      if(!detail) return;
      if(detail.style.display!=='none'){{ detail.style.display='none'; return; }}
      detail.style.display='';
      if(detail.getAttribute('data-loaded')==='1') return;
      detail.querySelector('div').textContent='加载中…';
      try{{
        const url='/api/analyst-backtest-detail?analyst='+encodeURIComponent(analyst)+'&horizon_days='+abtHorizon();
        const resp=await fetch(url); const data=await resp.json();
        renderDetail(detail,data);
        detail.setAttribute('data-loaded','1');
      }}catch(e){{ detail.querySelector('div').textContent='加载失败：'+e; }}
    }});
  }});
  const expandAll=document.getElementById('analyst-expand');
  const collapseAll=document.getElementById('analyst-collapse');
  if(expandAll) expandAll.addEventListener('click',function(){{ rows.forEach(function(r){{ r.click(); }}); }});
  if(collapseAll) collapseAll.addEventListener('click',function(){{
    rows.forEach(function(r){{ const d=document.getElementById('detail-'+r.getAttribute('data-analyst')); if(d) d.style.display='none'; }});
  }});
}})();
(function(){{
  const drawBtn=document.getElementById('audit-sample');
  if(!drawBtn) return;
  const nInput=document.getElementById('audit-n');
  const resultEl=document.getElementById('audit-result');
  const summaryEl=document.getElementById('audit-summary');
  function esc(s){{ const d=document.createElement('div'); d.textContent=s==null?'':String(s); return d.innerHTML; }}
  function badge(v,cls){{ return '<span class="badge '+cls+'">'+esc(v)+'</span>'; }}
  function dirBadge(d){{ return d==='BULL'?badge('看多','up'):(d==='BEAR'?badge('看空','down'):badge(d||'—','watch')); }}
  function renderSample(s){{
    const o=s.outcome||{{}};
    const pnl=o.strategy_pnl_pct; const dc=o.direction_correct; const uc=o.underlying_change_pct;
    const pnlHtml=pnl==null?'—':'<span class="'+(pnl>=0?'up':'down')+'">'+(pnl*100).toFixed(2)+'%</span>';
    const dcHtml=dc==null?'—':(dc===1?'<span class="up">对</span>':'<span class="down">错</span>');
    return '<div class="card" style="padding:14px">'
      +'<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
      +'<strong>'+esc(s.symbol)+'</strong><span class="muted">'+esc(s.contract_key)+'</span>'
      +dirBadge(s.direction)+'<span class="muted">分析师 '+esc(s.analyst)+'</span>'
      +'<span class="muted">决策 '+esc(s.decision)+'</span>'
      +'<span class="muted">正股 '+((uc==null)?'—':(uc*100).toFixed(2)+'%')+'</span>'
      +'<span class="muted">方向 '+dcHtml+'</span>'
      +'<span class="muted">卖方盈亏 '+pnlHtml+'</span>'
      +'<span class="muted">退出 '+esc(o.strategy_exit_reason||'—')+'</span>'
      +'<span class="muted">ATM '+esc(o.atm_ticker||'—')+'</span>'
      +'<button type="button" class="secondary audit-review" data-id="'+s.signal_id+'" style="margin:0">AI 复核</button>'
      +'</div>'
      +'<details style="margin-top:8px"><summary class="muted">原文本</summary><pre style="max-height:200px">'+esc(s.raw_text)+'</pre></details>'
      +'<div class="audit-verdict" data-id="'+s.signal_id+'" style="margin-top:6px"></div>'
      +'</div>';
  }}
  async function review(id,btn,verdictEl){{
    btn.disabled=true; btn.textContent='复核中…';
    try{{
      const r=await fetch('/api/audit-review?signal_id='+id);
      const d=await r.json();
      if(d.status==='ok'){{
        verdictEl.innerHTML='<p style="margin:6px 0 0">现有解析：决策 '+esc(d.existing.decision)+'、方向 '+esc(d.existing.direction)+'</p>'
          +'<p class="card" style="margin:6px 0 0;padding:10px">'+esc(d.verdict)+'</p>';
      }} else {{
        verdictEl.innerHTML='<p class="error" style="margin:6px 0 0">复核失败：'+esc(d.message||d.status)+'</p>';
      }}
    }}catch(e){{ verdictEl.innerHTML='<p class="error" style="margin:6px 0 0">请求失败：'+e+'</p>'; }}
    finally{{ btn.disabled=false; btn.textContent='AI 复核'; }}
  }}
  drawBtn.addEventListener('click',async function(){{
    drawBtn.disabled=true; drawBtn.textContent='抽取中…'; resultEl.textContent='';
    try{{
      const n=parseInt(nInput.value,10)||50;
      const r=await fetch('/api/audit-sample?n='+n+'&seed=42');
      const d=await r.json();
      const samples=d.samples||[];
      resultEl.innerHTML=samples.map(renderSample).join('');
      summaryEl.textContent='已抽取 '+samples.length+' 个信号（种子 42）';
    }}catch(e){{ resultEl.textContent='抽取失败：'+e; }}
    finally{{ drawBtn.disabled=false; drawBtn.textContent='随机抽取'; }}
  }});
  document.addEventListener('click',function(e){{
    const btn=e.target.closest('.audit-review');
    if(!btn) return;
    const id=btn.getAttribute('data-id');
    const verdict=document.querySelector('.audit-verdict[data-id="'+id+'"]');
    review(id,btn,verdict);
  }});
  const reviewAll=document.getElementById('audit-review-all');
  if(reviewAll) reviewAll.addEventListener('click',function(){{
    const buttons=Array.from(document.querySelectorAll('.audit-review'));
    buttons.forEach(function(btn){{
      if(!btn.disabled){{
        const id=btn.getAttribute('data-id');
        const verdict=document.querySelector('.audit-verdict[data-id="'+id+'"]');
        review(id,btn,verdict);
      }}
    }});
  }});
}})();
</script></body></html>'''

    def _login_page(self, message: str = "") -> str:
        notice = f'<p class="error">{html.escape(message)}</p>' if message else ""
        if self.app.admin_configured():
            content = (
                "<h1>异常期权助手</h1><p class=\"sub\">使用管理员账户登录（管理口令仍可备用）。</p>"
                f'{notice}<div class="card"><form method="post" action="/login">'
                '<label>用户名</label><input name="username" type="text" required autofocus>'
                '<label>密码</label><input name="password" type="password" required>'
                '<button>进入管理面板</button></form>'
                '<details class="card" style="margin-top:12px"><summary>使用管理口令登录（备用）</summary>'
                '<form method="post" action="/login"><label>管理口令</label>'
                '<input name="token" type="password"><button>进入</button></form></details></div>'
            )
        else:
            content = (
                "<h1>异常期权助手</h1><p class=\"sub\">首次使用：先创建管理员账户，之后用它登录。</p>"
                f'{notice}<div class="card"><form method="post" action="/admin-init">'
                '<label>管理员用户名</label><input name="username" type="text" required autofocus>'
                '<label>密码（至少8位）</label><input name="password" type="password" required>'
                '<label>确认密码</label><input name="confirm" type="password" required>'
                '<button>创建管理员账户</button></form></div>'
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
        action_cards = ""
        if path == "/":
            actions = [("/api/actions/collect", "立即采集并生成推荐"), ("/api/actions/report", "生成飞书日报")]
            if isinstance(data, Mapping) and data.get("is_latest"):
                actions.append(("/api/actions/reevaluate", "刷新评分"))
            action_cards = self._action_forms(csrf, tuple(actions), reload_paths={"/api/actions/reevaluate"})
        elif path == "/portfolio":
            action_cards = self._action_forms(csrf, (("/futu/import-watchlist", "从富途导入自选"), ("/api/portfolio/refresh", "手动刷新持仓")))
        elif path == "/signals":
            action_cards = self._action_forms(csrf, (("/api/actions/flow-classify", "分类Flow类型"),), reload_paths={"/api/actions/flow-classify"})
        elif path == "/system":
            action_cards = self._action_forms(csrf, (
                ("/api/actions/collect", "手动采集"),
                ("/api/actions/reevaluate", "刷新评分"),
                ("/futu/sync", "富途同步"),
                ("/api/ibkr/sync", "同步IBKR持仓"),
                ("/api/providers/massive/test", "测试Massive"),
                ("/api/actions/deepseek-test", "测试DeepSeek"),
                ("/api/actions/feishu-test", "测试飞书"),
                ("/api/actions/discord-login", "Discord扫码登录"),
                ("/api/actions/backup", "创建备份"),
                ("/api/actions/gex-snapshot", "记录GEX快照"),
                ("/api/actions/alerts-check", "检查GEX预警"),
            ))
        elif path == "/providers":
            action_cards = self._action_forms(csrf, (("/api/ibkr/sync", "同步 IBKR 持仓"),))
        content = f'<h1>{html.escape(title)}</h1><p class="sub">{html.escape(description)}</p>'
        sidebar_bits = []
        if action_cards:
            sidebar_bits.append(action_cards)
        date_selector = self._date_selector(path, params)
        if date_selector:
            sidebar_bits.append(date_selector)
        if sidebar_bits:
            content += (
                '<div class="layout">'
                '<div class="main-col">'
                f'{self._visual_summary(path, data, params, csrf)}'
                f'<details class="card"><summary>查看原始数据</summary><pre>{encoded}</pre></details>'
                '</div>'
                '<aside class="sidebar">'
                f'<div id="action-result"></div>'
                + "".join(sidebar_bits)
                + '</aside></div>'
            )
        else:
            content += (
                f'<div id="action-result"></div>{self._visual_summary(path, data, params, csrf)}'
                f'<details class="card"><summary>查看原始数据</summary><pre>{encoded}</pre></details>'
            )
        return self._shell(f"{title} - Options Radar", content, path)

    @staticmethod
    def _date_selector(path: str, params: Mapping[str, str]) -> str:
        if path not in {"/", "/signals"}:
            return ""
        if str(params.get("symbol", "") or "").strip():
            # Symbol filter spans the last 30 days; the date selector is N/A.
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
    def _visual_summary(path: str, data: Any, params: Optional[Mapping[str, str]] = None, csrf: str = "") -> str:
        if path == "/providers":
            return SetupRequestHandler._provider_actions(csrf)
        if path == "/audit":
            return (
                '<section class="card"><h2>数据复核</h2>'
                '<p class="muted">随机抽样 TRADE 信号，对比原文本、解析方向与回测结果，用 AI 重新解析原文本以发现系统性解析错误。</p>'
                '<div class="card toolbar">'
                '<label style="margin:0;font-weight:600">抽样数量</label>'
                '<input type="number" id="audit-n" value="50" min="1" max="200" style="width:90px;margin:0">'
                '<button type="button" id="audit-sample">随机抽取</button>'
                '<button type="button" class="secondary" id="audit-review-all">全部 AI 复核</button>'
                '<span class="muted" id="audit-summary"></span>'
                '</div>'
                '<div id="audit-result" style="margin-top:12px"></div></section>'
            )
        if path == "/" and isinstance(data, Mapping):
            sell = data.get("sell") if isinstance(data.get("sell"), list) else []
            buy = data.get("buy") if isinstance(data.get("buy"), list) else []
            if not sell and not buy:
                return '<section class="card"><h2>今日暂无候选</h2><p class="muted">点击“立即采集并生成推荐”，或等待新的异常期权事件。</p></section>'
            from options_radar.timeutil import us_cash_session_label
            session_label = us_cash_session_label()
            market_banner = ""
            if session_label != "交易中":
                market_banner = (
                    f'<div class="market-banner">当前<b>{html.escape(session_label)}</b>，'
                    '行情为最近收盘快照，bid/ask 与 OI 暂不新鲜，开市后自动刷新为实时数据。</div>'
                )
            is_latest = bool(data.get("is_latest", True))
            snapshot_note = "" if is_latest else (
                '<div class="card" style="margin:10px 0;border-left:3px solid #d97706">'
                '<b>历史快照</b>：此日期为历史数据，评分基于当日信号与行情，不随当前权重/行情重算；'
                '卡片只展示当日已落库的 GEX/策略提示，不注入实时行情。</div>'
            )
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
            def render_card(item: Mapping) -> str:
                key = html.escape(str(item.get("contract_key", "")))
                bid = format_price(item.get("bid"))
                ask = format_price(item.get("ask"))
                entry = format_price(item.get("max_entry_price"))
                take_profit = format_price(item.get("take_profit"))
                stop_loss = format_price(item.get("stop_loss"))
                direction = str(item.get("direction", "-"))
                badge_cls = "up" if direction in {"BULL", "看多", "多"} else "down" if direction in {"BEAR", "看空", "空"} else "watch"
                badge = f'<span class="badge {badge_cls}">{html.escape(direction)}</span>'
                # Flow-only = no scored recommendation yet (dashboard falls back to raw flow).
                # Do NOT key off analyst_count: scored cards also expose vote counts.
                dq = str(item.get("data_quality", "") or "")
                flow_only = dq in {"仅flow", "flow"} or str(item.get("market_status", "")) == "flow"
                if flow_only:
                    source_badge = '<span class="badge watch">仅flow</span>'
                else:
                    n_votes = item.get("analyst_count")
                    if n_votes is None:
                        votes = item.get("votes") if isinstance(item.get("votes"), list) else []
                        n_votes = len(votes) if votes else None
                    source_badge = (
                        f'<span class="badge watch">分析师{html.escape(str(n_votes))}</span>'
                        if n_votes is not None else '<span class="badge watch">已评分</span>'
                    )
                score = item.get("score")
                score_html = f'{float(score):.1f}' if isinstance(score, (int, float)) else html.escape(str(score))
                grade = html.escape(str(item.get("grade", "-")))
                premium_text = format_premium(item.get("premium"))
                data_quality = str(item.get("data_quality", item.get("market_status", "待行情")))
                market_state = "行情待刷新"
                if data_quality in {"native", "realtime", "ok"}:
                    market_state = "实时行情"
                elif data_quality in {"仅flow", "flow"}:
                    market_state = "待采集"
                exec_status = str(item.get("execution_status", "待行情"))
                state_cls = "ok" if exec_status == "可执行" else ("flow" if flow_only else "warn")
                state_badge = f'<span class="rec-state {state_cls}">{html.escape(exec_status)}</span>'
                # ⓘ tooltip: analyst votes / score components / risks / IV / explanation.
                reason = str(item.get("reason", ""))
                tip_parts = []
                if "分析师判断" in reason:
                    vote_part = reason.split("评分组成", 1)[0].replace("分析师判断：", "").strip()
                    vote_lines = [html.escape(vote.strip().rstrip("；")) for vote in vote_part.split("、") if vote.strip()]
                    if vote_lines:
                        tip_parts.append("<b>分析师投票</b><br>" + "<br>".join(vote_lines))
                if "评分组成" in reason:
                    components = reason.split("评分组成：", 1)[1].split("风险提示", 1)[0].strip().rstrip("；")
                    if components:
                        tip_parts.append("<b>评分组成</b><br>" + html.escape(components))
                HOLDING_PATTERNS = ("行情时间戳已过期", "盘口与OI为过期数据", "暂未取到实时行情", "等待数据源恢复")
                if "风险提示" in reason:
                    risks = reason.split("风险提示：", 1)[1].split("；执行字段", 1)[0].split("；")
                    real_risks = [r.strip() for r in risks if r.strip() and not any(
                        pattern in r for pattern in HOLDING_PATTERNS
                    )]
                    if real_risks:
                        tip_parts.append("<b>风险提示</b><br>" + "<br>".join(
                            f"⚠ {html.escape(r)}" for r in real_risks
                        ))
                vol = item.get("vol") if isinstance(item.get("vol"), Mapping) else None
                if vol and (vol.get("atm_iv") is not None or vol.get("put_skew") is not None or vol.get("call_skew") is not None):
                    vol_parts = []
                    if vol.get("atm_iv") is not None:
                        vol_parts.append(f"ATM IV {float(vol['atm_iv']):.0f}%")
                    if vol.get("put_skew") is not None:
                        vol_parts.append(f"Put Skew {float(vol['put_skew']):+.1f}")
                    if vol.get("call_skew") is not None:
                        vol_parts.append(f"Call Skew {float(vol['call_skew']):+.1f}")
                    term = vol.get("term_structure")
                    if isinstance(term, Mapping) and term:
                        term_items = []
                        for expiry, iv in sorted(term.items()):
                            label = str(expiry)[5:].replace("-", "/")
                            try:
                                term_items.append(f"{label} {float(iv):.0f}%")
                            except (TypeError, ValueError):
                                continue
                        if term_items:
                            vol_parts.append("期限 " + " · ".join(term_items))
                    tip_parts.append("<b>IV / Skew</b><br>" + html.escape(
                        " · ".join(vol_parts) + "（Put Skew 正=下跌保护贵，卖 Put 更划算）"
                    ))
                short_gamma_plan = str(item.get("short_gamma_plan") or "")
                if short_gamma_plan:
                    tip_parts.append("<b>Short Gamma 风控</b><br>" + html.escape(short_gamma_plan))
                iv_rank_val = item.get("iv_rank")
                if isinstance(iv_rank_val, (int, float)):
                    iv_label = "历史高位" if iv_rank_val >= 70 else "历史低位" if iv_rank_val <= 30 else "历史中位"
                    tip_parts.append(f"IV Rank {iv_rank_val:.0f}/100（{iv_label}，数值越低 IV 越便宜）")
                ps = item.get("price_structure") if isinstance(item.get("price_structure"), Mapping) else None
                if ps and any(ps.get(k) is not None for k in ("ma20", "ma50", "high", "low")):
                    ps_parts = []
                    if ps.get("spot") is not None:
                        ps_parts.append(f"现价 ${float(ps['spot']):g}")
                    if ps.get("ma20") is not None:
                        ps_parts.append(f"MA20 ${float(ps['ma20']):g}")
                    if ps.get("ma50") is not None:
                        ps_parts.append(f"MA50 ${float(ps['ma50']):g}")
                    if ps.get("high") is not None:
                        ps_parts.append(f"20日高 ${float(ps['high']):g}")
                    if ps.get("low") is not None:
                        ps_parts.append(f"20日低 ${float(ps['low']):g}")
                    tip_parts.append("<b>价格结构</b><br>" + html.escape(" · ".join(ps_parts)))
                explanation = reason
                for marker in ("分析师判断：", "评分组成：", "风险提示："):
                    explanation = explanation.split(marker, 1)[0]
                explanation = explanation.strip().rstrip("；")
                if explanation:
                    tip_parts.append("<b>解释</b><br>" + html.escape(explanation))
                info_html = ""
                if tip_parts:
                    tip_html = "<br><br>".join(tip_parts)
                    tip_plain = re.sub(r"<[^>]+>", " ", tip_html)
                    info_html = (
                        '<span class="info" title="' + html.escape(tip_plain, quote=True) + '">ⓘ'
                        '<span class="tip">' + tip_html + '</span></span>'
                    )
                # 策略块：主策略 + 行权价/到期提示 + 风险警告分行展示，突出重点。
                strategy_hint = str(item.get("strategy_hint", ""))
                strike_hint = str(item.get("strike_hint", ""))
                event_risk = str(item.get("event_risk", ""))
                next_earn = item.get("next_earnings_days")
                expected_move = item.get("expected_move")
                short_gamma_risk = str(item.get("short_gamma_risk", ""))
                strategy_block = ""
                if strategy_hint:
                    lines = [f'<div class="rec-strategy-main"><b>策略</b> {html.escape(strategy_hint)}</div>']
                    if strike_hint:
                        lines.append(f'<div class="rec-hint">🎯 {html.escape(strike_hint)}</div>')
                    expiration_hint = str(item.get("expiration_hint") or "")
                    if expiration_hint:
                        lines.append(f'<div class="rec-hint">📅 {html.escape(expiration_hint)}</div>')
                    if event_risk == "HIGH":
                        earn_txt = f"财报 {int(next_earn)} 天后" if isinstance(next_earn, (int, float)) else "财报临近"
                        move_txt = f"预期波动 {expected_move:.1f}%" if isinstance(expected_move, (int, float)) else ""
                        lines.append(f'<div class="rec-warn">⚠ 事件风险 HIGH（{html.escape(earn_txt)}{" " + html.escape(move_txt) if move_txt else ""}）</div>')
                    if short_gamma_risk == "HIGH":
                        lines.append('<div class="rec-warn">⚠ Short Gamma 风险 HIGH，避免裸卖</div>')
                    strategy_block = '<div class="rec-strategy">' + "".join(lines) + '</div>'
                gex = item.get("gex") if isinstance(item.get("gex"), Mapping) else None
                gex_segment = ""
                if gex:
                    symbol = str(item.get("contract_key", "")).split("|", 1)[0].split(".", 1)[-1]
                    regime_map = {"positive": "正 Gamma（抑制波动）", "negative": "负 Gamma（放大波动）", "mixed": "混合 Gamma（敞口平衡）"}
                    regime_text = regime_map.get(str(gex.get("regime", "")), "—")
                    wall_chips = []
                    _pw_v = gex.get("put_wall")
                    _cw_v = gex.get("call_wall")
                    _flip_v = gex.get("gamma_flip")
                    if _pw_v is not None:
                        wall_chips.append(f'<span class="wall-chip" style="color:#d70015;border-color:#f0c8cc">PW ${float(_pw_v):g}</span>')
                    if _cw_v is not None:
                        wall_chips.append(f'<span class="wall-chip" style="color:#00a651;border-color:#c8e8d5">CW ${float(_cw_v):g}</span>')
                    if _flip_v is not None:
                        wall_chips.append(f'<span class="wall-chip" style="color:#c47f00;border-color:#f0e0c0">Flip ${float(_flip_v):g}</span>')
                    else:
                        wall_chips.append('<span class="wall-chip" style="color:#86868b">无 Flip</span>')
                    # 位置条：有 spot 就渲染，单侧 wall 缺失显示「—」。
                    position_bar = ""
                    try:
                        if gex.get("spot") is not None:
                            _spot = float(gex.get("spot"))
                            _pw = float(gex["put_wall"]) if gex.get("put_wall") is not None else None
                            _cw = float(gex["call_wall"]) if gex.get("call_wall") is not None else None
                            pw_label = f"Put Wall ${_pw:g}" if _pw is not None else "Put Wall —"
                            cw_label = f"Call Wall ${_cw:g}" if _cw is not None else "Call Wall —"
                            if _pw is not None and _cw is not None and _cw > _pw > 0:
                                _pct = max(0.0, min(100.0, (_spot - _pw) / (_cw - _pw) * 100.0))
                            elif _pw is not None:
                                _pct = 0.0 if _spot <= _pw else 100.0
                            elif _cw is not None:
                                _pct = 100.0 if _spot >= _cw else 0.0
                            else:
                                _pct = None
                            marker = ('<div style="position:absolute;left:{0}%;top:-4px;width:3px;height:14px;background:#1d1d1f;border-radius:1px"></div>'.format(round(_pct, 1)) if _pct is not None else "")
                            note = f"当前价 ${_spot:g}" + (f" · 位置 {round(_pct)}%" if _pct is not None else "")
                            position_bar = (
                                '<div style="margin-top:6px;flex-basis:100%">'
                                '<div style="display:flex;justify-content:space-between;font-size:11px;color:#86868b">'
                                f"<span>{pw_label}</span><span>{cw_label}</span></div>"
                                '<div style="position:relative;height:6px;background:linear-gradient(90deg,#d70015,#f0a500,#00a651);border-radius:3px;margin:3px 0">'
                                + marker + '</div>'
                                f'<div style="font-size:11px;color:#1d1d1f">{note}</div></div>'
                            )
                    except (TypeError, ValueError):
                        pass
                    gex_segment = (
                        '<div class="rec-gex">'
                        '<div class="rec-gex-head">'
                        f'<b>GEX</b><span class="muted">{html.escape(regime_text)}</span>'
                        f'<button type="button" class="secondary gex-btn" data-gex="{html.escape(symbol)}">曲线</button>'
                        '</div>'
                        f'<div class="rec-walls">{"".join(wall_chips)}</div>'
                        + position_bar
                        + '</div>'
                    )
                grade_html = f'<span class="grade">{grade}级</span>' if grade != "-" else ""
                raw_key = str(item.get("contract_key", ""))
                key_parts = raw_key.split("|")
                if len(key_parts) == 4:
                    rec_sym = html.escape(key_parts[0].split(".")[-1] or key_parts[0])
                    leg_type = "CALL" if str(key_parts[3]).upper() == "C" else "PUT"
                    rec_leg = html.escape(f"{key_parts[1][5:].replace('-', '/')} · ${key_parts[2]} {leg_type}")
                else:
                    rec_sym, rec_leg = key, ""
                return (
                    '<section class="card rec">'
                    '<div class="rec-top">'
                    f'<div class="rec-contract" title="{key}"><span class="rec-sym">{rec_sym}</span>'
                    + (f'<span class="rec-leg">{rec_leg}</span>' if rec_leg else '')
                    + '</div>'
                    f'<div class="rec-score"><span class="score-num">{score_html}</span>{grade_html}{info_html}</div>'
                    '</div>'
                    f'<div class="rec-meta">{badge}{source_badge}<span class="muted">交易量 {premium_text}</span>'
                    f'<span class="muted">{html.escape(market_state)}</span>{state_badge}</div>'
                    '<div class="rec-grid">'
                    f'<div class="rec-kv bidask"><b>bid/ask</b><span>{bid} / {ask}</span></div>'
                    f'<div class="rec-kv"><b>入场</b><span>{entry}</span></div>'
                    f'<div class="rec-kv"><b>止盈</b><span>{take_profit}</span></div>'
                    f'<div class="rec-kv"><b>止损</b><span>{stop_loss}</span></div>'
                    '</div>'
                    f'{strategy_block}'
                    f'{gex_segment}'
                    '</section>'
                )
            sections = []
            sell_block = ('<div class="grid">' + "".join(render_card(item) for item in sell) + '</div>') if sell else '<p class="muted">暂无卖方推荐（等待 DTE 14~60 的信号确认）</p>'
            sections.append('<section class="card"><h2>卖方推荐（DTE 14~60，卖 ATM 期权）</h2>' + sell_block + '</section>')
            return market_banner + snapshot_note + "".join(sections) + '<details class="card" style="margin-top:16px"><summary>评价标准说明</summary><p>候选从 Discord 异常期权/分析师频道采集（解析成交额、持仓、分析师卡片），再叠加以下五维评分。每个维度都用<strong>可验证的数据</strong>计算，不是主观打分：</p><table><tr><th>维度</th><th>权重</th><th>依据</th></tr><tr><td>权利金质量</td><td>40%</td><td>卖方 alpha 核心：<strong>IV 溢价</strong>（ATM 隐含波动率 − 正股历史波动率 VRP，越高越值得卖）+ 买卖价差 + Open Interest + 成交量</td></tr><tr><td>信号质量</td><td>20%</td><td>分析师卡片是否给出方向、置信度、入场/止盈/止损、理由等字段的完整程度</td></tr><tr><td>组合适配</td><td>15%</td><td>标的是否已在持仓/自选、仓位集中度是否过高——对应风控的集中度限制</td></tr><tr><td>方向共识</td><td>15%</td><td>多分析家族方向投票（90 天回测显示方向正确率≈50%，故降权，仅作参考）</td></tr><tr><td>历史胜率</td><td>10%</td><td>分析师卖方策略回测胜率动态校准（弱化）</td></tr></table><p>评级：A级≥80分（飞书提醒）｜B 65-79（合格）｜C 50-64（观察榜）｜D&lt;50（过滤）。仅flow=异常期权事件但分析师尚未确认。休市/行情缺失只影响「可执行性」，不压低信号评分。</p><p class="muted">数据来源：Discord 频道文本（成交额/分析师观点）、富途 OpenD 实时行情（盘口/OI/IV/希腊字母）、Alpaca 历史行情（回测）、DeepSeek 仅做翻译与文字整理，不参与任何数值决策。</p></details>'
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
                "quant_mean_reversion": "量化均值回归", "flow_positioning": "流价背离",
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
                classification = item.get("classification") or {}
                flow_type = str(classification.get("flow_type", "") or "")
                type_names = {"Directional": "方向性", "Hedging": "对冲", "Spread": "价差", "Closing": "平仓", "Unknown": "未知"}
                type_label = type_names.get(flow_type, flow_type or "—")
                type_cls = {"Directional": "up", "Hedging": "watch", "Spread": "watch", "Closing": "down"}.get(flow_type, "watch")
                type_html = f'<span class="badge {type_cls}">{html.escape(type_label)}</span>' if flow_type else '<span class="muted">—</span>'
                strength = classification.get("flow_strength")
                strength_html = f'{float(strength):.0f}' if isinstance(strength, (int, float)) else "—"
                rows.append(
                    '<tr data-filter="{0}"><td>{1}</td><td>{2}</td><td class="muted">{3}</td><td class="muted">{4}</td>'
                    '<td>{5}</td><td>{6}</td><td>{7}</td><td>{8}</td></tr>'
                    '<tr class="signal-detail"><td colspan="8">{9}</td></tr>'.format(
                        html.escape(filter_text),
                        html.escape(str(item.get("symbol", ""))),
                        html.escape(str(item.get("contract_key", ""))),
                        premium_text,
                        html.escape(str(item.get("observed_at", ""))),
                        direction_html,
                        badge,
                        type_html,
                        strength_html,
                        detail_html,
                    )
                )
            return (
                '<div class="card toolbar">'
                '<input type="search" id="signal-search" placeholder="搜索标的或合约…" style="max-width:320px"'
                + (f' value="{html.escape(str((params or {}).get("symbol", "")).strip())}"' if (params or {}).get("symbol") else '')
                + '>'
                '<select id="signal-dir" style="width:auto">'
                '<option value="all">方向：全部</option><option value="bull">看多</option>'
                '<option value="bear">看空</option><option value="neutral">中性</option></select>'
                '<select id="signal-status" style="width:auto">'
                '<option value="all">状态：全部</option><option value="has-analyst">分析师确认</option>'
                '<option value="flow-only">仅flow</option></select>'
                '<button type="button" class="secondary" id="signal-expand">全部展开</button>'
                '<button type="button" class="secondary" id="signal-collapse">全部折叠</button>'
                '</div>'
                '<section class="card" id="signal-table"><div class="table-wrap"><table>'
                '<tr><th>标的</th><th>合约</th><th>交易量</th><th>时间</th><th>方向</th><th>分析师意见数</th><th>类型</th><th>强度</th></tr>'
                + "".join(rows) + '</table></div></section>'
            )
        if path == "/portfolio" and isinstance(data, dict):
            def render_row(symbol, item):
                if not isinstance(item, Mapping):
                    return f'<tr><td>{html.escape(str(symbol))}</td><td colspan="11">{html.escape(str(item))}</td></tr>'
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
                iv_rank = item.get("iv_rank")
                iv_rank_str = f"{float(iv_rank):.0f}" if iv_rank is not None else "-"
                iv = item.get("iv")
                hv = item.get("hv_30d")
                hv_pct = (float(hv) * 100.0) if (hv is not None and float(hv) < 1.5) else hv
                if iv is not None and hv_pct is not None:
                    ivhv_str = f"{float(iv) - float(hv_pct):+.0f}"
                    ivhv_title = f"IV {float(iv):.0f}% · HV {float(hv_pct):.0f}%"
                else:
                    ivhv_str = "-"
                    ivhv_title = ""
                ivhv_cell = (
                    f'<td title="{html.escape(ivhv_title, quote=True)}">{html.escape(ivhv_str)}</td>'
                    if ivhv_title else f"<td>{html.escape(ivhv_str)}</td>"
                )
                gex_regime = str(item.get("gex_regime") or "")
                regime_map = {"positive": "正", "negative": "负", "mixed": "混合"}
                gex_str = regime_map.get(gex_regime, "-")
                filter_text = "|".join([
                    str(symbol).lower(), str(item.get("company_name", "") or "").lower(),
                    str(item.get("group_name", "") or "").lower(), held_token,
                ])
                spark_cell = f'<td><span class="spark" data-spark="{html.escape(str(symbol))}"></span></td>'
                gex_btn = f'<button type="button" class="secondary" style="padding:3px 10px;font-size:12px;margin:0" data-gex="{html.escape(str(symbol))}">GEX</button>'
                flow_link = (
                    f'<a class="button secondary" style="padding:3px 10px;font-size:12px;margin:0 0 0 6px" '
                    f'href="/signals?symbol={quote(str(symbol))}">期权事件</a>'
                    if item.get("has_flow") else ""
                )
                actions = gex_btn + flow_link
                row = '<tr data-filter="{0}"><td>{1}{2}</td><td class="muted">{3}</td><td class="muted">{4}</td><td>{5}</td><td>{6}</td><td class="{7}">{8}</td><td>{9}</td><td>{10}</td><td>{11}</td><td>{12}</td><td>{13}</td><td>{14}</td><td>{15}</td></tr>'.format(
                    html.escape(filter_text),
                    html.escape(str(symbol)), flag, display_name, industry,
                    html.escape(str(item.get("held_quantity", 0))),
                    price_str, change_cls, change_str, iv_rank_str, ivhv_cell, gex_str,
                    float(item.get("concentration", 0) or 0), spark_cell, actions, updated,
                )
                contracts = item.get("flow_contracts") or []
                if contracts:
                    detail = "".join(f'<li>{html.escape(str(c))}</li>' for c in contracts)
                    row += f'<tr class="flow-detail"><td colspan="13"><details><summary>相关异常期权事件</summary><ul>{detail}</ul></details></td></tr>'
                return row
            items = list(data.items())
            sort_key = str((params or {}).get("sort", "") or "")
            def pair_sort(pair):
                _symbol, value = pair
                if not isinstance(value, Mapping):
                    return (0, 0.0)
                if sort_key == "iv_rank":
                    v = value.get("iv_rank")
                    return (0 if v is not None else 1, -(float(v)) if v is not None else 0.0)
                if sort_key == "iv_hv":
                    iv_v = value.get("iv")
                    hv_v = value.get("hv_30d")
                    hv_pct_v = (float(hv_v) * 100.0) if (hv_v is not None and float(hv_v) < 1.5) else hv_v
                    diff = (float(iv_v) - float(hv_pct_v)) if (iv_v is not None and hv_pct_v is not None) else None
                    return (0 if diff is not None else 1, -diff if diff is not None else 0.0)
                if sort_key == "change":
                    v = value.get("change_pct")
                    return (0 if v is not None else 1, -(float(v)) if v is not None else 0.0)
                return (0, 0.0)
            flow_pairs = sorted(
                [p for p in items if isinstance(p[1], Mapping) and p[1].get("has_flow")], key=pair_sort,
            )
            other_pairs = sorted(
                [p for p in items if not (isinstance(p[1], Mapping) and p[1].get("has_flow"))], key=pair_sort,
            )
            flow_rows = [render_row(s, v) for s, v in flow_pairs]
            other_rows = [render_row(s, v) for s, v in other_pairs]
            sort_options = [
                ('<option value="">默认（标的）</option>'),
                (f'<option value="iv_rank"{" selected" if sort_key == "iv_rank" else ""}>IV Rank 高→低</option>'),
                (f'<option value="iv_hv"{" selected" if sort_key == "iv_hv" else ""}>IV-HV 高→低</option>'),
                (f'<option value="change"{" selected" if sort_key == "change" else ""}>涨跌 高→低</option>'),
            ]
            toolbar = (
                '<div class="card toolbar">'
                '<input type="search" id="portfolio-search" placeholder="搜索标的或公司名…" style="max-width:320px">'
                '<select id="portfolio-hold" style="width:auto"><option value="all">持仓：全部</option>'
                '<option value="held">有持仓</option><option value="no-hold">无持仓</option></select>'
                f'<select id="portfolio-sort" data-goto="/portfolio?sort=" style="width:auto">{"".join(sort_options)}</select>'
                '<button type="button" class="secondary" id="portfolio-expand">全部展开</button>'
                '<button type="button" class="secondary" id="portfolio-collapse">全部折叠</button>'
                '</div>'
            )
            sections = [toolbar]
            header = '<tr><th>标的</th><th>公司名称</th><th>分组</th><th>持仓</th><th>最新价</th><th>涨跌</th><th>IV Rank</th><th>IV-HV</th><th>GEX</th><th>集中度</th><th>近30日</th><th>操作</th><th>更新</th></tr>'
            if flow_rows:
                sections.append('<section class="card" id="portfolio-table"><h2>异常期权相关</h2><div class="table-wrap"><table>' + header + "".join(flow_rows) + '</table></div></section>')
            sections.append('<section class="card" id="portfolio-table"><h2>全部自选</h2><div class="table-wrap"><table>' + header + ("".join(other_rows) or '<tr><td colspan="13">暂无组合快照</td></tr>') + '</table></div></section>')
            return "".join(sections)
        if path == "/backtest" and isinstance(data, dict):
            paper = data.get("paper") or {}
            parts = []

            # -- 1. 分析师信号回测（置顶，核心） --
            accuracy = data.get("analyst_accuracy") or {}
            _raw_horizon = accuracy.get("horizon_days")
            try:
                horizon = 0 if _raw_horizon in (None, "") else int(_raw_horizon)
            except (TypeError, ValueError):
                horizon = 0
            horizon_label = "持有到期" if horizon == 0 else f"{horizon} 日"
            horizon_value = "" if horizon == 0 else str(horizon)
            acc_summary = accuracy.get("summary") if isinstance(accuracy.get("summary"), list) else []
            if acc_summary:
                acc_rows = []
                for item in acc_summary:
                    trades = int(item.get("trades", 0) or 0)
                    filled = int(item.get("filled", 0) or 0)
                    direction_rated = int(item.get("direction_rated", 0) or 0)
                    direction_hits = int(item.get("direction_hits", 0) or 0)
                    strategy_wins = int(item.get("strategy_wins", 0) or 0)
                    direction_rate = f"{direction_hits / direction_rated * 100:.1f}%" if direction_rated else "—"
                    strategy_rate = f"{strategy_wins / filled * 100:.1f}%" if filled else "—"
                    avg_pnl = float(item.get("avg_pnl", 0) or 0)
                    avg_stock = float(item.get("avg_stock_pnl", 0) or 0)
                    avg_premium = float(item.get("avg_premium_pct", 0) or 0)
                    analyst = str(item.get("analyst", ""))
                    pnl_cls = "up" if avg_pnl >= 0 else "down"
                    stock_cls = "up" if avg_stock >= 0 else "down"
                    prem_cls = "up" if avg_premium >= 0 else "down"
                    acc_rows.append(
                        '<tr class="analyst-row" data-analyst="{0}"><td><span class="muted">▸</span> {0}</td>'
                        '<td>{1}</td><td>{2}</td><td class="{3}">{4:+.2f}%</td><td>{5}</td>'
                        '<td class="{6}">{7:+.2f}%</td><td class="{8}">{9:+.0f}%</td></tr>'
                        '<tr class="analyst-detail" id="detail-{0}" style="display:none"><td colspan="7">'
                        '<div class="muted">展开中…</div></td></tr>'.format(
                            analyst, trades, direction_rate, stock_cls, avg_stock * 100,
                            strategy_rate, pnl_cls, avg_pnl * 100, prem_cls, avg_premium * 100,
                        )
                    )
                parts.append(
                    '<section class="card"><h2>分析师信号回测（TRADE 信号，双口径）</h2>'
                    f'<p class="muted">当前持有期：{html.escape(horizon_label)}。点击分析师行展开逐笔明细。</p>'
                    '<form method="get" class="card toolbar" style="margin-bottom:10px">'
                    '<label style="margin:0;font-weight:600;white-space:nowrap">持有期（交易日）</label>'
                    '<input type="number" name="horizon_days" value="' + horizon_value + '" min="0" max="365" '
                    'placeholder="到期" style="width:110px;margin:0">'
                    '<span class="muted">输入 0 = 持有到期前 3 天平仓</span>'
                    '<button type="submit">更新</button>'
                    '<button type="button" class="secondary" id="analyst-expand">全部展开</button>'
                    '<button type="button" class="secondary" id="analyst-collapse">全部折叠</button>'
                    '</form>'
                    '<div class="table-wrap"><table>'
                    '<tr><th>分析师</th><th>信号数</th><th>方向正确率</th><th>正股盈亏</th><th>卖方胜率</th><th>卖方盈亏</th><th>权利金赚取率</th></tr>'
                    + "".join(acc_rows) + '</table></div>'
                    '<p class="muted">方向正确率基于正股涨跌；正股策略 = BULL 次日买正股 / BEAR 空仓；'
                    '卖方策略 = BULL 卖平值 PUT / BEAR 卖平值 CALL（次日开盘成交，3% 滑点，无止盈，权利金涨 50% 止损，收益率按 25% 保证金口径）。'
                    '权利金赚取率 = 每笔赚/亏权利金的比例（如 +65% = 赚了 65% 权利金，-50% = 止损亏 50% 权利金）。</p></section>'
                )
            elif accuracy.get("running"):
                parts.append('<section class="card"><h2>分析师信号回测</h2><p class="muted">正在回测 TRADE 信号（拉取期权历史行情），稍后刷新查看。</p></section>')

            # -- 2. 信号日历（折叠） --
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
                    '<details class="card"><summary>信号日历（' + horizon_label + '结算，双口径）</summary><div class="table-wrap"><table>'
                    '<tr><th>交易日</th><th>信号数</th><th>方向正确率</th><th>正股盈亏</th><th>卖方策略胜率</th><th>卖方平均盈亏</th></tr>'
                    + "".join(cal_rows) + '</table></div>'
                    '<p class="muted">按信号产生日聚合；正股 = BULL 买正股 / BEAR 空仓，卖方 = 卖平值期权（25% 保证金口径）。</p></details>'
                )

            # -- 2.5 买方短线回测（买原始合约，1/2/3 日 / 到期，无止盈止损） --
            buyside = data.get("buyside") or {}
            buyside_summary = buyside.get("summary") if isinstance(buyside.get("summary"), list) else []
            if buyside_summary:
                HORIZONS = [(1, "1 日"), (2, "2 日"), (3, "3 日"), (0, "持有到期")]
                by_analyst: Dict[str, Dict[int, tuple]] = {}
                for item in buyside_summary:
                    analyst = str(item.get("analyst", ""))
                    horizon = int(item.get("horizon_days", 0) or 0)
                    filled = int(item.get("filled", 0) or 0)
                    wins = int(item.get("strategy_wins", 0) or 0)
                    avg_pnl = float(item.get("avg_pnl", 0) or 0)
                    by_analyst.setdefault(analyst, {})[horizon] = (filled, wins, avg_pnl)
                bs_rows = []
                for analyst in sorted(by_analyst, key=lambda a: (a != "fpd", a)):
                    cells = by_analyst.get(analyst, {})
                    row = ['<tr><td>{}</td>'.format(html.escape(analyst))]
                    for h, _label in HORIZONS:
                        cell = cells.get(h)
                        if not cell:
                            row.append('<td class="muted">—</td>')
                            continue
                        filled, wins, avg_pnl = cell
                        win_rate = f"{wins / filled * 100:.0f}%" if filled else "—"
                        cls = "up" if avg_pnl >= 0 else "down"
                        row.append('<td class="{0}">{1:+.1f}%<span class="muted">（{2}）</span></td>'.format(
                            cls, avg_pnl * 100, win_rate))
                    row.append('</tr>')
                    bs_rows.append("".join(row))
                parts.append(
                    '<details class="card" open><summary>买方短线回测（买原始合约，1/2/3 日 / 到期，无止盈止损）</summary>'
                    '<div class="table-wrap"><table>'
                    '<tr><th>分析师</th><th>1 日</th><th>2 日</th><th>3 日</th><th>持有到期</th></tr>'
                    + "".join(bs_rows) + '</table></div>'
                    '<p class="muted">买方 = BULL 买 CALL / BEAR 买 PUT（信号关联的原始合约，次日开盘买入，收益率按权利金口径，括号内为胜率）。'
                    '90 天数据显示买原始合约整体期望为负（各家族到期口径均亏损），故买方短线流已关闭，此表仅供对照参考。</p></details>'
                )

            # -- 2.6 正股计划回测（分析师入场/目标/止损，持有到期） --
            plan = data.get("plan") or {}
            plan_summary = plan.get("summary") if isinstance(plan.get("summary"), list) else []
            if plan_summary:
                plan_rows = []
                for item in plan_summary:
                    analyst = str(item.get("analyst", ""))
                    trades = int(item.get("trades", 0) or 0)
                    target_hits = int(item.get("target_hits", 0) or 0)
                    stop_hits = int(item.get("stop_hits", 0) or 0)
                    expiries = int(item.get("expiries", 0) or 0)
                    avg_pnl = float(item.get("avg_pnl", 0) or 0)
                    hit_rate = f"{target_hits / trades * 100:.1f}%" if trades else "—"
                    pnl_cls = "up" if avg_pnl >= 0 else "down"
                    plan_rows.append(
                        '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td class="{}">{:+.2f}%</td></tr>'.format(
                            html.escape(analyst), trades, target_hits, stop_hits, expiries, hit_rate, pnl_cls, avg_pnl * 100,
                        )
                    )
                parts.append(
                    '<details class="card" open><summary>正股计划回测（分析师入场/目标/止损，持有到期）</summary>'
                    '<div class="table-wrap"><table>'
                    '<tr><th>分析师</th><th>信号数</th><th>触目标</th><th>触止损</th><th>持有到期</th><th>触目标率</th><th>平均收益</th></tr>'
                    + "".join(plan_rows) + '</table></div>'
                    '<p class="muted">正股口径：信号日收盘价入场（超分析师入场价 5% 跳过），触及目标止盈、触及止损止损（同日先判止损）、否则持有到期。'
                    '触目标率 = 触目标 ÷ 信号数（对比分析师自报胜率）。收益 = 正股涨跌（不含期权杠杆）。</p></details>'
                )

            # -- 3. 模拟交易统计 --
            if paper:
                parts.append(f'<section class="card"><h2>模拟交易统计</h2><p>平仓: {paper.get("closed",0)} 笔　胜率: {round(paper.get("win_rate",0)*100,1)}%　损益: ${paper.get("realized_pnl",0):,.2f}</p></section>')

            # -- 4. 历史验证 Test D（文档 13.3）：HV 分层 + 评分分层 --
            validation = data.get("validation") if isinstance(data.get("validation"), Mapping) else {}
            hv_rows = validation.get("hv") if isinstance(validation.get("hv"), list) else []
            score_rows = validation.get("score") if isinstance(validation.get("score"), list) else []
            if hv_rows or score_rows:
                def val_rows(items, premium):
                    out = []
                    for item in items:
                        n = int(item.get("n", 0) or 0)
                        win_rate = f"{float(item.get('win_rate', 0)) * 100:.0f}%" if n else "—"
                        pnl = float(item.get("avg_pnl_pct", 0) or 0)
                        cls = "up" if pnl >= 0 else "down"
                        cells = '<td>{0}</td><td>{1}</td><td class="{2}">{3:+.2f}%</td>'.format(
                            html.escape(str(item.get("bucket", ""))), n, cls, pnl * 100,
                        )
                        if premium:
                            prem = item.get("avg_premium_pct")
                            cells += '<td class="muted">{}</td>'.format(
                                f"{float(prem) * 100:+.0f}%" if prem is not None else "—"
                            )
                        out.append("<tr>" + cells + "</tr>")
                    return "".join(out)

                hv_html = ""
                if hv_rows:
                    hv_html = (
                        '<div class="table-wrap"><table><tr><th>信号日 HV 分层</th><th>样本数</th>'
                        '<th>卖方平均盈亏</th><th>平均赚取权利金</th></tr>'
                        + val_rows(hv_rows, premium=True) + '</table></div>'
                    )
                score_html = ""
                if score_rows:
                    score_html = (
                        '<div class="table-wrap"><table><tr><th>推荐评分分层</th><th>样本数</th>'
                        '<th>卖方平均盈亏</th></tr>'
                        + val_rows(score_rows, premium=False) + '</table></div>'
                    )
                parts.append(
                    '<details class="card"><summary>历史验证 Test D（文档 13.3：IV/HV 分层 + 评分区分度）</summary>'
                    + hv_html + score_html
                    + '<p class="muted">口径：卖方策略（BULL 卖平值 PUT / BEAR 卖平值 CALL，25% 保证金，持有到期）。'
                    'HV 用信号日已存储的正股日线序列计算（年化 20 日 close-to-close），无额外行情请求；'
                    '评分分层按推荐落库时的分数（权利金质量优先新评分），检验分数与卖方盈亏的区分度。</p></details>'
                )
            return "".join(parts) if parts else '<section class="card"><p>暂无回测数据。等待系统积累足够交易日后再查看。</p></section>'
        if path == "/system" and isinstance(data, dict):
            from options_radar.timeutil import us_cash_session_label
            parts = []
            overall = str(data.get("status", "unknown"))
            session_label = us_cash_session_label()
            # NasRuntime.health() 把 service.health() 放在 core 字段下；数据源卡片
            # 与采集/同步时间戳都在 core 里，顶层只有 status/scheduler/opend。
            core = data.get("core") if isinstance(data.get("core"), Mapping) else {}
            portfolio = core.get("portfolio") or {}
            def tile(label, value):
                return f'<div class="tile"><b>{html.escape(label)}</b><span>{html.escape(value)}</span></div>'
            parts.append(
                '<section class="card"><h2>总览</h2><div class="tiles">'
                + tile("系统状态", overall)
                + tile("美股时段", session_label)
                + tile("最近采集", str((core.get("last_collection") or data.get("last_collection") or "从未采集")[:19]))
                + tile("最近同步", str((core.get("last_sync") or data.get("last_sync") or "从未同步")[:19]))
                + tile("持仓来源", str(portfolio.get("source", "无")))
                + tile("持仓数", str(portfolio.get("positions", 0)))
                + tile("自选数", str(core.get("watchlist_count", 0)))
                + tile("净值可用", "是" if portfolio.get("nav_present") else "否")
                + '</div></section>'
            )
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
                return f'<section class="card"><h2>{label}</h2><div class="tile"><span>{html.escape(state)}</span></div></section>'
            grid = [provider_card("futu", "富途 OpenD", core.get("futu")),
                    provider_card("ibkr", "IBKR", core.get("ibkr")),
                    provider_card("discord", "Discord", core.get("discord")),
                    provider_card("ai", "DeepSeek", core.get("ai")),
                    provider_card("feishu", "飞书", core.get("feishu"))]
            parts.append('<section class="card"><h2>数据源</h2><div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">' + "".join(grid) + '</div></section>')
            gex_snap = data.get("gex_snapshots") or {}
            bottom = []
            if data.get("last_error"):
                bottom.append(f'<section class="card error"><h2>最近错误</h2><p>{html.escape(str(data["last_error"]))}</p></section>')
            bottom.append(
                '<section class="card"><h2>GEX 前瞻验证</h2><div class="tiles">'
                + tile("已记录交易日", f'{gex_snap.get("days", 0)} 天')
                + tile("覆盖标的", f'{gex_snap.get("symbols", 0)} 个')
                + tile("最近快照", str(gex_snap.get("latest_date") or "暂无"))
                + tile("说明", "每日收盘后自动记录 flow 触达标的的 strike 级 GEX")
                + '</div></section>'
            )
            if len(bottom) == 2:
                parts.append('<div class="grid">' + "".join(bottom) + '</div>')
            else:
                parts.append(bottom[0])
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
    def _action_forms(csrf: str, actions: Tuple[Tuple[str, str], ...], reload_paths: Optional[set] = None) -> str:
        labels = {"/api/actions/collect":"立即采集并生成推荐","/api/actions/reevaluate":"刷新评分","/api/actions/report":"生成日报","/api/actions/backup":"创建备份","/api/actions/gex-snapshot":"记录GEX快照","/api/actions/alerts-check":"检查GEX预警","/api/actions/flow-classify":"分类Flow类型","/api/actions/feishu-test":"测试飞书","/api/actions/discord-login":"打开Discord登录","/api/actions/deepseek-test":"测试DeepSeek","/api/ibkr/sync":"同步IBKR持仓","/api/providers/massive/test":"测试Massive","/futu/import-watchlist":"从富途导入自选","/api/portfolio/refresh":"手动刷新持仓"}
        reload_paths = reload_paths or set()
        forms = "".join(
            '<form data-ajax="1"' + (' data-reload="1"' if path in reload_paths else '')
            + f' method="post" action="{path}">'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf)}"><button>{html.escape(labels.get(path, label))}</button></form>'
            for path, label in actions
        )
        return '<div class="card"><b>快捷操作</b><div>' + forms + "</div></div>"

    @staticmethod
    def _alerts_setup_html(data: Mapping[str, Any], csrf: str) -> str:
        rules: Dict[str, Dict[str, Any]] = {}
        for item in (data.get("alerts") or {}).get("rules") or []:
            if isinstance(item, Mapping):
                rules[str(item.get("type", ""))] = dict(item)
        checked = lambda value: "checked" if value else ""
        num_field = lambda field, value: f'<input type="number" step="any" name="{field}" value="{html.escape(str(value))}" style="width:120px">'
        text_field = lambda field, value, placeholder="": f'<input type="text" name="{field}" value="{html.escape(str(value))}" placeholder="{html.escape(placeholder)}" style="width:240px">'
        parts = []

        def block(rtype: str, label: str, body: str, default_enabled: bool = True) -> None:
            r = rules.get(rtype, {})
            parts.append(
                '<div style="margin:10px 0;border-top:1px solid var(--line);padding-top:8px">'
                f'<label style="display:flex;align-items:center;gap:8px;font-weight:600">'
                f'<input type="checkbox" name="alerts_{rtype}_enabled" {checked(bool(r.get("enabled", default_enabled)))}> {label}</label>'
                f'<div class="muted" style="margin:4px 0 0 26px">{body}</div></div>'
            )

        block("min_score", "评分 ≥ 阈值", num_field("alerts_min_score_threshold", rules.get("min_score", {}).get("threshold", 65)))
        block("min_premium", "权利金 ≥ 阈值（美元）", num_field("alerts_min_premium_threshold", rules.get("min_premium", {}).get("threshold", 500000)))
        direction = str(rules.get("direction", {}).get("direction", ""))
        direction_options = "".join(
            f'<option value="{d}"{" selected" if direction == d else ""}>{lbl}</option>'
            for d, lbl in (("", "全部方向"), ("BULL", "仅看多 BULL"), ("BEAR", "仅看空 BEAR"), ("BOTH", "双向"))
        )
        block("direction", "仅特定方向", f'<select name="alerts_direction_value">{direction_options}</select>')
        block("watchlist_only", "仅自选/持仓标的（可加自定义代码，逗号分隔）",
              text_field("alerts_watchlist_symbols", rules.get("watchlist_only", {}).get("symbols", ""), "AAPL,TSLA"),
              default_enabled=False)
        block("dte_range", "DTE 区间",
              num_field("alerts_dte_min", rules.get("dte_range", {}).get("min_dte", 7))
              + " – " + num_field("alerts_dte_max", rules.get("dte_range", {}).get("max_dte", 60)))
        block("wall_proximity", "现价逼近 Wall/Flip ±%（默认 1）", num_field("alerts_wall_proximity_pct", rules.get("wall_proximity", {}).get("threshold_pct", 1.0)))
        block("regime_flip", "Gamma Regime 翻转", '<span class="muted">（开关）</span>')
        block("iv_rank", "IV Rank ≥ 阈值（期权偏贵）", num_field("alerts_iv_rank_threshold", rules.get("iv_rank", {}).get("threshold", 80)))
        block("discord_down", "Discord 登录失效提醒（连续失败次数）", num_field("alerts_discord_max_failures", rules.get("discord_down", {}).get("max_failures", 3)))
        block("opend_down", "OpenD 掉线提醒（连续失败次数）", num_field("alerts_opend_max_failures", rules.get("opend_down", {}).get("max_failures", 3)))
        return (
            '<section class="card"><h2>提醒设置</h2>'
            '<p>按规则触发后通过飞书推送，每条规则可独立开关；同一条提醒每个交易日只发一次。评分 ≥65 为 B 级线。</p>'
            '<form method="post" action="/save-alerts"><input type="hidden" name="csrf" value="' + html.escape(csrf) + '">'
            + "".join(parts)
            + '<button>保存提醒设置</button></form></section>'
        )

    def _setup_page(self, csrf: str, message: str = "") -> str:
        display_labels = {"timezone":"\u65f6\u533a","discord_server":"Discord\u670d\u52a1\u5668","flow_channel":"\u5f02\u5e38\u671f\u6743\u9891\u9053","pa_channel":"PA\u5206\u6790\u5e08\u9891\u9053","mr_channel":"MR\u5206\u6790\u5e08\u9891\u9053","qmr_channel":"QMR\u5206\u6790\u5e08\u9891\u9053","fpd_channel":"FPD\u5206\u6790\u5e08\u9891\u9053","feishu_app_id":"\u98de\u4e66 App ID","flash_model":"DeepSeek\u65e5\u5e38\u6a21\u578b","pro_model":"DeepSeek\u590d\u6838\u6a21\u578b","report_delay":"\u6536\u76d8\u540e\u65e5\u62a5\u5ef6\u8fdf\uff08\u5206\u949f\uff09","poll_minutes":"Discord\u91c7\u96c6\u95f4\u9694\uff08\u5206\u949f\uff09","session_start_hour":"\u91c7\u96c6\u5f00\u59cb\u5c0f\u65f6\uff08\u7f8e\u4e1c0-23\uff09","session_end_hour":"\u91c7\u96c6\u7ed3\u675f\u5c0f\u65f6\uff08\u7f8e\u4e1c0-23\uff09"}
        data = self.app.store.load()
        inputs = []
        for field in FIELDS:
            value = _get_path(data, field.config_path, "")
            kind = "number" if field.form_name in {"report_delay", "poll_minutes", "session_start_hour", "session_end_hour"} else "text"
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
        # 富途登录密码：由 _save_secrets 特殊处理（仅持久化 OpenD 协议的 MD5 形式）。
        futu_pwd_placeholder = "已保存，留空保持原值" if statuses.get("富途登录凭据") else "粘贴到这里"
        secret_inputs.append(
            f'<label for="futu_login_password">富途登录密码</label><input id="futu_login_password" name="futu_login_password" '
            f'type="password" autocomplete="new-password" placeholder="{futu_pwd_placeholder}">'
        )
        status_html = "".join(
            f'<span>{html.escape(label)}</span><strong>{"已保存" if present else "待配置"}</strong>'
            for label, present in statuses.items()
        )
        notice = f'<p class="{"error" if ("出错" in message or "失败" in message) else "ok"}">{html.escape(message)}</p>' if message else ""
        qr_path = Path(os.getenv("DATA_DIR", "/data")) / "evidence" / "discord-login.png"
        local = os.getenv("OPTIONS_RADAR_LOCAL") == "1"
        if local:
            qr_html = (
                '<p class="muted">点击“打开Discord登录”后，会在本机弹出 Discord 浏览器窗口，'
                '请直接在那个窗口里用手机扫码或账号登录；登录状态自动保存，无需在本页操作。</p>'
            )
        else:
            qr_html = ""
            if qr_path.is_file():
                qr_html = (
                    '<h2>Discord扫码登录</h2>'
                    '<p class="muted">二维码约 2 分钟过期，请及时扫码；过期后点“刷新二维码”。</p>'
                    '<img id="discord-qr" src="/discord-login.png" alt="Discord登录二维码" style="max-width:320px;width:100%">'
                    '<div><button type="button" class="secondary" onclick="refreshDiscordQr()">刷新二维码</button></div>'
                )
            qr_html += (
                '<script>'
                'async function refreshDiscordQr(){'
                '  const btn=event.target; btn.disabled=true; btn.textContent="刷新中…";'
                '  try{'
                '    const r=await fetch("/api/actions/discord-refresh-qr",{method:"POST",body:new URLSearchParams({csrf:"' + html.escape(csrf) + '"})});'
                '    const d=await r.json();'
                '    const img=document.getElementById("discord-qr");'
                '    if(img){img.src="/discord-login.png?t="+Date.now();}'
                '    btn.textContent=(r.ok?"已刷新":"失败")+"，点击重试";'
                '  }catch(e){btn.textContent="刷新失败";}'
                '  btn.disabled=false;'
                '}'
                '</script>'
            )
        captcha_path = Path(os.getenv("DATA_DIR", "/data")) / "opend-profile" / ".com.futunn.FutuOpenD" / "F3CNN" / "PicVerifyCode.png"
        captcha_html = '<h3>富途图形验证码</h3><img src="/futu-captcha.png" alt="富途图形验证码" style="max-width:360px;width:100%">' if captcha_path.is_file() else ""
        location = "本机的 <code>data-local/secrets</code>" if local else "NAS的 <code>/data/secrets</code>"
        content = (
            f'<h1>一次性配置</h1><p class="sub">密钥只保存在{location}；交易解锁保持关闭。</p>'
            '<div id="action-result"></div>'
            f'{notice}<div class="card"><div class="status">{status_html}</div></div>'
            '<form method="post" action="/save"><input type="hidden" name="csrf" value="' + html.escape(csrf) + '">'
            '<div class="grid"><section class="card"><h2>基础配置</h2><div class="form-grid">'
            + "".join(f'<div>{inp}</div>' for inp in inputs) + '</div></section>'
            '<section class="card"><h2>API配置</h2><div class="form-grid">'
            + "".join(f'<div>{inp}</div>' for inp in secret_inputs) + '</div></section></div>'
            '<button>保存并启用</button></form>'
            '<section class="card"><h2>Discord登录</h2>'
            '<p>采集使用浏览器模拟（能读取 REST API 读不到的订阅帖）。点「打开Discord登录」生成二维码，用手机 App 扫一次即可；登录态持久保存，之后全自动采集。下方「Discord 用户 Token」字段仅作预留。</p>'
            '<div id="discord-status" class="muted">正在读取 Discord 状态…</div>'
            '<script>async function refreshDiscordStatus(){try{const r=await fetch("/api/discord-status");const d=await r.json();'
            'const el=document.getElementById("discord-status");const s=(d&&d.status)||"";'
            'const label={ready:"已登录（采集就绪）",login_required:"等待扫码登录",login_pending:"登录处理中",starting:"浏览器启动中",stopped:"未启动",error:"出错"}[s]||s||"未知";'
            'el.innerHTML="采集状态：<b>"+label+"</b>"+(d.last_success?("<br>最近成功采集："+d.last_success):"")+(d.last_error?("<br><span class=\'error\'>最近错误："+d.last_error+"</span>"):"");'
            '}catch(e){document.getElementById("discord-status").textContent="状态读取失败";}}'
            'refreshDiscordStatus();setInterval(refreshDiscordStatus,15000);</script>'
            + self._action_forms(csrf, (("/api/actions/discord-login", "打开Discord登录"),))
            + qr_html + '</section>'
            + ('<section class="card"><h2>富途 OpenD 登录</h2>'
               '<p>富途 OpenD 为主数据源。在上方填写富途账号与登录密码并保存后，容器会后台下载约 467MB 的 OpenD 并自动启动；状态显示「就绪」后即可拉取行情与持仓。</p>'
               '<div id="opend-status" class="muted">正在读取 OpenD 状态…</div>'
               '<script>async function refreshOpend(){try{const r=await fetch("/api/futu/status");const d=await r.json();'
               'const el=document.getElementById("opend-status");const s=(d&&d.state)||"unknown";const run=(d&&d.running)?"运行中":"未运行";'
               'el.innerHTML="状态：<b>"+s+"</b>（"+run+"）"+((d&&d.message)?("<br>"+d.message):"");}'
               'catch(e){document.getElementById("opend-status").textContent="状态读取失败";}}refreshOpend();</script>'
               + self._action_forms(csrf, (("/futu/send-code", "发送验证码"), ("/futu/relogin", "重新登录"), ("/futu/sync", "立即同步"), ("/futu/import-watchlist", "从富途导入自选")))
               + '<form data-ajax="1" method="post" action="/futu/submit-code"><input type="hidden" name="csrf" value="' + html.escape(csrf) + '">'
               '<label>手机验证码</label><input name="verification_code" inputmode="numeric" autocomplete="one-time-code">'
               '<label>图形验证码（出现时填写）</label><input name="captcha_code" autocomplete="off"><button>提交验证码</button></form>'
               + captcha_html + '</section>'
               if not local else
               '<section class="card"><h2>富途 OpenD</h2>'
               '<p>桌面版请在本机启动并登录富途 OpenD（OpenD 应用），本面板连接 127.0.0.1:11111；在上方填写富途账号后可一键导入自选。</p>'
               + self._action_forms(csrf, (("/futu/import-watchlist", "从富途导入自选"),))
               + '</section>')
            + '<section class="card"><h2>配置与数据迁移</h2>'
              '<p>导出当前配置与密钥，或导入从桌面版/另一台机器导出的配置；也可导出、导入历史数据库（迁移时使用）。</p>'
              '<div class="toolbar">'
              '<button type="button" onclick="exportConfig()">导出配置</button>'
              '<label>导入配置 <input type="file" id="config-file" accept=".json,application/json"></label>'
              '<button type="button" onclick="importConfig()">导入配置</button>'
              '<button type="button" onclick="downloadData()">导出数据</button>'
              '<label>导入数据 <input type="file" id="data-file" accept=".db"></label>'
              '<button type="button" onclick="importData()">导入数据</button>'
              '</div>'
              '<script>'
              'const _csrf=' + json.dumps(str(csrf)) + ';'
              'async function exportConfig(){'
              '  const r=await fetch("/api/config/export");const d=await r.json();'
              '  const blob=new Blob([JSON.stringify(d,null,2)],{type:"application/json"});'
              '  const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="options-radar-config.json";a.click();'
              '}'
              'async function importConfig(){'
              '  const f=document.getElementById("config-file").files[0];if(!f){alert("请选择配置文件");return;}'
              '  let d;try{d=JSON.parse(await f.text())}catch(e){alert("JSON 解析失败");return;}'
              '  const r=await fetch("/api/config/import",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":_csrf},body:JSON.stringify(d)});'
              '  const out=await r.json();alert(out.message||out.status);'
              '  if(out.status==="ok")setTimeout(function(){location.reload();},800);'
              '}'
              'function downloadData(){location.href="/api/data/export";}'
              'async function importData(){'
              '  const f=document.getElementById("data-file").files[0];if(!f){alert("请选择数据库文件");return;}'
              '  const r=await fetch("/api/data/import",{method:"POST",headers:{"X-CSRF-Token":_csrf},body:f});'
              '  const out=await r.json();alert(out.message||out.status);'
              '  if(out.status==="ok")setTimeout(function(){location.reload();},800);'
              '}'
              '</script></section>'
            + '<section class="card"><h2>管理员账户</h2>'
              '<form data-ajax="1" method="post" action="/admin-password"><input type="hidden" name="csrf" value="' + html.escape(csrf) + '">'
              '<label>原密码</label><input name="old_password" type="password" autocomplete="current-password">'
              '<label>新密码（至少8位）</label><input name="new_password" type="password" autocomplete="new-password">'
              '<label>确认新密码</label><input name="confirm_password" type="password" autocomplete="new-password">'
              '<button>修改管理员密码</button></form></section>'
            + self._alerts_setup_html(data, csrf)
            + f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><button class="secondary">退出登录</button></form>'
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
