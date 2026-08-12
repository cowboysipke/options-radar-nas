import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace

from options_radar.ibkr_provider import DEFAULT_PORTS, IBKRProvider


NOW = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)


class FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self):
        self.connected = False
        self.connect_kwargs = {}
        self.market_type = None
        self.history_calls = []

    def connect(self, host, port, **kwargs):
        self.connected = True
        self.connect_kwargs = {"host": host, "port": port, **kwargs}

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def managedAccounts(self):
        return ["U12345678"]

    def accountValues(self):
        return [
            SimpleNamespace(account="U12345678", tag="NetLiquidation", value="120000", currency="USD"),
            SimpleNamespace(account="U12345678", tag="TotalCashValue", value="20000", currency="USD"),
        ]

    def positions(self):
        contract = SimpleNamespace(symbol="AAPL", secType="STK", currency="USD")
        return [SimpleNamespace(account="U12345678", contract=contract, position=5, avgCost=180)]

    def makeStock(self, symbol, exchange, currency):
        return SimpleNamespace(symbol=symbol, secType="STK", exchange=exchange, currency=currency, conId=265598)

    def makeOption(self, symbol, expiry, strike, right, exchange, currency, multiplier):
        return SimpleNamespace(symbol=symbol, secType="OPT", lastTradeDateOrContractMonth=expiry,
                               strike=strike, right=right, exchange=exchange, currency=currency,
                               multiplier=str(multiplier))

    def qualifyContracts(self, *contracts):
        return contracts

    def reqSecDefOptParams(self, symbol, exchange, sec_type, con_id):
        return [SimpleNamespace(exchange="SMART", expirations={"20260918"}, strikes={195, 200},
                                multiplier="100", tradingClass="AAPL")]

    def reqTickers(self, *contracts):
        greeks = SimpleNamespace(impliedVol=.31, delta=.52, gamma=.03, vega=.11, theta=-.07, undPrice=201)
        return [SimpleNamespace(contract=c, bid=2.1, ask=2.3, last=2.2, volume=100,
                                callOpenInterest=500, putOpenInterest=400, modelGreeks=greeks,
                                time=NOW) for c in contracts]

    def reqMarketDataType(self, value):
        self.market_type = value

    def reqHistoricalData(self, contract, **kwargs):
        self.history_calls.append((contract, kwargs))
        return [SimpleNamespace(date=NOW, open=2, high=2.4, low=1.9, close=2.2, volume=50)]


class IBKRProviderTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.socket_calls = []

        def socket_factory(address, timeout):
            self.socket_calls.append((address, timeout))
            if address[1] in (7497, 4002):
                return FakeSocket()
            raise OSError("closed")

        self.provider = IBKRProvider(backend=self.backend, socket_factory=socket_factory, now=lambda: NOW)

    def test_discovers_all_standard_ports_in_priority_order(self):
        endpoints = self.provider.discover()
        self.assertEqual([item.port for item in endpoints], [7497, 4002])
        self.assertEqual([address[1] for address, _ in self.socket_calls], list(DEFAULT_PORTS))

    def test_health_connects_readonly_and_capabilities_have_no_trading(self):
        health = self.provider.health()
        self.assertTrue(health.connected)
        self.assertEqual(self.backend.connect_kwargs["port"], 7497)
        self.assertTrue(self.backend.connect_kwargs["readonly"])
        capabilities = self.provider.capabilities()
        self.assertTrue(capabilities.option_chain)
        self.assertTrue(health.details["readonly"])
        self.assertFalse(hasattr(self.provider, "place_order"))
        self.assertFalse(hasattr(self.provider, "exercise_option"))

    def test_positions_nav_are_provenanced_and_account_is_masked(self):
        snapshot = self.provider.sync_positions()
        self.assertEqual(snapshot.nav, 120000)
        self.assertEqual(snapshot.provider, "ibkr")
        self.assertEqual(snapshot.positions["US.AAPL"]["quantity"], 5)
        self.assertNotIn("U12345678", repr(snapshot))
        self.assertEqual(self.provider.get_nav(), 120000)

    def test_chain_uses_security_definition_and_snapshots_keep_quote_pair(self):
        chain = self.provider.get_option_chain("aapl", date(2026, 9, 1), date(2026, 9, 30), "call")
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].contract_key, "US.AAPL|2026-09-18|195|C")
        snapshots = self.provider.get_snapshots([chain[0]])
        item = snapshots[chain[0].contract_key]
        self.assertEqual(item.midpoint, 2.2)
        self.assertEqual(item.bid.quality, "realtime")
        self.assertEqual(item.bid.market_timestamp, item.ask.market_timestamp)

    def test_delayed_type_marks_all_snapshot_fields(self):
        self.provider.set_market_data_type(3)
        contract = self.provider.get_option_chain("AAPL", option_type="P")[0]
        item = self.provider.get_snapshots([contract])[contract.contract_key]
        self.assertEqual(self.backend.market_type, 3)
        self.assertEqual(item.bid.quality, "delayed")

    def test_history_and_underlying_bars_use_read_api(self):
        contract = self.provider.get_option_chain("AAPL", option_type="C")[0]
        bars = self.provider.get_history(contract, date(2026, 8, 10), date(2026, 8, 12))
        underlying = self.provider.get_underlying_bars("AAPL", date(2026, 8, 10), date(2026, 8, 12))
        self.assertEqual(bars[0].close, 2.2)
        self.assertEqual(underlying[0].instrument_key, "US.AAPL")
        self.assertEqual(self.backend.history_calls[0][1]["durationStr"], "3 D")

    def test_missing_optional_dependency_degrades_health(self):
        provider = IBKRProvider(backend_factory=lambda: (_ for _ in ()).throw(RuntimeError("optional dependency ib_insync is not installed")), now=lambda: NOW)
        health = provider.health()
        self.assertEqual(health.status, "missing_dependency")
        self.assertFalse(health.connected)


if __name__ == "__main__":
    unittest.main()
