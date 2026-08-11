import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace

from options_radar.futu_provider import FutuProvider


NOW = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)


class FakeQuoteContext:
    def __init__(self):
        self.closed = False
        self.subscribed = []
        self.unsubscribed = []
        self.history_calls = []
        self.order_book_calls = []
        self.groups = {
            "科技": {"type": "CUSTOM", "codes": {"US.AAPL", "US.TSLA"}},
            "全部": {"type": "SYSTEM", "codes": {"US.AAPL"}},
        }
        self.watchlist_modifications = []

    def close(self):
        self.closed = True

    def get_global_state(self):
        return 0, {"qot_logined": True, "trd_logined": True, "program_status_type": "READY",
                   "server_ver": "10.9", "market_us": "OPEN"}

    def query_subscription(self, is_all_conn=True):
        return 0, {"total_used": 3, "own_used": 2, "remain": 497,
                   "own_security_firm": "FUTUINC", "sub_list": {"QUOTE": ["US.AAPL"]}}

    def get_user_security_group(self, group_type):
        return 0, [{"group_name": name, "group_type": data["type"]}
                   for name, data in self.groups.items()]

    def get_user_security(self, group_name):
        if group_name not in self.groups:
            return -1, "group not found"
        rows = [{"code": code} for code in self.groups[group_name]["codes"]]
        if group_name == "科技":
            rows.append({"code": "HK.00700"})
        return 0, rows

    def create_user_security_group(self, group_name):
        self.groups[group_name] = {"type": "CUSTOM", "codes": set()}
        return 0, "success"

    def modify_user_security(self, group_name, operation, codes):
        self.watchlist_modifications.append((group_name, operation, list(codes)))
        if operation == "ADD":
            self.groups[group_name]["codes"].update(codes)
        else:
            self.groups[group_name]["codes"].difference_update(codes)
        return 0, "success"

    def get_option_chain(self, **kwargs):
        return 0, [
            {"code": "US.AAPL260918C00200000", "strike_time": "2026-09-18", "strike_price": 200,
             "option_type": "CALL", "name": "AAPL 200 Call", "lot_size": 100},
            {"code": "US.AAPL260918P00200000", "strike_time": "2026-09-18", "strike_price": 200,
             "option_type": "PUT", "name": "AAPL 200 Put", "lot_size": 100},
        ]

    def get_market_snapshot(self, codes):
        return 0, [{"code": code, "bid_price": 2.1, "ask_price": 2.3, "last_price": 2.2,
                    "volume": 120, "open_interest": 500, "implied_volatility": 32.0,
                    "delta": 0.52, "gamma": 0.03, "vega": 0.12, "theta": -0.08,
                    "rho": 0.01, "option_owner_price": 201.5,
                    "update_time": "2026-08-11T08:00:00+00:00", "data_type": "REALTIME"}
                   for code in codes]

    def subscribe(self, codes, subtypes, is_first_push, subscribe_push):
        self.subscribed.append((codes, subtypes, is_first_push, subscribe_push))
        return 0, None

    def unsubscribe(self, codes, subtypes):
        self.unsubscribed.append((codes, subtypes))
        return 0, None

    def get_order_book(self, code, num=1):
        self.order_book_calls.append((code, num))
        return 0, {"Bid": [(2.05, 10, 1, {})], "Ask": [(2.35, 20, 1, {})],
                   "svr_recv_time_bid_timestamp": 1786435200}

    def request_history_kline(self, code, **kwargs):
        self.history_calls.append((code, kwargs))
        if kwargs["page_req_key"] is None:
            return 0, [{"code": code, "time_key": "2026-08-10T14:35:00+00:00",
                        "open": 2, "high": 2.4, "low": 1.9, "close": 2.2,
                        "volume": 40, "turnover": 8800}], b"next"
        return 0, [{"code": code, "time_key": "2026-08-10T14:40:00+00:00",
                    "open": 2.2, "high": 2.5, "low": 2.1, "close": 2.4,
                    "volume": 30, "turnover": 7200}], None


