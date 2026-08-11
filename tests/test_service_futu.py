from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from options_radar.futu_provider import (
    FutuHealth, FutuHistoryBar, FutuMarketSnapshot, FutuOptionContract,
    FutuPortfolioSnapshot, FutuPosition, FutuQuoteRights, SubscriptionResult,
    WatchlistMutationResult, WatchlistSnapshot,
)
from options_radar.models import FlowEvent, ParsedSignal
from options_radar.service import FutuHistoryAdapter, OptionsRadarService


class FakeDiscord:
    channel_urls = {}

    def health(self):
        return {"status": "ready"}

    def close(self):
        return None


class FakeFutu:
    def __init__(self):
        self.expiry = date.today() + timedelta(days=30)
        self.contract = FutuOptionContract(
            "US.AAPL260101C00200000", f"US.AAPL|{self.expiry.isoformat()}|200|C",
            "AAPL", self.expiry, 200.0, "C",
        )
        self.subscribed = []

    def sync_watchlists(self):
        now = datetime.now(timezone.utc)
        return WatchlistSnapshot(now, {"Options Radar": ["US.AAPL"]}, ["AAPL"])

    def sync_positions(self):
        now = datetime.now(timezone.utc)
        return FutuPortfolioSnapshot(
            now, [FutuPosition("US.AAPL", "AAPL", 10, 2000, 180, 200)],
            2000, nav=100000, cash=80000,
        )

    def get_option_chain(self, symbol, start, end, option_type=None):
        return [self.contract] if symbol.upper() == "AAPL" else []

    def subscribe_candidates(self, codes):
        self.subscribed = list(codes)
        return SubscriptionResult(self.subscribed, self.subscribed, [], "ready")

    def get_snapshots(self, codes):
        now = datetime.now(timezone.utc)
        return {
            code: FutuMarketSnapshot(
                code, now, now, 4.90, 5.00, 4.95, 600, 1200, 0.45, 0.50,
                0.05, 0.10, -0.06, 0.02, 201.0, "REALTIME",
                {"bid": "native", "ask": "native", "open_interest": "native"},
            ) for code in codes
        }

    def get_history(self, code, start, end, interval="K_5M"):
        now = datetime.now(timezone.utc)
        if interval == "K_DAY":
            return [
                FutuHistoryBar(code, now - timedelta(days=offset), 198, 202, 197, 201, 1_000_000, 1, interval)
                for offset in range(20, 0, -1)
            ]
        return [FutuHistoryBar(code, now, 4.8, 5.2, 4.7, 5.0, 100, 1, interval)]

    def health(self):
        return FutuHealth("ready", True, True, True, "10.9", "OPEN", datetime.now(timezone.utc))

    def quote_rights(self):
        return FutuQuoteRights("ready", True, remaining=50)

    def add_watchlist(self, symbol, group="Options Radar"):
        return WatchlistMutationResult("ready", "add", f"US.{symbol.upper()}", group, True)

    def remove_watchlist(self, symbol, group="Options Radar"):
        return WatchlistMutationResult("ready", "remove", f"US.{symbol.upper()}", group, True)

    def close(self):
        return None


class FutuServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = root / "config.yaml"
        self.config.write_text(yaml.safe_dump({
            "database_path": str(root / "radar.db"),
            "discord": {"channel_names": {}},
            "futu": {"max_quote_age_seconds": 300},
            "scoring": {
                "recommendation_threshold": 65, "disagreement_threshold": 0.25,
                "min_dte": 14, "max_dte": 60, "min_abs_delta": 0.30,
                "max_abs_delta": 0.65, "max_spread_pct": 0.12,
                "min_open_interest": 100,
            },
            "paper": {"starting_cash": 100000, "risk_per_trade": 0.01},
        }, allow_unicode=True), encoding="utf-8")
        self.futu = FakeFutu()
        self.service = OptionsRadarService(
            str(self.config), str(root), futu_provider=self.futu, discord_source=FakeDiscord()
        )

    def tearDown(self):
        self.service.stop()
        self.temp.cleanup()

    def test_syncs_futu_portfolio_and_watchlist_with_nav(self):
        snapshot = self.service.sync_broker()
        self.assertEqual(snapshot.source, "futu_opend")
        self.assertEqual(snapshot.nav, 100000)
        self.assertTrue(self.service._portfolio["AAPL"].in_watchlist)
        self.assertEqual(self.service._portfolio["AAPL"].held_quantity, 10)

    def test_exact_contract_is_enriched_and_deterministically_scored(self):
        now = datetime.utcnow()
        event = FlowEvent(
            "event-1", self.futu.contract.contract_key, "AAPL", self.futu.expiry,
            200, "C", 250000, 4.5, 30, now, date.today(),
        )
        self.service.database.insert_flow_event(event)
        for analyst, family in (("pa", "price_action"), ("fqd", "flow_positioning")):
            self.service.database.insert_signal(ParsedSignal(
                "event-1", event.contract_key, "AAPL", self.futu.expiry, 200, "C",
                "TRADE", "BULL", "explicit", 0.95, "95%", family, analyst,
                analyst, now, ["趋势与资金方向一致"], dte=30, completeness=1.0,
            ))
        results = self.service._evaluate(date.today())
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["eligible"])
        self.assertEqual(results[0]["market_status"], "ok")
        self.assertEqual(results[0]["open_interest"], 1200)
        self.assertEqual(self.futu.subscribed, [self.futu.contract.code])

    def test_history_adapter_uses_futu_five_minute_bars(self):
        adapter = FutuHistoryAdapter(self.futu)
        bars = adapter.aggregate_bars(self.futu.contract.contract_key, date.today(), date.today())
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["c"], 5.0)


if __name__ == "__main__":
    unittest.main()
