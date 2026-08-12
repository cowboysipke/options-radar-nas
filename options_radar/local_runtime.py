"""Windows-first runtime that reuses the production service and dashboard."""

from __future__ import annotations

import os
import secrets
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, Mapping

from .service import OptionsRadarService
from .setup_server import SetupConfigStore, create_setup_server


class LocalRuntime:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.data_dir = self.root / "data-local"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("DATA_DIR", str(self.data_dir))
        os.environ.setdefault("OPTIONS_RADAR_LOCAL", "1")
        config_path = self.root / "config.local.yaml"
        if not config_path.exists():
            template = self.root / "config.example.yaml"
            config_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        self.store = SetupConfigStore(config_path)
        self.store.initialize()
        self.service = OptionsRadarService(str(config_path), str(self.data_dir))
        self.server = None
        self.thread = None

    def callbacks(self) -> Mapping[str, Any]:
        callbacks: Dict[str, Any] = dict(self.service.dashboard_callbacks())
        callbacks.setdefault("status", lambda _payload: self.service.health())
        callbacks.setdefault("futu_status", lambda _payload: self.service.health().get("opend", {}))
        callbacks["reload_service"] = self.reload_service
        return callbacks

    def reload_service(self, _payload: Mapping[str, Any]) -> Dict[str, Any]:
        self.service.stop()
        self.service = OptionsRadarService(str(self.store.path), str(self.data_dir))
        self.service.start()
        return {"status": "ready", "message": "配置已保存，后台任务已启动"}

    def run(self, port: int = 8787, open_browser: bool = True) -> None:
        token_path = self.data_dir / "setup-token"
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
        else:
            token = secrets.token_urlsafe(24)
            token_path.write_text(token, encoding="utf-8")
        # Configuration is saved by /setup before scheduled jobs are allowed.
        if self.store.load().get("setup_completed"):
            self.service.start()
        self.server = create_setup_server(
            "127.0.0.1", port, self.store, token, self.service.health,
            callbacks=self.callbacks(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        print(f"本地面板: http://127.0.0.1:{port}/")
        print(f"管理口令: {token}")
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        try:
            self.thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.service.stop()


def main() -> None:
    LocalRuntime(Path(__file__).resolve().parent.parent).run(
        int(os.getenv("OPTIONS_RADAR_PORT", "8787")),
        os.getenv("OPTIONS_RADAR_NO_BROWSER", "") != "1",
    )


if __name__ == "__main__":
    main()