class FakeTradeContext:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.fund_calls = []

    def close(self):
        self.closed = True

    def get_acc_list(self):
        return 0, [
            {"acc_id": "111111", "trd_env": "SIMULATE", "trdmarket_auth": ["US"]},
            {"acc_id": "987654321", "trd_env": "REAL", "trdmarket_auth": ["US"],
             "acc_status": "ACTIVE", "card_num": "PRIVATE-CARD"},
        ]

    def accinfo_query(self, **kwargs):
        self.fund_calls.append(kwargs)
        return 0, [{"total_assets": 123456.78, "us_cash": 23456.70,
                    "acc_id": "987654321"}]

    def position_list_query(self, **kwargs):
        self.calls.append(kwargs)
        return 0, [
            {"code": "US.AAPL", "qty": 5, "market_val": 1000, "cost_price": 180,
             "nominal_price": 200, "stock_type": "STOCK", "currency": "USD",
             "acc_id": "SECRET"},
            {"code": "HK.00700", "qty": 1, "market_val": 400},
        ]


SDK = SimpleNamespace(
    RET_OK=0,
    UserSecurityGroupType=SimpleNamespace(ALL="ALL"),
    TrdMarket=SimpleNamespace(US="US"),
    TrdEnv=SimpleNamespace(REAL="REAL"),
    SecurityFirm=SimpleNamespace(FUTUINC="FUTUINC"),
    OptionType=SimpleNamespace(CALL="CALL", PUT="PUT"),
    SubType=SimpleNamespace(QUOTE="QUOTE", ORDER_BOOK="ORDER_BOOK"),
    KLType=SimpleNamespace(K_5M="K_5M"),
    AuType=SimpleNamespace(NONE="NONE"),
    Currency=SimpleNamespace(USD="USD"),
    ModifyUserSecurityOp=SimpleNamespace(ADD="ADD", DEL="DEL", MOVE_OUT="MOVE_OUT"),
)


