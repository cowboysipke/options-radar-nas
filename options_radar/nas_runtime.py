"""Single-process NAS supervisor for setup, scheduling, and the radar service."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import os
import shutil
import signal
import sqlite3
import secrets
import sys
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .opend_manager import OpenDManager, OpenDState, load_opend_release
from .setup_server import SetupConfigStore, create_setup_server, setup_token_from_environment


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def data_paths() -> Dict[str, Path]:
    root = Path(os.getenv("DATA_DIR", "/data")).resolve()
    return {
        "root": root,
        "config": Path(os.getenv("CONFIG_PATH", str(root / "config.yaml"))).resolve(),
        "database": root / "options_radar.db",
        "browser": root / "browser-profile",
        "evidence": root / "evidence",
        "strategies": root / "strategies",
        "backups": root / "backups",
    }


def ensure_data_layout(paths: Mapping[str, Path]) -> None:
    for key in ("root", "browser", "evidence", "strategies", "backups"):
        paths[key].mkdir(parents=True, exist_ok=True)


def create_backup(paths: Optional[Mapping[str, Path]] = None) -> Path:
    """Create a consistent SQLite/config backup and prune old archives."""
    paths = dict(paths or data_paths())
    ensure_data_layout(paths)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    staging = paths["backups"] / f".{stamp}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        database = paths["database"]
        if database.exists():
            source = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10)
            destination = sqlite3.connect(str(staging / "options_radar.db"))
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        if paths["config"].exists():
            shutil.copy2(paths["config"], staging / "config.yaml")
        archive = paths["backups"] / f"options-radar-{stamp}.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for item in staging.iterdir():
                bundle.add(item, arcname=item.name, recursive=False)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    archives = sorted(paths["backups"].glob("options-radar-*.tar.gz"), reverse=True)
    for stale in archives[11:]:
        stale.unlink(missing_ok=True)
    return archive


class NasRuntime:
    def __init__(
        self,
        paths: Optional[Mapping[str, Path]] = None,
        *,
        opend_manager_factory: Optional[Any] = None,
        component_factory: Optional[Any] = None,
    ):
        self.paths = dict(paths or data_paths())
        ensure_data_layout(self.paths)
        self.store = SetupConfigStore(self.paths["config"])
        self.store.initialize()
        self.started_at = _utc_now()
        self.last_heartbeat = _utc_now()
        self.last_backup: Optional[str] = None
        self.last_error: Optional[str] = None
        self.scheduler: Any = None
        self.setup_server: Any = None
        self.setup_thread: Optional[threading.Thread] = None
        self.component: Any = None
        self.component_config_mtime: Optional[float] = None
        self.component_factory_attempted = False
        self._component_factory = component_factory
        self.opend_manager_factory = opend_manager_factory
        self.opend_manager: Optional[OpenDManager] = None
        self._opend_credential_signature: Optional[str] = None
        self.stop_event = threading.Event()
        self._lock = threading.RLock()
        self._opend_lock = threading.Lock()

    def health(self) -> Mapping[str, Any]:
        config = self.store.load()
        with self._lock:
            status = "setup" if not config.get("setup_completed") else "ok"
            if self.last_error:
                status = "degraded"
            component_health: Any = "pending"
            if self.component is not None:
                try:
                    component_health = self.component.health() if hasattr(self.component, "health") else "running"
                except Exception as exc:
                    component_health = {"status": "degraded", "error": type(exc).__name__}
            opend_health: Any = {"state": OpenDState.NOT_INSTALLED.value, "running": False}
            if self.opend_manager is not None:
                try:
                    opend_health = self.opend_manager.status().to_dict()
                except Exception as exc:
                    opend_health = {"state": "error", "running": False, "error": type(exc).__name__}
            return {
                "status": status,
                "started_at": self.started_at,
                "last_heartbeat": self.last_heartbeat,
                "scheduler": bool(self.scheduler and self.scheduler.running),
                "core": component_health,
                "opend": opend_health,
            }

    def _heartbeat(self) -> None:
        with self._lock:
            self.last_heartbeat = _utc_now()
        self._ensure_opend()
        self._ensure_component()

    @staticmethod
    def _secret(config: Mapping[str, Any], name: str) -> str:
        refs = config.get("secret_refs", {})
        if not isinstance(refs, Mapping):
            return ""
        path_text = str(refs.get(name, "")).strip()
        if not path_text:
            return ""
        try:
            return Path(path_text).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _new_opend_manager(self) -> OpenDManager:
        if self.opend_manager_factory is not None:
            return self.opend_manager_factory(self.paths["root"])
        release = load_opend_release()
        return OpenDManager(self.paths["root"], release)

    def _ensure_opend(self) -> None:
        """Install/configure/start OpenD once setup has supplied its credentials."""

        with self._opend_lock:
            self._ensure_opend_locked()

    def _ensure_opend_locked(self) -> None:

        config = self.store.load()
        if not config.get("setup_completed"):
            return
        futu = config.get("futu", {})
        if not isinstance(futu, Mapping):
            return
        account = str(futu.get("user_id", "")).strip()
        login_pwd_md5 = self._secret(config, "futu_login_password_md5")
        if not account or not login_pwd_md5:
            with self._lock:
                self.last_error = "opend:credentials_missing"
            return
        try:
            if self.opend_manager is None:
                self.opend_manager = self._new_opend_manager()
            manager = self.opend_manager
            signature = hashlib.sha256(f"{account}\0{login_pwd_md5}".encode("utf-8")).hexdigest()
            manager.install()
            if signature != self._opend_credential_signature or not manager.config_path.is_file():
                manager.write_config(account, login_pwd_md5)
                self._opend_credential_signature = signature
            status = manager.status()
            if not status.running:
                manager.start(monitor=True)
            with self._lock:
                if self.last_error and self.last_error.startswith("opend:"):
                    self.last_error = None
        except Exception as exc:
            with self._lock:
                self.last_error = f"opend:{type(exc).__name__}"

    def _backup(self) -> None:
        try:
            archive = create_backup(self.paths)
            with self._lock:
                self.last_backup = str(archive)
                self.last_error = None
        except Exception as exc:
            with self._lock:
                self.last_error = f"backup:{type(exc).__name__}"

    def _ensure_component(self) -> None:
        if not self.store.load().get("setup_completed"):
            return
        current_mtime = self.paths["config"].stat().st_mtime if self.paths["config"].exists() else None
        if self.component is not None and current_mtime == self.component_config_mtime:
            return
        if self.component is not None:
            try:
                if hasattr(self.component, "stop"):
                    self.component.stop()
            finally:
                self.component = None
        # The integration layer may provide a single lifecycle factory without
        # changing this supervisor. Format: package.module:callable.
        factory_path = os.getenv("OPTIONS_RADAR_SERVICE_FACTORY", "options_radar.service:create_service")
        module_name, _, attribute = factory_path.partition(":")
        if not module_name or not attribute:
            with self._lock:
                self.last_error = "core:invalid_factory"
            return
        try:
            if self._component_factory is None:
                module = importlib.import_module(module_name)
                factory = getattr(module, attribute)
            else:
                factory = self._component_factory
            component = factory(config_path=str(self.paths["config"]), data_dir=str(self.paths["root"]))
            if hasattr(component, "start"):
                component.start()
            self.component = component
            self.component_config_mtime = current_mtime
            with self._lock:
                if self.last_error and self.last_error.startswith("core:"):
                    self.last_error = None
        except ModuleNotFoundError as exc:
            # First-run setup stays operational while optional integrations are
            # absent from a development build. Production images include them.
            self.component_factory_attempted = True
            with self._lock:
                self.last_error = f"core:{type(exc).__name__}"
        except Exception as exc:
            self.component_factory_attempted = True
            with self._lock:
                self.last_error = f"core:{type(exc).__name__}"

    def _opend_status_callback(self, _payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._ensure_opend()
        if self.opend_manager is None:
            return {"state": OpenDState.NOT_INSTALLED.value, "running": False}
        result = self.opend_manager.status().to_dict()
        if self.component is not None and hasattr(self.component, "health"):
            try:
                core = self.component.health()
                if isinstance(core, Mapping):
                    result["api"] = core.get("opend", {})
                    result["quote_rights"] = core.get("quote_rights", {})
            except Exception as exc:
                result["api"] = {"status": "degraded", "error": type(exc).__name__}
        return result

    def _opend_send_verification(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._ensure_opend()
        if self.opend_manager is None:
            return {"status": "pending", "message": "OpenD is waiting for setup credentials"}
        kind = str(payload.get("type", payload.get("verification_type", "phone"))).lower()
        if kind in ("captcha", "picture", "pic"):
            captcha = self.opend_manager.request_captcha()
            return {"status": "requested", "type": "captcha", "captcha_available": captcha.is_file()}
        reply = self.opend_manager.request_phone_code()
        return {"status": "requested", "type": "phone", "reply": reply}

    def _opend_submit_verification(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._ensure_opend()
        if self.opend_manager is None:
            return {"status": "pending", "message": "OpenD is waiting for setup credentials"}
        replies: Dict[str, str] = {}
        phone_code = str(payload.get("verification_code", payload.get("phone_code", ""))).strip()
        captcha_code = str(payload.get("captcha_code", "")).strip()
        if phone_code:
            replies["phone"] = self.opend_manager.submit_phone_code(phone_code)
        if captcha_code:
            replies["captcha"] = self.opend_manager.submit_captcha(captcha_code)
        if not replies:
            raise ValueError("verification_code or captcha_code is required")
        return {"status": "submitted", "replies": replies, "opend": self.opend_manager.status().to_dict()}

    def _opend_relogin(self, _payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._ensure_opend()
        if self.opend_manager is None:
            return {"status": "pending", "message": "OpenD is waiting for setup credentials"}
        reply = self.opend_manager.relogin()
        return {"status": "submitted", "reply": reply, "opend": self.opend_manager.status().to_dict()}

    def _component_action(self, callback_name: str, payload: Mapping[str, Any]) -> Any:
        self._ensure_component()
        component = self.component
        if component is None:
            return {"status": "pending", "component": callback_name}
        method_names = {
            "status": ("health",),
            "futu_sync": ("sync_futu", "sync_broker", "sync_portfolio"),
            "collect": ("collect_today", "collect"),
            "report": ("publish_daily", "report"),
            "recommendations": ("dashboard_recommendations", "recommendations", "today_recommendations"),
            "portfolio": ("dashboard_portfolio", "portfolio", "positions"),
            "contracts": ("dashboard_contracts", "contracts", "contract_evaluations"),
            "rules": ("dashboard_rules", "rules"),
            "analysts": ("dashboard_analysts", "analysts", "analyst_performance"),
            "backtest": ("dashboard_backtest", "backtest_summary", "run_backtests"),
            "system": ("system_status", "health"),
        }.get(callback_name, (callback_name,))
        method = next((getattr(component, name) for name in method_names if callable(getattr(component, name, None))), None)
        if method is None:
            return {"status": "not_supported", "component": callback_name}
        signature = inspect.signature(method)
        parameters = list(signature.parameters.values())
        if not payload:
            return method()
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
            return method(**dict(payload))
        accepted = {
            parameter.name for parameter in parameters
            if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        if set(payload).issubset(accepted):
            return method(**dict(payload))
        if len(parameters) == 1 and parameters[0].name in ("payload", "request", "context"):
            return method(dict(payload))
        # Query parameters irrelevant to a read-only page are not forwarded.
        return method()

    def dashboard_callbacks(self) -> Mapping[str, Any]:
        callbacks: Dict[str, Any] = {
            "status": lambda _payload: self.health(),
            "futu_status": self._opend_status_callback,
            "futu_send_verification": self._opend_send_verification,
            "futu_submit_verification": self._opend_submit_verification,
            "futu_relogin": self._opend_relogin,
            "backup": self._backup_callback,
            "system": lambda _payload: self.health(),
        }
        for name in (
            "futu_sync", "collect", "report", "recommendations", "portfolio",
            "contracts", "rules", "analysts", "backtest",
        ):
            callbacks[name] = lambda payload, callback_name=name: self._component_action(callback_name, payload)
        return callbacks

    def _backup_callback(self, _payload: Mapping[str, Any]) -> Mapping[str, Any]:
        archive = create_backup(self.paths)
        with self._lock:
            self.last_backup = str(archive)
        return {"status": "ok", "archive": str(archive)}

    def start(self) -> None:
        # Make a consistent restore point before SQLite schema initialization.
        if self.paths["database"].exists():
            self._backup()
        token = setup_token_from_environment()
        if not token:
            token_path = self.paths["root"] / "setup-token"
            if token_path.is_file():
                token = token_path.read_text(encoding="utf-8").strip()
            if len(token) < 16:
                token = secrets.token_urlsafe(24)
                token_path.write_text(token, encoding="utf-8")
                try:
                    token_path.chmod(0o600)
                except OSError:
                    pass
        print(f"SETUP CODE: {token}", flush=True)
        host = os.getenv("SETUP_HOST", "127.0.0.1")
        port = int(os.getenv("SETUP_PORT", "8787"))
        self.setup_server = create_setup_server(
            host, port, self.store, token, self.health, callbacks=self.dashboard_callbacks()
        )
        self.setup_thread = threading.Thread(
            target=self.setup_server.serve_forever,
            kwargs={"poll_interval": 0.5},
            name="setup-server",
            daemon=True,
        )
        self.setup_thread.start()

        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        config = self.store.load()
        timezone_name = str(config.get("timezone", os.getenv("TZ", "Asia/Shanghai")))
        self.scheduler = BackgroundScheduler(timezone=timezone_name, daemon=True)
        self.scheduler.add_job(
            self._heartbeat,
            "interval",
            seconds=20,
            id="runtime-heartbeat",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._backup,
            CronTrigger(hour=3, minute=15, timezone=timezone_name),
            id="daily-backup",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        self._heartbeat()

    def stop(self) -> None:
        self.stop_event.set()
        if self.component is not None and hasattr(self.component, "stop"):
            try:
                self.component.stop()
            except Exception:
                pass
        if self.opend_manager is not None:
            try:
                self.opend_manager.stop()
            except Exception:
                pass
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
        if self.setup_server is not None:
            self.setup_server.shutdown()
            self.setup_server.server_close()

    def run_forever(self) -> None:
        self.start()
        while not self.stop_event.wait(1.0):
            pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="python -m options_radar.nas_runtime")
    result.add_argument("--backup", action="store_true", help="create a consistent backup and exit")
    return result


def main() -> None:
    args = parser().parse_args()
    if args.backup:
        print(create_backup())
        return
    runtime = NasRuntime()

    def stop(_signum=None, _frame=None) -> None:
        runtime.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        runtime.run_forever()
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
