"""Windows-first runtime that reuses the production service and dashboard."""

from __future__ import annotations

import os
import socket
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, Mapping

from .service import OptionsRadarService
from .setup_server import BUILD_SHA, BUILD_VERSION, SetupConfigStore, create_setup_server


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

    @staticmethod
    def _assert_port_available(host: str, port: int) -> None:
        """Fail clearly instead of binding over an unrelated/old dashboard."""
        if port == 0:
            return
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((host, port))
        except OSError as exc:
            detail = "端口已被其他进程占用"
            try:
                with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1.0) as response:
                    payload = response.read(16384).decode("utf-8", "replace")
                if '"build"' in payload and '"version":"v2-local"' in payload:
                    detail = "V2 已在运行，请直接使用当前浏览器页面"
                else:
                    detail = "端口被旧版或其他服务占用"
            except (OSError, urllib.error.URLError, ValueError):
                pass
            raise RuntimeError(f"无法启动本地面板：{detail}（{host}:{port}）。请先关闭占用该端口的进程。") from exc
        finally:
            probe.close()

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
        self._assert_port_available("127.0.0.1", port)
        token = ""
        # Configuration is saved by /setup before scheduled jobs are allowed.
        if self.store.load().get("setup_completed"):
            self.service.start()
        self.server = create_setup_server(
            "127.0.0.1", port, self.store, token, self.service.health,
            callbacks=self.callbacks(), local_mode=True,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        print(f"本地面板: http://127.0.0.1:{port}/")
        print(f"版本: {BUILD_VERSION}  Git SHA: {BUILD_SHA}")
        print("本地模式：无需管理口令（仅监听 127.0.0.1）")
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
