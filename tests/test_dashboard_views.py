import json
import unittest
from datetime import datetime

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
        service.dashboard_recommendations = lambda _payload: [
            {"contract_key": "US.A", "score": 59, "grade": "C", "direction": "BULL", "market_status": "eod", "reason": "测试理由A"},
            {"contract_key": "US.B", "score": 42, "grade": "D", "direction": "BEAR", "market_status": "eod", "reason": "测试理由B"},
        ]
        self.assertEqual(service.publish_top5(), "queued")
        content = service.feishu.card["elements"][0]["content"]
        self.assertIn("US.A", content)
        self.assertIn("US.B", content)
        self.assertIn("测试理由A", content)
        self.assertIn("59", content)

    def test_portfolio_view_uses_cache_without_refreshing_ibkr(self):
        service = object.__new__(OptionsRadarService)
        service._portfolio = {
            "AAPL": PortfolioContext(
                symbol="AAPL", held_quantity=1, snapshot_at=datetime(2026, 8, 12),
            )
        }
        service._stock_meta = {"AAPL": {"name": "Apple Inc."}}
        service._refresh_stock_meta = lambda: (_ for _ in ()).throw(AssertionError("must not refresh during page render"))
        result = service.dashboard_portfolio()
        self.assertEqual(result["AAPL"]["company_name"], "Apple Inc.")
        self.assertEqual(result["AAPL"]["held_quantity"], 1)


if __name__ == "__main__":
    unittest.main()
