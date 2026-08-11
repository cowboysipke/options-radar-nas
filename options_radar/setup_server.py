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
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import yaml


SESSION_COOKIE = "radar_setup_session"
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
            "fpd": "fqd分析师",
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
    # Kept for backwards-compatible loading while Futu is the primary provider.
    "ibkr": {"flex_query_id": ""},
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
    "schedule": {"report_delay_minutes": 75},
    "secret_refs": {
        "deepseek_api_key": "/data/secrets/deepseek_api_key",
        "feishu_app_secret": "/data/secrets/feishu_app_secret",
        "futu_login_password_md5": "/data/secrets/futu_login_password_md5",
        "ibkr_flex_token": "/data/secrets/ibkr_flex_token",
        "massive_api_key": "/data/secrets/massive_api_key",
    },
    "setup_completed": False,
}

SECRET_ENV_FILES = {
    "DeepSeek API Key": "DEEPSEEK_API_KEY_FILE",
    "飞书 App Secret": "FEISHU_APP_SECRET_FILE",
    "富途登录凭据": "FUTU_LOGIN_PASSWORD_MD5_FILE",
}

SECRET_FORM_FIELDS = {
    "deepseek_api_key": ("DeepSeek API Key", "deepseek_api_key"),
    "feishu_app_secret": ("飞书 App Secret", "feishu_app_secret"),
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
    Field("fpd_channel", "discord.channel_names.fpd", "FQD频道", _plain),
    Field("guide_channel", "discord.channel_names.guide", "使用指南频道", _plain),
    Field("subscriptions_channel", "discord.channel_names.subscriptions", "分析师订阅面板频道", _plain),
    Field("feishu_app_id", "notifications.feishu_app_id", "飞书 App ID", _plain),
    Field("futu_user_id", "futu.user_id", "富途ID、邮箱或手机号", _plain),
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
                        if analyst in {"flow", "pa", "mr", "qmr", "fpd", "guide", "subscriptions"}
                    }
            return _deep_merge(DEFAULT_CONFIG, data)

    def initialize(self) -> Dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                self.save(self.load())
            return self.load()

    def fixed_secret_refs(self) -> Dict[str, str]:
        secret_dir = self.path.parent / "secrets"
        return {name: str(secret_dir / name) for name in DEFAULT_CONFIG["secret_refs"]}

    def secret_presence(self) -> Dict[str, bool]:
        refs = self.load().get("secret_refs", self.fixed_secret_refs())
        ref_by_label = {
            "DeepSeek API Key": "deepseek_api_key",
            "飞书 App Secret": "feishu_app_secret",
            "富途登录凭据": "futu_login_password_md5",
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
                for source in ("flow", "pa", "mr", "qmr", "fpd", "guide", "subscriptions")
            }
            data["market"]["provider"] = "futu"
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
    ):
        if len(token) < 16:
            raise ValueError("SETUP_TOKEN must contain at least 16 characters")
        self.store = store
        self._token_digest = hashlib.sha256(token.encode("utf-8")).digest()
        self.health = health or (lambda: {"status": "ok"})
        self.callbacks = dict(callbacks or {})
        self.session_ttl = session_ttl
        self._sessions: Dict[str, Tuple[float, str]] = {}
        self._lock = threading.Lock()

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
    }
    return {
        label: bool(_read_secret(env_name) or (
            Path(str(refs[by_label[label]])).read_text(encoding="utf-8").strip()
            if Path(str(refs[by_label[label]])).is_file() else ""
        ))
        for label, env_name in SECRET_ENV_FILES.items()
    }


PAGE_INFO = {
    "/": ("今日概览", "recommendations", "今日推荐"),
    "/contracts": ("合约与推荐", "contracts", "候选合约"),
    "/rules": ("规则库", "rules", "Discord指南与规则版本"),
    "/portfolio": ("富途组合", "portfolio", "持仓、自选与风险集中度"),
    "/analysts": ("分析师", "analysts", "分析师表现与权重"),
    "/backtest": ("回测", "backtest", "模拟净值与策略版本"),
    "/system": ("系统", "status", "服务状态、日志与备份"),
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
}

