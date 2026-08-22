# -*- coding: utf-8 -*-
"""Phase 4: GEX alerts and Flow Type classification."""
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from options_radar.db import Database
from options_radar.models import FlowEvent
from options_radar.service import OptionsRadarService


def flow_event(symbol, event_key, premium=100000.0, raw_id=None):
    return FlowEvent(
        event_key=event_key, raw_message_id=raw_id,
        contract_key=f"US.{symbol}|2026-09-18|200|C", symbol=symbol,
        expiry=date(2026, 9, 18), strike=200.0, option_type="C",
        premium=premium, average_price=2.0, dte=30,
        observed_at=datetime.utcnow() - timedelta(hours=2),
        session_date=date(2026, 8, 21),
    )


class FakeFeishu:
    def __init__(self):
        self.cards = []

    def enqueue_card(self, card, source_message_id=None):
        self.cards.append((card, source_message_id))
        return "queued"


def fake_gex(spot=100.0, put_wall=None, call_wall=None, flip=None, regime="positive"):
    return SimpleNamespace(
        strikes=[95.0, 100.0, 105.0], net_gex=[1.0, -1.0, 1.0],
        spot=spot, put_wall=put_wall, call_wall=call_wall, gamma_flip=flip,
        regime=regime,
    )


class CheckAlertsTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(OptionsRadarService)
        self.service._trade_date = lambda: date(2026, 8, 21)
        self.service.feishu = FakeFeishu()
        self.service._last_alert_keys = set()
        self.service._last_alert_date = "2026-08-21"

    def make_db(self, gex, previous_regime=None, iv_rank=None):
        class FakeDatabase:
            def flow_events_for_date(self, trade_date):
                return [flow_event("NVDA", "EV1")]

            def latest_gex_regime_before(self, symbol, before_date):
                return previous_regime

        service = self.service
        service.database = FakeDatabase()
        service.get_gex = lambda symbol: gex
        service._underlying_overview = lambda symbol: {"iv_rank": iv_rank}
        return service

    def test_wall_proximity_alert(self):
        service = self.make_db(fake_gex(spot=100.0, put_wall=100.5), iv_rank=None)
        result = service.check_alerts()
        self.assertEqual(result["alerts"], 1)
        text = service.feishu.cards[0][0]["elements"][0]["content"]
        self.assertIn("逼近 Put Wall", text)

    def test_regime_flip_alert(self):
        service = self.make_db(fake_gex(regime="negative"), previous_regime="positive", iv_rank=None)
        result = service.check_alerts()
        self.assertEqual(result["alerts"], 1)
        text = service.feishu.cards[0][0]["elements"][0]["content"]
        self.assertIn("Regime 翻转", text)

    def test_iv_rank_alert(self):
        service = self.make_db(fake_gex(), iv_rank=85.0)
        result = service.check_alerts()
        self.assertEqual(result["alerts"], 1)
        text = service.feishu.cards[0][0]["elements"][0]["content"]
        self.assertIn("IV Rank 85", text)

    def test_dedup_within_session(self):
        service = self.make_db(fake_gex(spot=100.0, call_wall=100.2), iv_rank=90.0)
        first = service.check_alerts()
        self.assertGreaterEqual(first["alerts"], 1)
        second = service.check_alerts()
        self.assertEqual(second["alerts"], 0)

    def test_skips_empty_gex(self):
        service = self.make_db(None, iv_rank=None)
        self.assertEqual(service.check_alerts()["alerts"], 0)


class DeterministicFlowTypeTests(unittest.TestCase):
    def test_keywords(self):
        hint = OptionsRadarService._deterministic_flow_type
        self.assertEqual(hint("sold a bull put spread"), "Spread")
        self.assertEqual(hint("hedging position with collars"), "Hedging")
        self.assertEqual(hint("closing position, stc 100"), "Closing")
        self.assertEqual(hint("large sweep order bought calls"), "Directional")
        self.assertEqual(hint(""), "Unknown")
        self.assertEqual(hint("hello world"), "Unknown")


class FlowClassificationDbTests(unittest.TestCase):
    def test_save_map_and_unclassified(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "radar.db")
            database.insert_flow_event(flow_event("NVDA", "EV1", premium=1000.0, raw_id=None))
            database.insert_flow_event(flow_event("AAPL", "EV2", premium=9000.0, raw_id=None))
            pending = database.unclassified_flow_events(5)
            self.assertEqual({key for key, _ in pending}, {"EV2", "EV1"})
            database.save_flow_classification("EV1", "Directional", 42.5, "rule")
            self.assertEqual(
                {key for key, _ in database.unclassified_flow_events(5)}, {"EV2"},
            )
            mapping = database.flow_classification_map(["EV1", "EV2"])
            self.assertEqual(mapping["EV1"]["flow_type"], "Directional")
            self.assertNotIn("EV2", mapping)
            self.assertEqual(database.flow_premium_for_event("EV2"), 9000.0)
            self.assertEqual(database.premium_percentile(1000.0), 50.0)
            self.assertEqual(database.premium_percentile(9000.0), 100.0)

    def test_raw_message_text(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "radar.db")
            with database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO raw_messages (channel, analyst, observed_at, content, content_hash) "
                    "VALUES ('flow', 'flow', ?, 'sweep order', 'h1')",
                    (datetime.utcnow().isoformat(),),
                )
                raw_id = int(cursor.lastrowid)
            self.assertEqual(database.raw_message_text(raw_id), "sweep order")
            self.assertIsNone(database.raw_message_text(99999))


class ClassifyFlowEventsTests(unittest.TestCase):
    def test_rule_and_ai_paths(self):
        class FakeDatabase:
            def __init__(self):
                self.saved = []

            def unclassified_flow_events(self, limit):
                return [("EV1", 1), ("EV2", 2)]

            def raw_message_text(self, raw_message_id):
                return {1: "bull put spread order", 2: "something cryptic"}[raw_message_id]

            def flow_premium_for_event(self, event_key):
                return 5000.0

            def premium_percentile(self, premium):
                return 77.7

            def save_flow_classification(self, event_key, flow_type, strength, source):
                self.saved.append((event_key, flow_type, strength, source))

        class FakeAI:
            enabled = True

            def classify_flow_type(self, text):
                return "Hedging"

        service = object.__new__(OptionsRadarService)
        service.database = FakeDatabase()
        service.ai = FakeAI()
        result = service.classify_flow_events(limit=10)
        self.assertEqual(result["classified"], 2)
        self.assertEqual(result["ai_calls"], 1)
        self.assertEqual(service.database.saved[0], ("EV1", "Spread", 77.7, "rule"))
        self.assertEqual(service.database.saved[1], ("EV2", "Hedging", 77.7, "ai"))


if __name__ == "__main__":
    unittest.main()
