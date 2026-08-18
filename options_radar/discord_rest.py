"""Read-only Discord message collection through the official HTTP API.

The REST source replaces the fragile DOM scraper whenever the operator can
provide a Discord user token.  It paginates channel history by message id,
parses every message body + embed into a :class:`SourceMessage`, and stores a
per-channel cursor.  No UI automation, no OCR, no token stored in the database.

The token is an access token for a personal Discord account ("self-bot").  It
must be supplied via ``DISCORD_USER_TOKEN`` (or a file referenced by
``DISCORD_USER_TOKEN_FILE`` / ``secret_refs.discord_user_token``).  Discord's
Terms of Service do not allow automating a personal account; the operator
accepts that risk when enabling this source.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .discord_source import DiscordSource, source_message_as_raw
from .models import RawMessage, SourceCursor, SourceMessage


DISCORD_API_BASE = "https://discord.com/api/v10"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
PAGE_SIZE = 100
MAX_PAGES = 200
RETRY_AFTER_429_SECONDS = 2.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_timestamp(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        return _utcnow()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        return _utcnow()


def channel_snowflake(target: str) -> Optional[str]:
    """Extract the numeric channel id from a URL or a bare id."""
    text = str(target or "").strip()
    if not text:
        return None
    if text.isdigit():
        return text
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme in {"http", "https"}:
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[-1].isdigit():
            return parts[-1]
    digits = "".join(character for character in text if character.isdigit())
    return digits if digits else None


def _embed_text(embed: Dict[str, Any]) -> str:
    """Render one Discord embed as searchable text for the deterministic parser."""
    lines: List[str] = []
    if embed.get("author") and embed["author"].get("name"):
        lines.append(str(embed["author"]["name"]))
    title = embed.get("title")
    if title:
        lines.append(str(title))
    description = embed.get("description")
    if description:
        lines.append(str(description))
    for field in embed.get("fields") or []:
        name = str(field.get("name", ""))
        value = str(field.get("value", ""))
        if name and value:
            lines.append(f"{name}: {value}")
        elif name:
            lines.append(name)
        elif value:
            lines.append(value)
    if embed.get("footer") and embed["footer"].get("text"):
        lines.append(str(embed["footer"]["text"]))
    return "\n".join(line for line in lines if line.strip())


class DiscordRestSource(DiscordSource):
    """Pagination-based Discord channel reader using a user token.

    ``channel_targets`` maps the source role (the pipeline's channel label) to
    either a channel URL, a numeric channel id, or a channel name.  Names are
    resolved through ``GET /guilds/{guild_id}/channels`` when ``guild_id`` is
    configured, so a Chinese-only config works out of the box.
    """

    def __init__(
        self,
        channel_targets: Dict[str, str],
        token: str,
        base_url: str = DISCORD_API_BASE,
        timeout_ms: int = 30000,
        page_size: int = PAGE_SIZE,
        guild_id: str = "",
        proxy_url: str = "",
    ):
        self.channel_targets = {str(role): str(target) for role, target in channel_targets.items()}
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout_ms = int(timeout_ms)
        self.page_size = max(1, min(int(page_size), 100))
        self.guild_id = str(guild_id or "").strip() or None
        self.proxy_url = str(proxy_url or "").strip() or None
        self._opener = self._build_opener() if self.proxy_url else None
        self._lock = threading.RLock()
        self._state = "ready" if token else "stopped"
        self._last_success: Optional[str] = None
        self._last_error: Optional[str] = None
        self._messages_seen = 0
        self._resolved: Dict[str, Optional[str]] = {}
        self._guild_channels: Optional[Dict[str, str]] = None

    @property
    def channel_urls(self) -> Dict[str, str]:
        """Compatibility view so the service iterates sources uniformly."""
        return dict(self.channel_targets)

    @staticmethod
    def as_raw_message(message: SourceMessage) -> RawMessage:
        return source_message_as_raw(message)

    def _load_guild_channels(self) -> None:
        if self._guild_channels is not None or not self.guild_id:
            return
        try:
            payload = self._request("GET", f"/guilds/{self.guild_id}/channels")
        except Exception:
            payload = None
        resolved: Dict[str, str] = {}
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and item.get("id") and item.get("name"):
                    resolved[str(item["name"]).strip().lower()] = str(item["id"])
        with self._lock:
            self._guild_channels = resolved

    def _snowflake(self, role: str) -> Optional[str]:
        with self._lock:
            if role in self._resolved:
                return self._resolved[role]
        target = self.channel_targets.get(role)
        if not target:
            with self._lock:
                self._resolved[role] = None
            return None
        snowflake = channel_snowflake(target)
        if snowflake is None:
            self._load_guild_channels()
            if self._guild_channels:
                needle = str(target).strip().lower()
                snowflake = self._guild_channels.get(needle)
                if snowflake is None:
                    # Discord strips the 频道 suffix from names; a contains
                    # match keeps Chinese names like "异常期权" working.
                    candidates = [
                        channel_id for name, channel_id in self._guild_channels.items()
                        if needle in name or name in needle
                    ]
                    snowflake = candidates[0] if candidates else None
        with self._lock:
            self._resolved[role] = snowflake
        if snowflake is None:
            self._last_error = f"channel_id_unresolvable:{role}"
        return snowflake

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": self.token,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }

    def _build_opener(self) -> Optional[urllib.request.OpenerDirector]:
        """Route HTTPS through an HTTP proxy (Clash/v2ray etc.) when configured.

        Used from mainland China where Discord is not reachable directly.
        """
        proxy = urllib.request.ProxyHandler({"http": self.proxy_url, "https": self.proxy_url})
        return urllib.request.build_opener(proxy)

    def _request(
        self, method: str, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(url, headers=self._headers(), method=method)
        for attempt in range(3):
            try:
                if self._opener is not None:
                    with self._opener.open(request, timeout=self.timeout_ms / 1000.0) as response:
                        return json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(request, timeout=self.timeout_ms / 1000.0) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    time.sleep(RETRY_AFTER_429_SECONDS * (attempt + 1))
                    continue
                raise RuntimeError(f"discord_http_{exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise RuntimeError("discord_network_error") from exc
            except (ValueError, UnicodeError) as exc:
                raise RuntimeError("discord_invalid_json") from exc
        raise RuntimeError("discord_http_429")

    def _messages_from_payload(self, role: str, payload: Any) -> List[SourceMessage]:
        items: List[Dict[str, Any]] = []
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("messages"), list):
            items = payload["messages"]
        output: List[SourceMessage] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            message_id = str(item.get("id") or "")
            if not message_id:
                continue
            author = ""
            author_obj = item.get("author")
            if isinstance(author_obj, dict):
                author = str(author_obj.get("username") or author_obj.get("id") or "").strip()
            text = str(item.get("content") or "").strip()
            embeds: List[Dict[str, Any]] = []
            for embed in item.get("embeds") or []:
                if isinstance(embed, dict):
                    embeds.append({"text": _embed_text(embed)})
            created_at = _parse_timestamp(item.get("timestamp"))
            edited_at = _parse_timestamp(item.get("edited_timestamp")) if item.get("edited_timestamp") else None
            digest = hashlib.sha256(
                f"{role}|{message_id}|{edited_at.isoformat() if edited_at else ''}|{text}".encode("utf-8")
            ).hexdigest()
            output.append(SourceMessage(
                channel_id=role,
                message_id=message_id,
                analyst=author,
                created_at=created_at,
                edited_at=edited_at,
                raw_text=text,
                embeds=embeds,
                attachments=[str(url) for url in (item.get("attachments") or []) if isinstance(url, dict) and url.get("url")],
                content_hash=digest,
            ))
        return output

    def fetch_since(
        self, channel_id: str, cursor: Optional[SourceCursor], scroll_pages: int = 0,
        timeout_ms: Optional[int] = None,
    ) -> List[SourceMessage]:
        if timeout_ms is not None:
            self.timeout_ms = int(timeout_ms)
        with self._lock:
            snowflake = self._snowflake(channel_id)
            if snowflake is None:
                self._state = "error"
                return []
            pages = max(1, min(int(scroll_pages) + 1, MAX_PAGES))
            collected: Dict[str, SourceMessage] = {}
            before: Optional[str] = None
            reached_cursor = False
            try:
                for _ in range(pages):
                    params: Dict[str, Any] = {"limit": self.page_size}
                    if before:
                        params["before"] = before
                    payload = self._request("GET", f"/channels/{snowflake}/messages", params)
                    messages = self._messages_from_payload(channel_id, payload)
                    if not messages:
                        break
                    for message in messages:
                        collected[message.message_id] = message
                        if cursor is not None:
                            if message.created_at < cursor.last_timestamp:
                                reached_cursor = True
                            elif (
                                message.created_at == cursor.last_timestamp
                                and message.message_id <= cursor.last_message_id
                            ):
                                reached_cursor = True
                    if reached_cursor or len(messages) < self.page_size:
                        break
                    before = messages[-1].message_id
            except Exception as exc:
                self._state = "error"
                self._last_error = f"{type(exc).__name__}:{str(exc)[:120]}"
                return sorted(collected.values(), key=lambda value: (value.created_at, value.message_id))

            output = list(collected.values())
            if cursor:
                output = [
                    message for message in output
                    if message.created_at > cursor.last_timestamp
                    or (
                        message.created_at == cursor.last_timestamp
                        and message.message_id > cursor.last_message_id
                    )
                    or (
                        message.edited_at is not None
                        and message.edited_at > cursor.last_timestamp
                    )
                ]
            output.sort(key=lambda value: (value.created_at, value.message_id))
            self._state = "ready"
            self._last_error = None
            self._last_success = _utcnow().isoformat()
            self._messages_seen += len(output)
            return output

    def health(self) -> Dict[str, object]:
        result: Dict[str, object] = {
            "status": self._state,
            "collector": "discord_rest",
            "token_configured": bool(self.token),
            "configured_channels": len(self.channel_targets),
            "resolved_channels": sum(1 for value in self._resolved.values() if value),
            "guild_id": self.guild_id or "",
            "proxy_url": self.proxy_url or "",
            "last_success": self._last_success,
            "last_error": self._last_error,
            "messages_seen": self._messages_seen,
        }
        # Probe token validity with a lightweight read-only call.
        if self.token and self._state != "stopped":
            try:
                self._request("GET", "/users/@me")
                result["token_valid"] = True
            except Exception as exc:
                text = str(exc)
                if "http_401" in text:
                    result["token_valid"] = False
                    result["token_error"] = "expired_or_revoked"
                    result["status"] = "token_invalid"
                elif "http_403" in text:
                    result["token_valid"] = False
                    result["token_error"] = "forbidden"
                else:
                    result["token_valid"] = None
        return result

    def close(self) -> None:
        return None


def read_token(environ: Optional[Dict[str, str]] = None) -> str:
    """Read the Discord user token from the environment or a secret file."""
    env = os.environ if environ is None else environ
    token = str(env.get("DISCORD_USER_TOKEN", "") or env.get("DISCORD_TOKEN", "")).strip()
    file_var = str(env.get("DISCORD_USER_TOKEN_FILE", "") or env.get("DISCORD_TOKEN_FILE", "")).strip()
    if not token and file_var:
        path = Path(file_var)
        if path.is_file():
            token = path.read_text(encoding="utf-8").strip()
    return token


def build_discord_source(
    settings: Dict[str, object],
    data_dir: Path,
    token: str = "",
) -> DiscordSource:
    """Pick the collector backend: REST first when a token is present, otherwise web."""
    if token or read_token():
        return DiscordRestSource(
            channel_targets=dict(settings.get("channel_urls", {})) or dict(settings.get("channel_names", {})),
            token=token or read_token(),
        )
    from .discord_source import DiscordBrowserSource

    channels = dict(settings.get("channel_urls", {})) or dict(settings.get("channel_names", {}))
    return DiscordBrowserSource(
        profile_dir=data_dir / "browser-profile",
        evidence_dir=data_dir / "evidence",
        channel_urls=channels,
        server_name=str(settings.get("source_server", "")),
        headless=bool(settings.get("headless", True)),
    )


__all__ = [
    "DiscordRestSource",
    "channel_snowflake",
    "read_token",
    "build_discord_source",
    "DISCORD_API_BASE",
]