class FutuProviderTests(unittest.TestCase):
    def setUp(self):
        self.quotes = []
        self.trades = []

        def quote_factory(**kwargs):
            context = FakeQuoteContext()
            self.quotes.append(context)
            return context

        def trade_factory(**kwargs):
            context = FakeTradeContext()
            self.trades.append(context)
            return context

        self.provider = FutuProvider(
            sdk=SDK, quote_context_factory=quote_factory, trade_context_factory=trade_factory,
            now=lambda: NOW, snapshot_batch_size=1,
        )

    def tearDown(self):
        self.provider.close()

    def test_health_and_quote_rights(self):
        health = self.provider.health()
        self.assertTrue(health.ready)
        self.assertEqual(health.server_version, "10.9")
        rights = self.provider.quote_rights()
        self.assertEqual(rights.status, "ready")
        self.assertEqual(rights.remaining, 497)
        self.assertEqual(rights.subscriptions["QUOTE"], ["US.AAPL"])
        self.assertTrue(all(context.closed for context in self.quotes))

    def test_watchlists_only_include_us_and_deduplicate(self):
        snapshot = self.provider.sync_watchlists()
        self.assertEqual(snapshot.groups["科技"], ["US.AAPL", "US.TSLA"])
        self.assertEqual(snapshot.symbols, ["AAPL", "TSLA"])

    def test_add_watchlist_creates_custom_group_and_remove_uses_move_out(self):
        added = self.provider.add_watchlist("nvda")
        self.assertEqual(added.status, "success")
        self.assertTrue(added.group_created)
        self.assertTrue(added.changed)
        self.assertEqual(self.quotes[-1].watchlist_modifications[-1],
                         ("Options Radar", "ADD", ["US.NVDA"]))

        removed = self.provider.remove_watchlist("tsla", "科技")
        self.assertEqual(removed.status, "success")
        self.assertEqual(self.quotes[-1].watchlist_modifications[-1],
                         ("科技", "MOVE_OUT", ["US.TSLA"]))

    def test_add_watchlist_returns_structured_group_creation_status(self):
        def factory(**kwargs):
            context = FakeQuoteContext()
            context.create_user_security_group = None
            return context

        provider = FutuProvider(sdk=SDK, quote_context_factory=factory, now=lambda: NOW)
        result = provider.add_watchlist("NVDA")
        self.assertEqual(result.status, "needs_group")
        self.assertEqual(result.reason, "group_creation_unsupported")
        self.assertFalse(result.changed)

    def test_watchlist_write_is_idempotent_and_blocks_system_group(self):
        present = self.provider.add_watchlist("AAPL", "科技")
        self.assertEqual(present.reason, "already_present")
        blocked = self.provider.add_watchlist("MSFT", "全部")
        self.assertEqual(blocked.reason, "system_group")
        self.assertFalse(blocked.changed)

    def test_positions_are_read_only_and_discard_account_identity(self):
        snapshot = self.provider.sync_positions()
        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(snapshot.positions[0].quantity, 5)
        self.assertEqual(snapshot.gross_market_value, 1000)
        self.assertEqual(snapshot.nav, 123456.78)
        self.assertEqual(snapshot.cash, 23456.70)
        self.assertNotIn("SECRET", repr(snapshot))
        self.assertNotIn("987654321", repr(snapshot))
        self.assertFalse(hasattr(self.trades[0], "unlock_trade"))
        self.assertEqual(self.trades[0].calls[0]["refresh_cache"], False)
        self.assertEqual(self.trades[0].calls[0]["acc_id"], 987654321)
        self.assertEqual(self.trades[0].fund_calls[0]["acc_id"], 987654321)
        self.assertEqual(self.trades[0].fund_calls[0]["currency"], "USD")

    def test_option_chain_normalises_contract_key_and_filter(self):
        chain = self.provider.get_option_chain("aapl", date(2026, 9, 1), date(2026, 9, 30), "call")
        self.assertEqual(chain[0].contract_key, "US.AAPL|2026-09-18|200|C")
        self.assertEqual(len(chain), 1)

    def test_snapshots_are_batched_and_mark_field_quality(self):
        snapshots = self.provider.get_snapshots(["US.AAPL260918C00200000", "US.AAPL260918P00200000"])
        self.assertEqual(len(snapshots), 2)
        item = snapshots["US.AAPL260918C00200000"]
        self.assertEqual(item.midpoint, 2.2)
        self.assertEqual(item.field_quality["delta"], "native")
        self.assertEqual(len(self.quotes[-1].subscribed), 0)

    def test_subscribe_candidates_diffs_and_releases(self):
        first = self.provider.subscribe_candidates(["US.A", "US.B"])
        second = self.provider.subscribe_candidates(["US.B", "US.C"])
        self.assertEqual(first.subscribed, ["US.A", "US.B"])
        self.assertEqual(second.unsubscribed, ["US.A"])
        self.assertEqual(second.subscribed, ["US.C"])
        self.assertEqual(second.active_codes, ["US.B", "US.C"])
        persistent = self.quotes[-1]
        snapshot = self.provider.get_snapshots(["US.B"])["US.B"]
        self.assertEqual(snapshot.bid, 2.05)
        self.assertEqual(snapshot.ask, 2.35)
        self.assertEqual(persistent.order_book_calls, [("US.B", 1)])
        self.assertFalse(persistent.closed)
        self.provider.close()
        self.assertTrue(persistent.closed)

    def test_history_follows_pages(self):
        bars = self.provider.get_history("US.AAPL260918C00200000", date(2026, 8, 10), date(2026, 8, 11))
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1].close, 2.4)
        self.assertEqual(self.quotes[-1].history_calls[-1][1]["page_req_key"], b"next")

    def test_health_returns_error_object_on_connection_failure(self):
        provider = FutuProvider(sdk=SDK, quote_context_factory=lambda **_: (_ for _ in ()).throw(OSError("down")), now=lambda: NOW)
        health = provider.health()
        self.assertEqual(health.status, "error")
        self.assertIn("OSError", health.message)


if __name__ == "__main__":
    unittest.main()
