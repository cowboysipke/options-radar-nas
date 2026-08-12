from __future__ import annotations

import re
import hashlib
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from PIL import ImageGrab

from .models import RawMessage
from .parser import CONTRACT_HEADER, RAW_FLOW, split_contract_messages
from .timeutil import us_session_date_from_china_time


SOURCE_DATE_PATTERNS = [
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})"),
    re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})"),
]


def extract_source_timestamp(text: str) -> Optional[datetime]:
    for pattern in SOURCE_DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            return datetime(*[int(item) for item in match.groups()])
    return None


class DiscordDesktopCollector:
    """Foreground collector using Discord's visible UI Automation tree.

    The logged-in desktop session is the sole access mechanism; no user token is read.
    """

    def __init__(self, settings: Dict[str, object], evidence_dir: Path, ocr_client=None):
        self.settings = settings
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.ocr_client = ocr_client
        self._local_ocr_engine = None

    def _ocr(self, path: Path) -> str:
        text_path = path.with_suffix(".txt")
        if text_path.exists():
            return text_path.read_text(encoding="utf-8")
        local_text = ""
        try:
            if self._local_ocr_engine is None:
                from rapidocr_onnxruntime import RapidOCR  # type: ignore
                self._local_ocr_engine = RapidOCR()
            result, _ = self._local_ocr_engine(str(path))
            if result:
                ordered = sorted(result, key=lambda item: (item[0][0][1], item[0][0][0]))
                local_text = "\n".join(str(item[1]) for item in ordered)
        except Exception:
            local_text = ""
        if (CONTRACT_HEADER.search(local_text) or RAW_FLOW.search(local_text)):
            text_path.write_text(local_text, encoding="utf-8")
            return local_text
        if self.ocr_client is not None:
            cloud_text = self.ocr_client.ocr_discord_image(path)
            text_path.write_text(cloud_text, encoding="utf-8")
            return cloud_text
        if local_text:
            text_path.write_text(local_text, encoding="utf-8")
        return local_text

    @staticmethod
    def _imports():
        try:
            from pywinauto import Desktop  # type: ignore
            from pywinauto.keyboard import send_keys  # type: ignore
            import win32clipboard  # type: ignore
            return Desktop, send_keys, win32clipboard
        except ImportError as exc:
            raise RuntimeError("desktop collection requires pywinauto and pywin32") from exc

    def _window(self):
        Desktop, _, _ = self._imports()
        pattern = str(self.settings.get("window_title_pattern", "Discord"))
        candidates = [window for window in Desktop(backend="uia").windows() if re.search(pattern, window.window_text(), re.I)]
        candidates = [window for window in candidates if "Discord" in window.window_text()]
        if not candidates:
            raise RuntimeError("Discord desktop window was not found")
        return candidates[0]

    @staticmethod
    def _clipboard_text(text: str, win32clipboard) -> None:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()

    def _navigate(self, window, channel: str) -> None:
        _, send_keys, win32clipboard = self._imports()
        window.set_focus()
        server_name = str(self.settings.get("source_server", "Alpha夜航社"))
        server_items = [
            item for item in window.descendants(control_type="TreeItem")
            if server_name in (item.window_text() or "")
        ]
        if server_items:
            server_items[0].click_input()
            time.sleep(0.8)
            channel_links = [
                item for item in window.descendants(control_type="Hyperlink")
                if re.search(
                    rf"(?:^|，){re.escape(channel)}（文字频道）",
                    item.window_text() or "",
                )
            ]
            if channel_links:
                channel_links[0].click_input()
                time.sleep(float(self.settings.get("initial_wait_seconds", 2.0)))
                send_keys("^{END}")
                time.sleep(0.5)
                return
        # If the desktop client is actually a Chrome Discord shell, its UIA
        # tree may expose only the Friends landing page. A full channel URL is
        # the reliable navigation target and does not depend on sidebar text.
        if str(channel).startswith(("https://", "http://")):
            send_keys("^l")
            self._clipboard_text(str(channel), win32clipboard)
            send_keys("^v")
            send_keys("{ENTER}")
            time.sleep(float(self.settings.get("initial_wait_seconds", 5.0)))
            send_keys("^{END}")
            time.sleep(0.5)
            return
        # Fallback when Discord has virtualized the channel out of the sidebar.
        send_keys("^k")
        time.sleep(0.3)
        self._clipboard_text("#" + channel, win32clipboard)
        send_keys("^v")
        time.sleep(0.7)
        send_keys("{ENTER}")
        time.sleep(float(self.settings.get("initial_wait_seconds", 2.0)))
        send_keys("^{END}")
        time.sleep(0.5)

    def _uia_messages(self, window, channel: str, analyst: str, target_date: date) -> List[RawMessage]:
        messages: List[RawMessage] = []
        seen = set()
        for control in window.descendants(control_type="ListItem"):
            name = control.window_text().strip()
            if not name or not (CONTRACT_HEADER.search(name) or RAW_FLOW.search(name)):
                continue
            timestamp = extract_source_timestamp(name)
            if timestamp and us_session_date_from_china_time(timestamp) != target_date:
                continue
            for block in split_contract_messages(name):
                key = " ".join(block.split())
                if key in seen:
                    continue
                seen.add(key)
                messages.append(RawMessage(
                    channel=channel,
                    analyst=analyst,
                    observed_at=datetime.now(),
                    source_timestamp=timestamp,
                    content=block,
                ))
        return messages

    def _screenshot(self, window, channel: str, page: int) -> Path:
        rectangle = window.rectangle()
        width, height = rectangle.width(), rectangle.height()
        left = rectangle.left + int(width * float(self.settings.get("capture_left_ratio", 0.30)))
        top = rectangle.top + int(height * float(self.settings.get("capture_top_ratio", 0.10)))
        right = rectangle.left + int(width * float(self.settings.get("capture_right_ratio", 0.98)))
        bottom = rectangle.top + int(height * float(self.settings.get("capture_bottom_ratio", 0.92)))
        safe_channel = re.sub(r"[^A-Za-z0-9._-]+", "_", channel).strip("_")
        if not safe_channel:
            safe_channel = hashlib.sha1(channel.encode("utf-8")).hexdigest()[:8]
        path = self.evidence_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{safe_channel}_{page}.png"
        ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).save(str(path))
        return path

    def collect_channel(self, channel: str, analyst: str, target_date: date) -> List[RawMessage]:
        _, send_keys, _ = self._imports()
        window = self._window()
        self._navigate(window, channel)
        max_pages = int(self.settings.get("max_pages_per_channel", 8))
        gathered: Dict[str, RawMessage] = {}
        empty_pages = 0
        for page in range(max_pages):
            before = len(gathered)
            current = self._uia_messages(window, channel, analyst, target_date)
            for message in current:
                gathered[" ".join(message.content.split())] = message
            screenshot = self._screenshot(window, channel, page)
            if not current:
                text = self._ocr(screenshot)
                for block in split_contract_messages(text):
                    gathered[" ".join(block.split())] = RawMessage(
                        channel=channel,
                        analyst=analyst,
                        observed_at=datetime.now(),
                        content=block,
                        screenshot_path=str(screenshot),
                    )
            if len(gathered) == before:
                empty_pages += 1
            else:
                empty_pages = 0
            if empty_pages >= 2:
                break
            send_keys("{PGUP}")
            time.sleep(float(self.settings.get("page_wait_seconds", 1.0)))
        return list(gathered.values())

    def collect_today(self, channels: Dict[str, str], target_date: Optional[date] = None) -> List[RawMessage]:
        target_date = target_date or date.today()
        results: List[RawMessage] = []
        for channel, analyst in channels.items():
            results.extend(self.collect_channel(channel, analyst, target_date))
        return results
