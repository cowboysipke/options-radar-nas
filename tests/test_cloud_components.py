import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from options_radar.db import Database
from options_radar.discord_source import DiscordBrowserSource, FixtureDiscordSource
from options_radar.ibkr_flex import IBKRFlexClient
from options_radar.massive_client import MassiveClient
from options_radar.models import (
    AnalystVote, BrokerSnapshot, ConsensusEvaluation, MarketSnapshot,
    PortfolioContext, SignalOutcome, SourceCursor, SourceMessage, StrategyVersion,
)
from options_radar.optimizer import OptionBar, promotion_gate, simulate_long_option
from options_radar.paper import build_candidate, select_top_candidates


class BrokerMarketTests(unittest.TestCase):
    def test_ibkr_flex_xml_discards_account_identity(self):
        xml = """<FlexQueryResponse><FlexStatements count='1'><FlexStatement accountId='U1234567' toDate='20260810'>
        <ChangeInNAV endingValue='123456.78'/><CashReport><CashReportCurrency currency='USD' endingCash='23456.7'/></CashReport>
        <OpenPositions><OpenPosition assetCategory='STK' symbol='TSLA' position='10' positionValue='2500' costBasisMoney='2100'/></OpenPositions>
        </FlexStatement></FlexStatements></FlexQueryResponse>"""
        snapshot = IBKRFlexClient.parse_statement(xml)
        self.assertEqual(snapshot.nav, 123456.78)
        self.assertEqual(snapshot.positions["TSLA"]["quantity"], 10)
        self.assertNotIn("U1234567", str(snapshot))

    def test_massive_occ_and_eod_quality(self):
        calls = []

        def fake_get(url, timeout):
            calls.append(url)
            return {"results": [{"t": 1786320000000, "c": 2.5, "v": 120}]}

        client = MassiveClient(api_key="key", http_get=fake_get, requests_per_minute=99)
        key = "US.SAP|2026-08-21|212.5|C"
        self.assertEqual(client.occ_ticker(key), "O:SAP260821C00212500")
        snapshot = client.exact_snapshot(key, date(2026, 8, 10))
        self.assertEqual(snapshot.data_status, "eod")
        self.assertEqual(snapshot.field_quality["bid"], "missing")
        self.assertEqual(snapshot.last, 2.5)


class DiscordAndStorageTests(unittest.TestCase):
    def test_source_cursor_and_dom_normalization(self):
        item = DiscordBrowserSource.normalize_dom_message("pa", {
            "id": "chat-messages-123456789012345678",
            "timestamp": "2026-08-10T12:00:00+00:00", "author": "AlphaAgent",
            "text": "SAP 2026-08-21 212.5C", "embeds": [],
        })
        source = FixtureDiscordSource([item])
        self.assertEqual(len(source.fetch_since("pa", None)), 1)
        cursor = SourceCursor("pa", item.message_id, item.created_at)
        self.assertEqual(source.fetch_since("pa", cursor), [])

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "radar.db")
            database.save_source_cursor(cursor)
            self.assertEqual(database.get_source_cursor("pa").last_message_id, item.message_id)
            database.upsert_watchlist("tsla", source="test")
            self.assertEqual(database.list_watchlist()[0].symbol, "TSLA")
            database.remove_watchlist("TSLA")
            self.assertEqual(database.list_watchlist(), [])

    def test_broker_outcome_and_strategy_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "radar.db")
            broker = BrokerSnapshot(datetime(2026, 8, 10), 100000, 25000, {"TSLA": {"quantity": 5.0}})
            database.save_broker_snapshot(broker)
            self.assertEqual(database.latest_broker_snapshot().nav, 100000)
            strategy = StrategyVersion("v1", "champion", {"threshold": 65}, {"avg_net_return": 0.02})
            database.save_strategy_version(strategy)
            self.assertEqual(database.strategy_versions()[0].version, "v1")


def evaluation(key="US.TEST|2026-10-16|100|C", score=79):
    return ConsensusEvaluation(
        contract_key=key, evaluated_at=datetime(2026, 8, 10), final_direction="BULL",
        score=score, grade="B", disagreement=False, consensus_strength=1.0,
        components={}, votes=[
            AnalystVote("pa", "price", "TRADE", "BULL", 0.9, 1.0, ["breakout"], underlying_entry=101),
            AnalystVote("fpd", "flow", "TRADE", "BULL", 0.8, 1.0, ["flow"], underlying_entry=103),
        ], eligible=True,
    )


class RecommendationBacktestTests(unittest.TestCase):
    def test_candidate_uses_family_median_limit_and_stale_nav_rule(self):
        market = MarketSnapshot(
            contract_key="US.TEST|2026-10-16|100|C", observed_at=datetime(2026, 8, 9),
            last=2.0, volume=100, data_status="eod", provider="massive",
        )
        portfolio = PortfolioContext(
            "TEST", in_watchlist=True, nav=100000,
            snapshot_at=datetime(2026, 8, 5),
        )
        candidate = build_candidate(evaluation(), market, portfolio, {
            "risk_per_trade": 0.01, "take_profit_pct": 0.35, "stop_loss_pct": 0.25,
            "max_holding_business_days": 5, "exit_before_expiry_days": 3,
        })
        self.assertEqual(candidate.max_entry_price, 2.1)
        self.assertIn(candidate.underlying_entry, {101.0, 103.0})
        self.assertEqual(candidate.quantity, 0)
        self.assertEqual(candidate.quantity_status, "portfolio_stale")
        self.assertTrue(candidate.strategy.startswith("PENDING_TRIGGER"))

    def test_unique_symbol_top_three(self):
        market1 = MarketSnapshot("US.TEST|2026-10-16|100|C", datetime.now(), last=2, data_status="eod")
        market2 = MarketSnapshot("US.TEST|2026-11-20|105|C", datetime.now(), last=2, data_status="eod")
        market3 = MarketSnapshot("US.OTHER|2026-11-20|10|C", datetime.now(), last=1, data_status="eod")
        portfolio = PortfolioContext("TEST", in_watchlist=True)
        candidates = [
            build_candidate(evaluation(market1.contract_key, 79), market1, portfolio, {}),
            build_candidate(evaluation(market2.contract_key, 75), market2, portfolio, {}),
            build_candidate(evaluation(market3.contract_key, 70), market3, portfolio, {}),
        ]
        self.assertEqual(len(select_top_candidates(candidates)), 2)

    def test_backtest_same_bar_stop_first_and_promotion_gate(self):
        bars = [OptionBar(datetime(2026, 8, 11, 14, 35), 1.0, 1.5, 0.6, 1.2)]
        result = simulate_long_option(bars)
        self.assertEqual(result.exit_reason, "stop-loss")
        passed, reason = promotion_gate(
            {"validation_avg_net_return": 0.01, "validation_max_drawdown": -0.10},
            {"validation_avg_net_return": 0.016, "validation_max_drawdown": -0.11,
             "validation_coverage": 0.9, "validation_samples": 40},
            100, {"price": 20, "flow": 20}, 20,
        )
        self.assertTrue(passed, reason)


if __name__ == "__main__":
    unittest.main()
