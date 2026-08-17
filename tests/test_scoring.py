import unittest
from datetime import date, datetime

from options_radar.models import MarketSnapshot, ParsedSignal, PortfolioContext
from options_radar.scoring import calibrated_weight, evaluate_consensus


def signal(analyst, family, decision="TRADE", direction="BULL", confidence=0.8, minute=0):
    return ParsedSignal(
        flow_event_key="event", contract_key="US.TEST|2026-10-16|100|C", symbol="TEST",
        expiry=date(2026, 10, 16), strike=100, option_type="C", decision=decision,
        direction=direction, direction_source="explicit", confidence=confidence,
        confidence_raw="4", analyst_family=family, analyst=analyst, channel=analyst,
        observed_at=datetime(2026, 8, 6, 1, minute), rationale=["structure"], completeness=1.0,
    )


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.market = MarketSnapshot(
            contract_key="US.TEST|2026-10-16|100|C", observed_at=datetime(2026, 8, 6),
            bid=2.0, ask=2.1, last=2.05, volume=500, open_interest=1000,
            implied_volatility=0.45, delta=0.5, data_status="ok",
        )
        self.portfolio = PortfolioContext(symbol="TEST", in_watchlist=True)

    def test_one_family_is_capped_below_recommendation(self):
        result = evaluate_consensus([signal("pa", "price_action")], market=self.market,
                                    portfolio=self.portfolio, now=datetime(2026, 8, 6, 2))
        self.assertLessEqual(result.score, 64)
        self.assertEqual(result.grade, "C")

    def test_mr_and_qmr_count_as_one_family(self):
        result = evaluate_consensus([
            signal("mr", "mean_reversion"), signal("qmr", "mean_reversion", minute=1)
        ], market=self.market, portfolio=self.portfolio, now=datetime(2026, 8, 6, 2))
        self.assertLessEqual(result.score, 64)

    def test_two_independent_families_can_reach_a(self):
        result = evaluate_consensus([
            signal("pa", "price_action", confidence=1.0),
            signal("fpd", "flow_positioning", confidence=1.0, minute=1),
        ], analyst_weights={"pa": 1.5, "fpd": 1.5}, market=self.market,
           portfolio=self.portfolio, now=datetime(2026, 8, 6, 1, 5))
        self.assertGreaterEqual(result.score, 80)
        self.assertEqual(result.grade, "A")

    def test_duplicate_analyst_has_one_vote(self):
        result = evaluate_consensus([
            signal("pa", "price_action", direction="BEAR"),
            signal("pa", "price_action", direction="BULL", minute=1),
            signal("fpd", "flow_positioning", direction="BULL"),
        ], market=self.market, portfolio=self.portfolio, now=datetime(2026, 8, 6, 2))
        self.assertEqual(len(result.votes), 2)
        self.assertEqual(result.final_direction, "BULL")

    def test_weight_shrinkage(self):
        self.assertEqual(calibrated_weight(1, 0.5, 1, 0), 1.0)
        self.assertEqual(calibrated_weight(1, 0.5, 1, 20), 2.0)


if __name__ == "__main__":
    unittest.main()
