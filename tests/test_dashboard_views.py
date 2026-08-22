import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from options_radar.db import Database
from options_radar.feishu import build_card
from options_radar.models import PortfolioContext
from options_radar.service import OptionsRadarService


class FakeFeishu:
    def __init__(self):
        self.card = None
        self.source = None

    def enqueue_card(self, card, source_message_id=None):
        self.card = card
        self.source = source_message_id
        return "queued"


class DashboardViewTests(unittest.TestCase):
    def test_recommendation_view_flattens_execution_and_builds_reason(self):
        payload = {
            "contract_key": "US.TEST|2026-09-18|100|C",
            "score": 72,
            "grade": "B",
            "final_direction": "BULL",
            "components": {"consensus": 88, "market_quality": 70},
            "votes": [{"analyst": "pa", "decision": "TRADE", "direction": "BULL"}],
            "risk_flags": [],
            "execution": {
                "bid": 2.0,
                "ask": 2.1,
                "max_entry_price": 2.2,
                "take_profit": 2.9,
                "stop_loss": 1.6,
            },
        }
        view = OptionsRadarService._recommendation_view(payload)
        self.assertEqual(view["bid"], 2.0)
        self.assertTrue(view["data_complete"])
        self.assertEqual(view["execution_status"], "可执行")
        self.assertIn("分析师判断", view["reason"])
        self.assertIn("共识", view["reason"])

    def test_recommendation_view_marks_missing_execution_fields(self):
        view = OptionsRadarService._recommendation_view({
            "contract_key": "US.TEST|2026-09-18|100|C",
            "score": 55,
            "votes": [],
            "execution": {"last": 2.0},
        })
        self.assertFalse(view["data_complete"])
        self.assertEqual(view["execution_status"], "待行情/字段未就绪")
        self.assertIn("bid", view["reason"])

    def test_top5_push_includes_scores_below_sixty(self):
        service = object.__new__(OptionsRadarService)
        service.feishu = FakeFeishu()
        service._trade_date = lambda: datetime(2026, 8, 11).date()
        service.dashboard_recommendations = lambda _payload: {
            "sell": [
                {"contract_key": "US.A", "score": 59, "grade": "C", "direction": "BULL", "market_status": "eod", "reason": "测试理由A", "strategy_type": "sell"},
                {"contract_key": "US.B", "score": 42, "grade": "D", "direction": "BEAR", "market_status": "eod", "reason": "测试理由B", "strategy_type": "sell"},
            ],
            "buy": [],
        }
        self.assertEqual(service.publish_top5(), "queued")
        content = service.feishu.card["elements"][0]["content"]
        self.assertIn("US.A", content)
        self.assertIn("US.B", content)
        self.assertIn("测试理由A", content)
        self.assertIn("59", content)

    def test_gex_enrichment_only_for_latest_session(self):
        class FakeDatabase:
            def __init__(self, payloads):
                self.payloads = payloads

            def recommendations_for_date(self, session):
                return [{"payload_json": json.dumps(p)} for p in self.payloads]

            def flow_events_for_date(self, session):
                return []

        payloads = [
            {"contract_key": "US.AAPL|2026-09-18|200|C", "score": 60, "strategy_type": "sell"},
            {"contract_key": "US.NVDA|2026-09-18|200|P", "score": 55, "strategy_type": "buy"},
        ]
        service = object.__new__(OptionsRadarService)
        service.database = FakeDatabase(payloads)
        enriched = []

        def fake_enrich(view):
            enriched.append(view.get("contract_key"))

        service._enrich_view_gex = fake_enrich
        # Latest session: enrichment runs.
        service._dashboard_date = lambda: date(2026, 8, 20)
        service.dashboard_recommendations({"date": "2026-08-20"})
        self.assertEqual(sorted(enriched), [
            "US.AAPL|2026-09-18|200|C", "US.NVDA|2026-09-18|200|P",
        ])
        # Historical session: no live enrichment (instant, stored-only view).
        enriched.clear()
        service.dashboard_recommendations({"date": "2026-08-11"})
        self.assertEqual(enriched, [])

    def test_portfolio_view_uses_cache_without_refreshing_ibkr(self):
        class FakeDatabase:
            def __init__(self):
                self.rows = {}
                self.writes = []

            def all_instrument_metadata(self):
                return dict(self.rows)

            def instrument_metadata(self, symbol):
                return self.rows.get(symbol)

            def save_instrument_metadata(self, symbol, **kwargs):
                self.writes.append((symbol, kwargs))

        service = object.__new__(OptionsRadarService)
        service._portfolio = {
            "AAPL": PortfolioContext(
                symbol="AAPL", held_quantity=1, snapshot_at=datetime(2026, 8, 12),
            )
        }
        service.database = FakeDatabase()
        service._stock_meta = {"AAPL": {"name": "Apple Inc."}}
        service._refresh_stock_meta = lambda: (_ for _ in ()).throw(AssertionError("must not refresh during page render"))
        result = service.dashboard_portfolio()
        self.assertEqual(result["AAPL"]["company_name"], "Apple Inc.")
        self.assertEqual(result["AAPL"]["held_quantity"], 1)

    def test_instrument_metadata_persists_and_merges(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "radar.db")
            database.save_instrument_metadata(
                "AAPL", name_en="Apple Inc.", name_zh="苹果公司",
                industry="Technology", current_price=200.0, change_pct=0.01, source="manual_refresh",
            )
            stored = database.instrument_metadata("AAPL")
            self.assertEqual(stored["name_zh"], "苹果公司")
            self.assertEqual(stored["industry"], "Technology")
            all_rows = database.all_instrument_metadata()
            self.assertIn("AAPL", all_rows)
            # COALESCE keeps existing values when new call omits them
            database.save_instrument_metadata("AAPL", name_en="Apple Inc.", current_price=205.0, source="manual_refresh")
            refreshed = database.instrument_metadata("AAPL")
            self.assertEqual(refreshed["name_zh"], "苹果公司")
            self.assertEqual(refreshed["current_price"], 205.0)


if __name__ == "__main__":
    unittest.main()
