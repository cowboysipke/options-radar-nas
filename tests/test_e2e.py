import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from options_radar.e2e_fixture import build_fixture_messages
from options_radar.futu_provider import (
    FutuHealth,
    FutuHistoryBar,
    FutuMarketSnapshot,
    FutuOptionContract,
    FutuPortfolioSnapshot,
    FutuPosition,
    FutuQuoteRights,
    SubscriptionResult,
    WatchlistMutationResult,
    WatchlistSnapshot,
)
from options_radar.service import OptionsRadarService


class FakeDiscord:
    channel_urls = {}

    def health(self):
        return {"status": "ready"}

    def close(self):
        return None


class FakeFutu:
    def __init__(self):
        self.expiry = date(2026, 9, 4)
        self.contract = FutuOptionContract(
            "US.FCX260904P00069000", "US.FCX|2026-09-04|69|P", "FCX", self.expiry, 69.0, "P",
        )

    def sync_watchlists(self):
        return WatchlistSnapshot(datetime.now(timezone.utc), {"Options Radar": ["US.FCX"]}, ["FCX"])

    def sync_positions(self):
        return FutuPortfolioSnapshot(
            datetime.now(timezone.utc),
            [FutuPosition("US.FCX", "FCX", 0, 0, 0, 0)], 0, nav=100000, cash=80000,
        )

    def get_option_chain(self, symbol, start, end, option_type=None):
        return [self.contract] if symbol.upper() == "FCX" else []

    def get_snapshots(self, codes):
        now = datetime.now(timezone.utc)
        return {
            code: FutuMarketSnapshot(
                code, now, now, 2.00, 2.10, 2.05, 900, 1500, 0.42, -0.45,
                0.05, 0.10, -0.06, 0.02, 60.0, "REALTIME",
                {"bid": "native", "ask": "native", "open_interest": "native"},
            ) for code in codes
        }

    def subscribe_candidates(self, codes):
        return SubscriptionResult(list(codes), list(codes), [], "ready")

    def get_history(self, code, start, end, interval="K_5M"):
        return []

    def health(self):
        return FutuHealth("ready", True, True, True, "10.9", "OPEN", datetime.now(timezone.utc))

    def quote_rights(self):
        return FutuQuoteRights("ready", True, remaining=50)

    def close(self):
        return None


class EndToEndTests(unittest.TestCase):
    def _service(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / "config.yaml"
        config.write_text(yaml.safe_dump({
            "database_path": str(root / "radar.db"),
            "discord": {"channel_names": {}},
            "futu": {"max_quote_age_seconds": 300},
            "providers": {
                "market_priority": ["futu", "ibkr", "massive"],
                "enabled": {"futu": True, "ibkr": False, "massive": False},
            },
            "scoring": {
                "recommendation_threshold": 65, "disagreement_threshold": 0.25,
                "min_dte": 14, "max_dte": 60, "min_abs_delta": 0.30,
                "max_abs_delta": 0.65, "max_spread_pct": 0.12, "min_open_interest": 100,
            },
            "paper": {"starting_cash": 100000, "risk_per_trade": 0.01},
            "backtest": {"use_synthetic_when_unavailable": True},
        }, allow_unicode=True), encoding="utf-8")
        return OptionsRadarService(str(config), str(root), futu_provider=FakeFutu(), discord_source=FakeDiscord())

    def tearDown(self):
        if hasattr(self, "temp"):
            self.temp.cleanup()

    def test_full_loop_fixture_to_recommendation_to_outcome(self):
        service = self._service()
        try:
            target = date(2026, 8, 5)
            service._ingest(build_fixture_messages(target))
            results = service._evaluate(target)
            self.assertEqual(len(results), 1)
            top = results[0]
            self.assertEqual(top["contract_key"], "US.FCX|2026-09-04|69|P")
            self.assertTrue(top["eligible"])
            self.assertEqual(top["market_status"], "ok")

            event_key = service.database.flow_events_for_date(target)[0].event_key
            signals = service.database.signals_for_event(event_key)
            self.assertEqual(len(signals), 4)
            self.assertTrue(all(item.dte == 30 for item in signals))

            settlement = service.backtests.replay(target, target + timedelta(days=9))
            self.assertGreaterEqual(settlement["saved"], 1)
            outcomes = service.database.signal_outcomes()
            self.assertTrue(outcomes)
            self.assertIn(outcomes[0].status, {"filled", "no-fill"})
        finally:
            service.stop()


if __name__ == "__main__":
    unittest.main()
