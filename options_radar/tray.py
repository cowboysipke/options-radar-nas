from __future__ import annotations

import threading
from datetime import datetime

from PIL import Image, ImageDraw

from .pipeline import RadarPipeline
from .timeutil import us_session_date_from_china_time


def run_tray(pipeline: RadarPipeline) -> None:
    try:
        import pystray  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pystray is not installed; install requirements.txt") from exc

    image = Image.new("RGB", (64, 64), "#111827")
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill="#22c55e")
    draw.text((20, 21), "OR", fill="white")

    def collect(icon, item):
        def worker():
            try:
                results = pipeline.collect_today(us_session_date_from_china_time(datetime.now()))
                icon.notify(f"完成 {len(results)} 张合约评价", "Options Radar")
            except Exception as exc:
                icon.notify(f"采集报错：{type(exc).__name__}: {str(exc)[:120]}", "Options Radar")
        threading.Thread(target=worker, daemon=True).start()

    def quit_app(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("采集今天", collect, default=True),
        pystray.MenuItem("退出", quit_app),
    )
    pystray.Icon("options-radar", image, "Options Radar", menu).run()
