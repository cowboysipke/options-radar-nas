from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .models import RawMessage, SourceCursor, SourceMessage


MESSAGE_ID = re.compile(r"(\d{15,25})")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_timestamp(value: object) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        return _utcnow()


class DiscordSource(ABC):
    @abstractmethod
    def fetch_since(
        self, channel_id: str, cursor: Optional[SourceCursor], scroll_pages: int = 0
    ) -> List[SourceMessage]:
        raise NotImplementedError

    def health(self) -> Dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class DiscordBrowserSource(DiscordSource):
    """Persistent-profile Discord web reader.

    It reads visible DOM nodes and uses screenshots/OCR only when an embed has
    no accessible text. No Discord user token, local storage value or request
    authorization header is inspected.
    """

    MESSAGE_SELECTOR = "li[id^='chat-messages'], [data-list-item-id^='chat-messages']"

    def __init__(
        self,
        profile_dir: Path,
        evidence_dir: Path,
        channel_urls: Dict[str, str],
        server_name: str = "",
        headless: bool = True,
        chromium_path: Optional[str] = None,
        timeout_ms: int = 30000,
    ):
        self.profile_dir = Path(profile_dir)
        self.evidence_dir = Path(evidence_dir)
        self.channel_urls = dict(channel_urls)
        self.server_name = server_name
        self.headless = headless
        self.chromium_path = chromium_path or os.getenv("CHROMIUM_EXECUTABLE_PATH")
        self.timeout_ms = timeout_ms
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = None
        self._context = None
        self._page = None
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="discord-browser")
        self._state = "stopped"
        self._last_success: Optional[str] = None
        self._last_error: Optional[str] = None
        self._qr_path: Optional[str] = None

    @staticmethod
    def normalize_dom_message(channel_id: str, item: Dict[str, object]) -> SourceMessage:
        raw_id = str(item.get("id") or "")
        match = MESSAGE_ID.search(raw_id)
        message_id = match.group(1) if match else raw_id
        created_at = _parse_timestamp(item.get("timestamp"))
        edited_at = _parse_timestamp(item.get("edited_at")) if item.get("edited_at") else None
        text = str(item.get("text") or "").strip()
        analyst = str(item.get("author") or "unknown").strip()
        embeds = item.get("embeds") if isinstance(item.get("embeds"), list) else []
        digest = hashlib.sha256(
            f"{channel_id}|{message_id}|{edited_at.isoformat() if edited_at else ''}|{text}".encode("utf-8")
        ).hexdigest()
        return SourceMessage(
            channel_id=channel_id, message_id=message_id, analyst=analyst,
            created_at=created_at, edited_at=edited_at, raw_text=text,
            embeds=[dict(value) for value in embeds if isinstance(value, dict)],
            content_hash=digest,
        )

    @staticmethod
    def as_raw_message(message: SourceMessage) -> RawMessage:
        content = message.raw_text
        if message.embeds:
            embed_text = "\n".join(
                str(value.get("text", "")) for value in message.embeds if value.get("text")
            )
            if embed_text and embed_text not in content:
                content = f"{content}\n{embed_text}".strip()
        return RawMessage(
            channel=message.channel_id, analyst=message.analyst,
            observed_at=message.created_at, source_timestamp=message.edited_at or message.created_at,
            content=content, screenshot_path=message.evidence_uri,
            content_hash=message.content_hash,
        )

    def start(self) -> None:
        with self._lock:
            if self._context is not None:
                return
            try:
                from playwright.sync_api import sync_playwright  # type: ignore
                self._playwright = sync_playwright().start()
                options: Dict[str, object] = {
                    "user_data_dir": str(self.profile_dir),
                    "headless": self.headless,
                    "viewport": {"width": 1440, "height": 1000},
                    "locale": "zh-CN",
                    "args": ["--disable-dev-shm-usage", "--no-sandbox"],
                }
                if self.chromium_path:
                    options["executable_path"] = self.chromium_path
                self._context = self._playwright.chromium.launch_persistent_context(**options)
                self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
                self._page.set_default_timeout(self.timeout_ms)
                self._state = "starting"
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}:{str(exc)[:120]}"
                self._state = "error"
                self.close()
                raise

    def _open_login_impl(self) -> Dict[str, object]:
        """Open the persistent Discord browser so local users can sign in."""
        with self._lock:
            try:
                if self._context is None:
                    self.start()
                if self._page is None:
                    raise RuntimeError("Discord浏览器未启动")
                self._page.goto("https://discord.com/channels/@me", wait_until="domcontentloaded")
                self._page.wait_for_timeout(1500)
                if self._logged_out():
                    self._capture_login()
                    return {**self.health(), "message": "Discord登录窗口已打开，请在弹出的浏览器中登录。"}
                self._state = "ready"
                self._last_error = None
                self._last_success = _utcnow().isoformat()
                return {**self.health(), "message": "Discord已经登录。"}
            except Exception as exc:
                self._state = "error"
                self._last_error = f"{type(exc).__name__}:{str(exc)[:160]}"
                return {**self.health(), "message": f"Discord启动失败：{str(exc)[:160]}"}

    def open_login(self) -> Dict[str, object]:
        return self._executor.submit(self._open_login_impl).result(timeout=max(60, self.timeout_ms / 1000 + 30))

    def _capture_login(self) -> None:
        if self._page is None:
            return
        path = self.evidence_dir / "discord-login.png"
        self._page.screenshot(path=str(path), full_page=False)
        self._qr_path = str(path)
        self._state = "login_required"

    def _logged_out(self) -> bool:
        if self._page is None:
            return True
        url = str(self._page.url)
        if "/login" in url:
            return True
        return bool(self._page.locator("input[name='email'], [data-qr-code]").count())

    @staticmethod
    def _ocr(path: Path) -> str:
        executable = shutil.which("tesseract")
        if not executable:
            return ""
        try:
            result = subprocess.run(
                [executable, str(path), "stdout", "-l", "chi_sim+eng"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _extract_dom(self, channel_id: str) -> List[SourceMessage]:
        if self._page is None:
            return []
        elements = self._page.locator(self.MESSAGE_SELECTOR)
        results: List[SourceMessage] = []
        for index in range(elements.count()):
            node = elements.nth(index)
            item = node.evaluate("""el => {
              const time = el.querySelector('time');
              const author = el.querySelector('[class*=username], h3 span');
              const edited = Array.from(el.querySelectorAll('time')).slice(-1)[0];
              const embeds = Array.from(el.querySelectorAll('[class*=embed]')).map(x => ({text: x.innerText || ''}));
              return {id: el.id || el.getAttribute('data-list-item-id') || '',
                timestamp: time ? time.getAttribute('datetime') : '',
                edited_at: edited && edited !== time ? edited.getAttribute('datetime') : '',
                author: author ? author.textContent : 'unknown', text: el.innerText || '', embeds};
            }""")
            message = self.normalize_dom_message(channel_id, dict(item))
            if not message.raw_text and not any(embed.get("text") for embed in message.embeds):
                evidence = self.evidence_dir / f"{channel_id}-{message.message_id}.png"
                node.screenshot(path=str(evidence))
                message.raw_text = self._ocr(evidence)
                message.evidence_uri = str(evidence)
                message.content_hash = hashlib.sha256(
                    f"{channel_id}|{message.message_id}|{message.raw_text}".encode("utf-8")
                ).hexdigest()
            results.append(message)
        return results

    def _resolve_channel_url(self, channel_id: str) -> Optional[str]:
        configured = self.channel_urls[channel_id]
        if configured.startswith("https://") or configured.startswith("http://"):
            return configured
        if configured.isdigit():
            guild = os.getenv("DISCORD_GUILD_ID", "").strip()
            if guild:
                return f"https://discord.com/channels/{guild}/{configured}"
        self._page.goto("https://discord.com/channels/@me", wait_until="domcontentloaded")
        self._page.wait_for_timeout(1200)
        if self._logged_out():
            self._capture_login()
            return None
        if self.server_name:
            server = self._page.locator(
                f"[aria-label*={json.dumps(self.server_name)}], [data-dnd-name={json.dumps(self.server_name)}]"
            )
            if server.count():
                server.first.click()
                self._page.wait_for_timeout(800)
            else:
                # The guild label can be hidden until the server list finishes
                # hydrating; for a configured channel URL it is not needed.
                # Continue with the visible channel/URL resolution instead of
                # aborting the entire collection.
                pass
        links = self._page.locator("a[href*='/channels/']").filter(has_text=configured)
        href = links.first.get_attribute("href") if links.count() else None
        # Discord's current client renders channel rows as buttons/divs in
        # some layouts, not anchor tags. Resolve those visible rows by exact
        # accessible text, click once, and read the canonical URL.
        if not href:
            candidates = self._page.get_by_text(configured, exact=True)
            if candidates.count() == 0:
                candidates = self._page.get_by_text(configured, exact=False)
            for index in range(min(candidates.count(), 8)):
                try:
                    candidates.nth(index).click(timeout=2000)
                    self._page.wait_for_timeout(500)
                    current = str(self._page.url)
                    if "/channels/" in current and "@me" not in current:
                        href = current
                        break
                except Exception:
                    continue
        if not href:
            # Persist a small diagnostic artifact instead of spinning/reloading
            # the browser. Discord may render rows asynchronously or split the
            # channel name across nested spans.
            try:
                diagnostic = self.evidence_dir / "discord-channel-list.txt"
                body = self._page.locator("body").inner_text(timeout=3000) if self._page else ""
                diagnostic.write_text(body[:20000], encoding="utf-8")
                if self._page:
                    self._page.screenshot(path=str(self.evidence_dir / "discord-channel-list.png"), full_page=False)
            except Exception:
                pass
            self._last_error = f"channel_not_found:{configured}"
            return None
        if href.startswith("/"):
            href = "https://discord.com" + href
        self.channel_urls[channel_id] = href
        return href

    def _fetch_since_impl(
        self, channel_id: str, cursor: Optional[SourceCursor], scroll_pages: int = 0
    ) -> List[SourceMessage]:
        with self._lock:
            if self._context is None:
                self.start()
            if channel_id not in self.channel_urls:
                raise KeyError(f"Discord channel is not configured: {channel_id}")
            try:
                channel_url = self._resolve_channel_url(channel_id)
                if not channel_url:
                    return []
                self._page.goto(channel_url, wait_until="commit")
                self._page.wait_for_timeout(5000)
                if self._logged_out():
                    self._capture_login()
                    return []
                self._state = "ready"
                messages = self._extract_dom(channel_id)
                for _ in range(max(0, scroll_pages)):
                    self._page.keyboard.press("PAGEUP")
                    self._page.wait_for_timeout(250)
                    messages.extend(self._extract_dom(channel_id))
                    if cursor and messages and min(item.created_at for item in messages) <= cursor.last_timestamp:
                        break
                messages = list({item.content_hash: item for item in messages}.values())
                if cursor:
                    messages = [message for message in messages if (
                        message.created_at > cursor.last_timestamp
                        or (message.created_at == cursor.last_timestamp and message.message_id > cursor.last_message_id)
                        or message.edited_at is not None and message.edited_at > cursor.last_timestamp
                    )]
                messages.sort(key=lambda value: (value.created_at, value.message_id))
                self._last_success = _utcnow().isoformat()
                self._last_error = None
                return messages
            except Exception as exc:
                self._state = "error"
                self._last_error = f"{type(exc).__name__}:{str(exc)[:120]}"
                return []

    def fetch_since(
        self, channel_id: str, cursor: Optional[SourceCursor], scroll_pages: int = 0
    ) -> List[SourceMessage]:
        return self._executor.submit(self._fetch_since_impl, channel_id, cursor, scroll_pages).result(timeout=180)

    def health(self) -> Dict[str, object]:
        return {
            "status": self._state, "profile_dir": str(self.profile_dir),
            "configured_channels": len(self.channel_urls), "last_success": self._last_success,
            "last_error": self._last_error, "login_qr_path": self._qr_path,
        }

    def close(self) -> None:
        with self._lock:
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
            self._context = None
            self._playwright = None
            self._page = None
            if self._state != "error":
                self._state = "stopped"


class FixtureDiscordSource(DiscordSource):
    """Deterministic source used for acceptance tests and offline demos."""

    def __init__(self, messages: Iterable[SourceMessage]):
        self.messages = list(messages)

    def fetch_since(
        self, channel_id: str, cursor: Optional[SourceCursor], scroll_pages: int = 0
    ) -> List[SourceMessage]:
        return [message for message in self.messages if message.channel_id == channel_id and (
            cursor is None or message.created_at > cursor.last_timestamp
            or (message.created_at == cursor.last_timestamp and message.message_id > cursor.last_message_id)
        )]

    def health(self) -> Dict[str, object]:
        return {"status": "ready", "messages": len(self.messages)}

    def close(self) -> None:
        return None
