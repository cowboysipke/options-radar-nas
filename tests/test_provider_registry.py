from datetime import datetime, timedelta
import unittest

from options_radar.provider_registry import ProviderRegistry, market_snapshot_from_composite
from options_radar.provider_types import ProviderCapability, ProviderHealth, ProviderMarketSnapshot, SourcedValue


NOW = datetime(2026, 8, 12, 15, 0)


def point(value, provider, quality="realtime", age=0):
    stamp = NOW - timedelta(seconds=age)
    return SourcedValue(value, provider, quality, stamp, NOW, age)


def snapshot(provider, bid=None, ask=None, quality="realtime", **fields):
    values = {name: point(value, provider, quality) for name, value in fields.items()}
    if bid is not None:
        values["bid"] = point(bid, provider, quality)
    if ask is not None:
        values["ask"] = point(ask, provider, quality)
    return ProviderMarketSnapshot("US.TEST|2026-09-18|100|C", provider, values, status=quality)


class ProviderRegistryTests(unittest.TestCase):
    def test_bid_ask_are_atomic_and_realtime_wins(self):
        registry = ProviderRegistry(priority=["futu", "ibkr", "alpaca"])
        value = registry.composite_snapshot(
            "US.TEST|2026-09-18|100|C",
            {
                "alpaca": snapshot("alpaca", 1.0, 1.3, "indicative", delta=.4),
                "ibkr": snapshot("ibkr", 1.1, 1.2, "realtime", delta=.42),
                "futu": snapshot("futu", bid=1.08, quality="realtime", delta=.41),
            }, now=NOW,
        )
        self.assertEqual(value.quote_provider, "ibkr")
        self.assertEqual(value.fields["bid"].provider, "ibkr")
        self.assertEqual(value.fields["ask"].provider, "ibkr")
        self.assertTrue(value.execution_allowed)
        self.assertEqual(value.fields["delta"].provider, "futu")

    def test_conflicting_realtime_quotes_disable_execution(self):
        registry = ProviderRegistry(priority=["futu", "ibkr"], conflict_threshold_pct=15)
        value = registry.composite_snapshot(
            "US.TEST|2026-09-18|100|C",
            {"futu": snapshot("futu", 1.0, 1.1), "ibkr": snapshot("ibkr", 1.5, 1.6)}, now=NOW,
        )
        self.assertFalse(value.execution_allowed)
        self.assertEqual(value.data_status, "conflict")
        self.assertTrue(value.conflicts)
        market = market_snapshot_from_composite(value)
        self.assertTrue(market.data_conflicts)

    def test_stale_realtime_does_not_become_execution_grade(self):
        old = point(1.0, "futu", "realtime", age=120)
        candidate = ProviderMarketSnapshot(
            "US.TEST|2026-09-18|100|C", "futu",
            {"bid": old, "ask": SourcedValue(1.1, "futu", "realtime", old.market_timestamp, NOW, 120)},
        )
        registry = ProviderRegistry(priority=["futu"], max_quote_age_seconds=60)
        value = registry.composite_snapshot(candidate.contract_key, {"futu": candidate}, now=NOW)
        self.assertEqual(value.data_status, "stale")
        self.assertFalse(value.execution_allowed)

    def test_status_and_enablement(self):
        class Fake:
            def health(self):
                return ProviderHealth("fake", True, True, "ready", quality="realtime")
            def capabilities(self):
                return ProviderCapability("fake", market=True, realtime=True)
        registry = ProviderRegistry({"fake": Fake()})
        self.assertTrue(registry.status("fake")["enabled"])
        self.assertFalse(registry.set_enabled("fake", False)["enabled"])


if __name__ == "__main__":
    unittest.main()
