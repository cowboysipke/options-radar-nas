from datetime import date, datetime, timezone

import pytest

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


def test_alpaca_normalises_contract_snapshot_and_marks_indicative():
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
    assert contracts[0].contract_key == CONTRACT
    assert contracts[0].provider == "alpaca"
    snapshot = provider.get_snapshots([CONTRACT])[CONTRACT]
    assert snapshot.provider == "alpaca"
    assert snapshot.fields["bid"].value == 4.1
    assert snapshot.fields["ask"].quality == "indicative"
    assert snapshot.status == "indicative"
    assert provider.capabilities().indicative is True
    assert all("APCA-API-SECRET-KEY" in call[2] for call in transport.calls)


def test_alpaca_history_is_indicative_and_cached():
    transport = Transport({
        "/v1beta1/options/bars": {"bars": {OCC: [
            {"t": "2026-01-02T15:00:00Z", "o": 4, "h": 5, "l": 3, "c": 4.5, "v": 9}
        ]}},
    })
    provider = AlpacaProvider("key", "secret", transport=transport, cache_seconds=300)
    args = (CONTRACT, datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 3, tzinfo=timezone.utc), "1Day")
    first = provider.get_history(*args)
    second = provider.get_history(*args)
    assert first == second
    assert first[0].quality == "indicative"
    assert first[0].provider == "alpaca"
    assert len(transport.calls) == 1
    assert provider.cache_status()["hits"] == 1


def test_alpaca_feed_quality_is_configurable():
    indicative = AlpacaProvider("key", "secret", feed="indicative")
    opra = AlpacaProvider("key", "secret", feed="opra")
    assert indicative.quality == "indicative"
    assert opra.quality == "realtime"


def test_marketdata_chain_quote_and_columnar_history_are_delayed():
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
    assert provider.get_option_chain("AAPL")[0].provider == "marketdata_app"
    snapshot = provider.get_snapshots([CONTRACT])[CONTRACT]
    assert snapshot.fields["open_interest"].value == 123
    assert snapshot.fields["bid"].quality == "delayed"
    bars = provider.get_history(CONTRACT, date(2026, 1, 1), date(2026, 1, 3), "daily")
    assert bars[0].close == 4.5
    assert bars[0].quality == "delayed"
    assert all(call[2]["Authorization"] == "Bearer token" for call in transport.calls)


@pytest.mark.parametrize("sandbox,expected", [(True, "delayed"), (False, "realtime")])
def test_tradier_live_and_sandbox_quality(sandbox, expected):
    transport = Transport({
        "/markets/quotes": {"quotes": {"quote": {
            "symbol": OCC, "bid": 4.1, "ask": 4.2, "last": 4.15,
            "trade_date": 1767366000000, "open_interest": 99,
            "greeks": {"mid_iv": 0.25, "delta": 0.5},
        }}},
    })
    provider = TradierProvider("token", sandbox=sandbox, transport=transport)
    snapshot = provider.get_snapshots([CONTRACT])[CONTRACT]
    assert snapshot.fields["bid"].quality == expected
    assert provider.capabilities().realtime is (not sandbox)
    assert provider.capabilities().delayed is sandbox


def test_tradier_chain_and_history_normalisation():
    transport = Transport({
        "/markets/options/chains": {"options": {"option": {"symbol": OCC}}},
        "/markets/history": {"history": {"day": {"date": "2026-01-02", "open": 4, "high": 5, "low": 3, "close": 4.5, "volume": 10}}},
    })
    provider = TradierProvider("token", sandbox=True, transport=transport)
    chain = provider.get_option_chain("AAPL", date(2026, 1, 16))
    assert chain[0].contract_key == CONTRACT
    assert chain[0].provider == "tradier"
    bars = provider.get_history(CONTRACT, date(2026, 1, 1), date(2026, 1, 3), "daily")
    assert bars[0].instrument_key == CONTRACT
    assert bars[0].quality == "delayed"


def test_health_reports_configuration_and_quota_without_leaking_secrets():
    missing = AlpacaProvider().health()
    assert missing.configured is False
    assert missing.status == "missing"

    transport = Transport({"/stocks/quotes/SPY/": {"s": "ok"}})
    ready = MarketDataAppProvider("top-secret", transport=transport).health()
    assert ready.connected is True
    assert ready.requests_used == 1
    assert "top-secret" not in repr(ready.to_dict())


def test_quota_is_enforced_but_cache_hits_do_not_spend_it():
    transport = Transport({"/stocks/quotes/SPY/": {"s": "ok"}})
    provider = MarketDataAppProvider("token", transport=transport, quota=1, cache_seconds=300)
    provider._get("/stocks/quotes/SPY/")
    provider._get("/stocks/quotes/SPY/")
    assert provider.quota_status()["used"] == 1
    with pytest.raises(ProviderRateLimitError):
        provider._get("/stocks/quotes/QQQ/")


def test_backup_clients_expose_no_trading_methods():
    for provider in (AlpacaProvider(), MarketDataAppProvider(), TradierProvider()):
        for name in ("place_order", "submit_order", "cancel_order", "exercise_option"):
            assert not hasattr(provider, name)