POST_APIS = {
    "/api/futu/send-verification": "futu_send_verification",
    "/api/futu/submit-verification": "futu_submit_verification",
    "/api/futu/relogin": "futu_relogin",
    "/api/futu/sync": "futu_sync",
    "/api/actions/collect": "collect",
    "/api/actions/report": "report",
    "/api/actions/backup": "backup",
}

# Browser-facing action URLs deliberately avoid API-looking navigation. Some
# privacy extensions block form navigation to paths containing words such as
# ``send-verification`` and show ERR_BLOCKED_BY_CLIENT before rendering the
# valid server response.
FORM_ACTIONS = {
    "/futu/send-code": "futu_send_verification",
    "/futu/submit-code": "futu_submit_verification",
    "/futu/relogin": "futu_relogin",
    "/futu/sync": "futu_sync",
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
            "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
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
        self._send(status, json.dumps(value, ensure_ascii=False, separators=(",", ":")), "application/json; charset=utf-8")

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
        return self.app.session(self.headers.get("Cookie", ""))

    def _callback(self, name: str, payload: Mapping[str, Any]) -> Any:
        try:
            return self.app.invoke(name, payload)
        except Exception as exc:  # boundary: runtime errors become stable HTTP responses
            return {"status": "error", "message": str(exc)}

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
        if path in GET_APIS:
            if not session:
                self._json(HTTPStatus.UNAUTHORIZED, {"status": "unauthorized"})
                return
            name = GET_APIS[path]
            if name == "status" and name not in self.app.callbacks:
                result = dict(self.app.health())
            else:
                result = self._callback(name, query)
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
                self._send(HTTPStatus.OK, self._dashboard_page(path, csrf))
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
            self._send(HTTPStatus.OK, self._setup_page(csrf, "配置已保存，后台服务将自动重新加载。"))
            return
        if path == "/logout":
            self.app.logout(session_id)
            self._redirect("/", {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
            return
        if path in FORM_ACTIONS:
            callback_payload = {key: value for key, value in values.items() if key != "csrf"}
            result = self._callback(FORM_ACTIONS[path], callback_payload)
            if isinstance(result, Mapping):
                message = str(result.get("message", "")).strip()
                if not message:
                    state = str(result.get("status", "ok"))
                    message = "操作已完成。" if state != "error" else "操作出错，请查看系统页。"
            else:
                message = "操作已完成。"
            status = HTTPStatus.INTERNAL_SERVER_ERROR if isinstance(result, Mapping) and result.get("status") == "error" else HTTPStatus.OK
            self._send(status, self._setup_page(csrf, message))
            return
        if path in POST_APIS:
            callback_payload = {key: value for key, value in values.items() if key != "csrf"}
            result = self._callback(POST_APIS[path], callback_payload)
            status = HTTPStatus.INTERNAL_SERVER_ERROR if isinstance(result, Mapping) and result.get("status") == "error" else HTTPStatus.OK
            self._json(status, result)
            return
        self._send(HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")

    @staticmethod
    def _shell(title: str, content: str, active: str = "") -> str:
        links = (("/", "首页"), ("/contracts", "合约"), ("/rules", "规则库"),
                 ("/portfolio", "富途组合"), ("/analysts", "分析师"),
                 ("/backtest", "回测"), ("/system", "系统"), ("/setup", "设置"))
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
</style></head><body><header><b>Options Radar</b><nav>{nav}</nav></header><main>{content}</main></body></html>'''

    def _login_page(self, message: str = "") -> str:
        notice = f'<p class="error">{html.escape(message)}</p>' if message else ""
        content = (
            "<h1>异常期权助手</h1><p class=\"sub\">输入容器日志中的 <code>SETUP CODE</code>，也可在 <code>/data/setup-token</code> 查看。</p>"
            f'{notice}<div class="card"><form method="post" action="/login"><label>管理口令</label>'
            '<input name="token" type="password" required autofocus><button>进入管理面板</button></form></div>'
        )
        return self._shell("Options Radar 登录", content)

    def _dashboard_page(self, path: str, csrf: str) -> str:
        title, callback, description = PAGE_INFO[path]
        data = self._callback(callback, {}) if callback != "status" or callback in self.app.callbacks else dict(self.app.health())
        encoded = html.escape(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        action_cards = ""
        if path == "/":
            action_cards = self._action_forms(csrf, (("/api/actions/collect", "立即采集"), ("/api/actions/report", "生成日报"), ("/api/futu/sync", "同步富途")))
        elif path == "/system":
            action_cards = self._action_forms(csrf, (("/api/futu/relogin", "重新登录OpenD"), ("/api/futu/sync", "同步富途"), ("/api/actions/backup", "创建备份")))
        content = f'<h1>{title}</h1><p class="sub">{description}</p>{action_cards}<section class="card"><h2>当前数据</h2><pre>{encoded}</pre></section>'
        return self._shell(f"{title} - Options Radar", content, path)

    @staticmethod
    def _action_forms(csrf: str, actions: Tuple[Tuple[str, str], ...]) -> str:
        return '<div class="card"><b>快捷操作</b><div>' + "".join(
            f'<form style="display:inline" method="post" action="{path}"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><button>{label}</button></form>'
            for path, label in actions
        ) + "</div></div>"

    def _setup_page(self, csrf: str, message: str = "") -> str:
        data = self.app.store.load()
        inputs = []
        for field in FIELDS:
            value = _get_path(data, field.config_path, "")
            kind = "number" if field.form_name == "report_delay" else "text"
            inputs.append(
                f'<label for="{field.form_name}">{html.escape(field.label)}</label>'
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
        secret_inputs.append(
            '<label for="futu_login_password">富途登录密码</label><input id="futu_login_password" '
            'name="futu_login_password" type="password" autocomplete="new-password" placeholder="只保存MD5登录凭据">'
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
        content = (
            '<h1>一次性配置</h1><p class="sub">密钥只保存在NAS的 <code>/data/secrets</code>；交易解锁保持关闭。</p>'
            f'{notice}<div class="card"><div class="status">{status_html}</div></div>'
            '<form method="post" action="/save"><input type="hidden" name="csrf" value="' + html.escape(csrf) + '">'
            '<div class="grid"><section class="card"><h2>基本信息</h2>' + "".join(inputs) + '</section>'
            '<section class="card"><h2>密钥与登录</h2>' + "".join(secret_inputs) + '</section></div><button>保存并启用</button></form>'
            '<section class="card"><h2>富途维护</h2><p>首次启动会在后台安装约467MB的OpenD；已显示“就绪”时无需再发送验证码。</p>'
            + self._action_forms(csrf, (("/futu/send-code", "发送验证码"), ("/futu/relogin", "重新登录"), ("/futu/sync", "立即同步")))
            + f'<form method="post" action="/futu/submit-code"><input type="hidden" name="csrf" value="{html.escape(csrf)}">'
              '<label>手机验证码</label><input name="verification_code" inputmode="numeric" autocomplete="one-time-code">'
              '<label>图形验证码（出现时填写）</label><input name="captcha_code" autocomplete="off"><button>提交验证码</button></form>'
            + captcha_html + qr_html + '</section>'
            f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><button class="secondary">退出登录</button></form>'
        )
        return self._shell("NAS 配置", content, "/setup")


def create_setup_server(
    host: str,
    port: int,
    store: SetupConfigStore,
    token: str,
    health: Optional[Callable[[], Mapping[str, Any]]] = None,
    callbacks: Optional[Mapping[str, DashboardCallback]] = None,
) -> ThreadingHTTPServer:
    app = SetupApplication(store, token, health=health, callbacks=callbacks)
    server = ThreadingHTTPServer((host, port), SetupRequestHandler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    return server


def setup_token_from_environment() -> str:
    return _read_secret("SETUP_TOKEN_FILE", "SETUP_TOKEN")
