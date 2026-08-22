import unittest
from datetime import datetime, timezone

from options_radar.discord_rest import (
    DiscordRestSource,
    channel_snowflake,
    read_token,
)
from options_radar.models import SourceCursor


def _discord_ts(value):
    """Render a datetime as a real Discord API timestamp (UTC, trailing Z)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _message(item_id, content, timestamp, author="flow_bot", edited=None):
    payload = {
        "id": str(item_id),
        "content": content,
        "timestamp": _discord_ts(timestamp),
        "author": {"username": author},
    }
    if edited:
        payload["edited_timestamp"] = _discord_ts(edited)
    else:
        payload["edited_timestamp"] = None
    return payload


class ChannelSnowflakeTests(unittest.TestCase):
    def test_extracts_channel_id_from_url(self):
        self.assertEqual(
            channel_snowflake("https://discord.com/channels/1434960637561409689/1434960638559522999"),
            "1434960638559522999",
        )

    def test_passes_bare_id_through(self):
        self.assertEqual(channel_snowflake("123456789012345678"), "123456789012345678")

    def test_rejects_garbage(self):
        self.assertIsNone(channel_snowflake("not-a-channel"))


class DiscordProxyTests(unittest.TestCase):
    def test_proxy_url_enables_opener_and_reports_health(self):
        source = DiscordRestSource(
            channel_targets={"flow": "100000000000000001"},
            token="t",
            proxy_url="http://127.0.0.1:7890",
        )
        self.assertIsNotNone(source._opener)
        self.assertEqual(source.health()["proxy_url"], "http://127.0.0.1:7890")

    def test_no_proxy_keeps_default_urlopen(self):
        source = DiscordRestSource(channel_targets={"flow": "100000000000000001"}, token="t")
        self.assertIsNone(source._opener)
        self.assertEqual(source.health()["proxy_url"], "")


class DiscordRestSourceTests(unittest.TestCase):
    def _source(self, pages=None, page_size=100):
        source = DiscordRestSource(
            channel_targets={"flow": "https://discord.com/channels/1/100000000000000001"},
            token="test-token",
            page_size=page_size,
        )
        calls = []

        def fake_request(method, path, params=None):
            calls.append((method, path, params))
            if pages is None or len(calls) > len(pages):
                return {"messages": []}
            return pages[len(calls) - 1]

        source._request = fake_request
        return source, calls

    def test_paginates_until_cursor_and_filters_new(self):
        old = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)
        new = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)
        page_one = {"messages": [
            _message(5002, "TSLA 2026-08-21 300 C | 解读", new),
            _message(5001, "NVDA 2026-08-21 140 C | 解读", old),
        ]}
        source, calls = self._source(pages=[page_one])
        cursor = SourceCursor(channel_id="flow", last_message_id="5001", last_timestamp=old)
        messages = source.fetch_since("flow", cursor, scroll_pages=2)
        self.assertEqual([item.message_id for item in messages], ["5002"])
        self.assertEqual(messages[0].analyst, "flow_bot")
        self.assertEqual(calls[0][1], "/channels/100000000000000001/messages")
        self.assertEqual(calls[0][2]["limit"], 100)

    def test_no_cursor_returns_all_pages(self):
        new = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)
        pages = [
            {"messages": [_message(5002, "AAA 2026-08-21 100 C | 解读", new)]},
            {"messages": [_message(5001, "BBB 2026-08-21 100 C | 解读", new)]},
        ]
        source, calls = self._source(pages=pages, page_size=1)
        messages = source.fetch_since("flow", None, scroll_pages=2)
        self.assertEqual([item.message_id for item in messages], ["5001", "5002"])
        # One extra request confirms history exhaustion with an empty page.
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1][2]["before"], "5002")

    def test_plain_list_payload_is_accepted(self):
        # The real Discord API returns a bare JSON array for the messages
        # endpoint; a wrapped {"messages": [...]} payload was the old
        # fixture-only shape and silently dropped every message.
        new = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)
        source = DiscordRestSource(
            channel_targets={"flow": "100000000000000001"},
            token="t",
            page_size=2,
        )

        def fake_request(method, path, params=None):
            if params and params.get("before"):
                return [_message(5000, "CC 2026-08-21 100 C | 解读", new)]
            return [
                _message(5002, "AA 2026-08-21 100 C | 解读", new),
                _message(5001, "BB 2026-08-21 100 C | 解读", new),
            ]

        source._request = fake_request
        messages = source.fetch_since("flow", None, scroll_pages=1)
        self.assertEqual([item.message_id for item in messages], ["5000", "5001", "5002"])
        self.assertEqual(source.health()["status"], "ready")

    def test_embed_text_is_rendered_into_raw(self):
        new = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)
        payload = {"messages": [{
            "id": "5002",
            "content": "SPY 2026-08-21 580 C",
            "timestamp": _discord_ts(new),
            "edited_timestamp": None,
            "author": {"username": "mr"},
            "embeds": [{
                "title": "MR 解读",
                "description": "Premium $1,234,567",
                "fields": [{"name": "方向", "value": "偏多"}, {"name": "DTE", "value": "15"}],
            }],
        }]}
        source, _ = self._source(pages=[payload])
        messages = source.fetch_since("flow", None, scroll_pages=0)
        raw = source.as_raw_message(messages[0])
        self.assertIn("Premium $1,234,567", raw.content)
        self.assertIn("方向: 偏多", raw.content)
        self.assertEqual(raw.analyst, "mr")

    def test_rate_limited_channel_returns_empty_not_raise(self):
        def failing(*_args, **_kwargs):
            raise RuntimeError("discord_http_403")

        source = DiscordRestSource(
            channel_targets={"flow": "100000000000000001"}, token="t",
        )
        source._request = failing
        self.assertEqual(source.fetch_since("flow", None, scroll_pages=0), [])
        self.assertEqual(source.health()["status"], "error")

    def test_channel_name_resolved_through_guild_channels(self):
        new = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)
        calls = []

        def fake_request(method, path, params=None):
            calls.append(path)
            if path == "/guilds/111111111111111111/channels":
                return [
                    {"id": "100000000000000001", "name": "异常期权"},
                    {"id": "100000000000000002", "name": "pa分析师"},
                ]
            return {"messages": [_message(5002, "TSLA 2026-08-21 300 C | 解读", new)]}

        source = DiscordRestSource(
            channel_targets={"flow": "异常期权", "pa": "pa分析师"},
            token="t", guild_id="111111111111111111",
        )
        source._request = fake_request
        messages = source.fetch_since("flow", None, scroll_pages=0)
        self.assertEqual([item.message_id for item in messages], ["5002"])
        self.assertIn("/channels/100000000000000001/messages", calls)
        self.assertEqual(source.health()["resolved_channels"], 1)

    def test_read_token_from_env(self):
        env = {"DISCORD_USER_TOKEN": "abc"}
        self.assertEqual(read_token(env), "abc")
        self.assertEqual(read_token({"DISCORD_TOKEN": "xyz"}), "xyz")


if __name__ == "__main__":
    unittest.main()
