import unittest
from datetime import date, datetime, timezone

from options_radar.backup_providers import (
    AlpacaProvider,
    MarketDataAppProvider,
    ProviderRateLimitError,
    TradierProvider,
)


CONTRACT = "US.AAPL|2026-01-16|200|C"
OCC = "AAPL260116C00200000"


class Transport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, method, url, *, headers, params, timeout):
        self.calls.append((method, url, dict(headers), dict(params), timeout))
        for suffix, payload in self.responses.items():
            if url.endswith(suffix):
                return payload
        raise AssertionError(f"unexpected URL: {url}")


# Kept unittest-compatible so CI runs the module via ``unittest discover``
# where pytest is not installed. ``subTest`` replaces parametrize.
class BackupProviderTests(unittest.TestCase):
    def test_alpaca_normalises_contract_snapshot_and_marks_indicative(self):
        transport = Transport({
            "/v2/options/contracts": {"option_contracts": [{"symbol": OCC}]},
            "/v1beta1/options/snapshots/AAPL": {"snapshots": {OCC: {
                "latestQuote": {"bp": 4.1, "ap": 4.3, "t": "2026-01-02T15:00:00Z"},
                "latestTrade": {"p": 4.2}, "dailyBar": {"v": 31},
                "impliedVolatility": 0.27, "greeks": {"delta": 0.51},
            }}},
        })
        provider = AlpacaProvider("key", "secret", transport=transport)

        contracts = provider.get_option_chain("aapl", date(2026, 1, 16))
        self.assertEqual(contracts[0].contract_key, CONTRACT)
        self.assertEqual(contracts[0].provider, "alpaca")
        snapshot = provider.get_snapshots([CONTRACT])[CONTRACT]
        self.assertEqual(snapshot.provider, "alpaca")
        self.assertEqual(snapshot.fields["bid"].value, 4.1)
        self.assertEqual(snapshot.fields["ask"].quality, "indicative")
        self.assertEqual(snapshot.status, "indicative")
        self.assertTrue(provider.capabilities().indicative)
        self.assertTrue(all("APCA-API-SECRET-KEY" in call[2] for call in transport.calls))

    def test_alpaca_history_is_indicative_and_cached(self):
        transport = Transport({
            "/v1beta1/options/bars": {"bars": {OCC: [
                {"t": "2026-01-02T15:00:00Z", "o": 4, "h": 5, "l": 3, "c": 4.5, "v": 9}
            ]}},
        })
        provider = AlpacaProvider("key", "secret", transport=transport, cache_seconds=300)
        args = (CONTRACT, datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 3, tzinfo=timezone.utc), "1Day")
        first = provider.get_history(*args)
        second = provider.get_history(*args)
        self.assertEqual(first, second)
        self.assertEqual(first[0].quality, "indicative")
        self.assertEqual(first[0].provider, "alpaca")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(provider.cache_status()["hits"], 1)

    def test_alpaca_feed_quality_is_configurable(self):
        indicative = AlpacaProvider("key", "secret", feed="indicative")
        opra = AlpacaProvider("key", "secret", feed="opra")
        self.assertEqual(indicative.quality, "indicative")
        self.assertEqual(opra.quality, "realtime")

    def test_marketdata_chain_quote_and_columnar_history_are_delayed(self):
        transport = Transport({
            "/options/chain/AAPL/": {"optionSymbol": [OCC]},
            f"/options/quotes/{OCC}/": {
                "bid": [4.1], "ask": [4.4], "last": [4.2], "openInterest": [123],
                "updated": [1767366000], "iv": [0.3],
            },
            f"/options/candles/daily/{OCC}/": {
                "t": [1767366000], "o": [4], "h": [5], "l": [3], "c": [4.5], "v": [20],
            },
        })
        provider = MarketDataAppProvider("token", transport=transport)
        self.assertEqual(provider.get_option_chain("AAPL")[0].provider, "marketdata_app")
        snapshot = provider.get_snapshots([CONTRACT])[CONTRACT]
        self.assertEqual(snapshot.fields["open_interest"].value, 123)
        self.assertEqual(snapshot.fields["bid"].quality, "delayed")
        bars = provider.get_history(CONTRACT, date(2026, 1, 1), date(2026, 1, 3), "daily")
        self.assertEqual(bars[0].close, 4.5)
        self.assertEqual(bars[0].quality, "delayed")
        self.assertTrue(all(call[2]["Authorization"] == "Bearer token" for call in transport.calls))

    def test_tradier_live_and_sandbox_quality(self):
        for sandbox, expected in ((True, "delayed"), (False, "realtime")):
            with self.subTest(sandbox=sandbox, expected=expected):
                transport = Transport({
                    "/markets/quotes": {"quotes": {"quote": {
                        "symbol": OCC, "bid": 4.1, "ask": 4.2, "last": 4.15,
                        "trade_date": 1767366000000, "open_interest": 99,
                        "greeks": {"mid_iv": 0.25, "delta": 0.5},
                    }}},
                })
                provider = TradierProvider("token", sandbox=sandbox, transport=transport)
                snapshot = provider.get_snapshots([CONTRACT])[CONTRACT]
                self.assertEqual(snapshot.fields["bid"].quality, expected)
                self.assertEqual(provider.capabilities().realtime, (not sandbox))
                self.assertEqual(provider.capabilities().delayed, sandbox)

    def test_tradier_chain_and_history_normalisation(self):
        transport = Transport({
            "/markets/options/chains": {"options": {"option": {"symbol": OCC}}},
            "/markets/history": {"history": {"day": {"date": "2026-01-02", "open": 4, "high": 5, "low": 3, "close": 4.5, "volume": 10}}},
        })
        provider = TradierProvider("token", sandbox=True, transport=transport)
        chain = provider.get_option_chain("AAPL", date(2026, 1, 16))
        self.assertEqual(chain[0].contract_key, CONTRACT)
        self.assertEqual(chain[0].provider, "tradier")
        bars = provider.get_history(CONTRACT, date(2026, 1, 1), date(2026, 1, 3), "daily")
        self.assertEqual(bars[0].instrument_key, CONTRACT)
        self.assertEqual(bars[0].quality, "delayed")

    def test_health_reports_configuration_and_quota_without_leaking_secrets(self):
        missing = AlpacaProvider().health()
        self.assertFalse(missing.configured)
        self.assertEqual(missing.status, "missing")

        transport = Transport({"/stocks/quotes/SPY/": {"s": "ok"}})
        ready = MarketDataAppProvider("top-secret", transport=transport).health()
        self.assertTrue(ready.connected)
        self.assertEqual(ready.requests_used, 1)
        self.assertNotIn("top-secret", repr(ready.to_dict()))

    def test_quota_is_enforced_but_cache_hits_do_not_spend_it(self):
        transport = Transport({"/stocks/quotes/SPY/": {"s": "ok"}})
        provider = MarketDataAppProvider("token", transport=transport, quota=1, cache_seconds=300)
        provider._get("/stocks/quotes/SPY/")
        provider._get("/stocks/quotes/SPY/")
        self.assertEqual(provider.quota_status()["used"], 1)
        with self.assertRaises(ProviderRateLimitError):
            provider._get("/stocks/quotes/QQQ/")

    def test_backup_clients_expose_no_trading_methods(self):
        for provider in (AlpacaProvider(), MarketDataAppProvider(), TradierProvider()):
            for name in ("place_order", "submit_order", "cancel_order", "exercise_option"):
                self.assertFalse(hasattr(provider, name))


if __name__ == "__main__":
    unittest.main()