import unittest
from datetime import date

from options_radar.gex import compute_gex


class FakeOptionContract:
    def __init__(self, code, contract_key, expiry, strike, option_type, lot_size=100.0):
        self.code = code
        self.contract_key = contract_key
        self.expiry = expiry
        self.strike = strike
        self.option_type = option_type
        self.lot_size = lot_size


class FakeSnapshot:
    def __init__(self, open_interest, gamma):
        self.open_interest = open_interest
        self.gamma = gamma


class FakeFutu:
    def __init__(self, contracts=None, snapshots=None, chain_error=None):
        self.contracts = contracts or []
        self.snapshots = snapshots or {}
        self.chain_error = chain_error

    def get_option_chain(self, symbol, start, end, option_type=None):
        if self.chain_error is not None:
            raise self.chain_error
        return [c for c in self.contracts if start <= c.expiry <= end]

    def get_snapshots(self, codes):
        return {code: self.snapshots[code] for code in codes if code in self.snapshots}


class GexTests(unittest.TestCase):
    def test_compute_gex_aggregates_walls_flip_and_regime(self):
        expiry = date(2026, 8, 31)
        futu = FakeFutu(
            contracts=[
                FakeOptionContract("US.SPY|2026-08-31|95|C", "US.SPY|2026-08-31|95|C", expiry, 95.0, "C"),
                FakeOptionContract("US.SPY|2026-08-31|95|P", "US.SPY|2026-08-31|95|P", expiry, 95.0, "P"),
                FakeOptionContract("US.SPY|2026-08-31|105|C", "US.SPY|2026-08-31|105|C", expiry, 105.0, "C"),
                FakeOptionContract("US.SPY|2026-08-31|105|P", "US.SPY|2026-08-31|105|P", expiry, 105.0, "P"),
            ],
            snapshots={
                "US.SPY|2026-08-31|95|C": FakeSnapshot(100.0, 0.5),
                "US.SPY|2026-08-31|95|P": FakeSnapshot(100.0, 0.25),
                "US.SPY|2026-08-31|105|C": FakeSnapshot(100.0, 0.25),
                "US.SPY|2026-08-31|105|P": FakeSnapshot(100.0, 0.75),
            },
        )

        result = compute_gex(futu, "SPY", 100.0, as_of=date(2026, 8, 1))

        self.assertEqual(result.symbol, "SPY")
        self.assertEqual(result.spot, 100.0)
        self.assertEqual(result.strikes, [95.0, 105.0])
        self.assertEqual(result.net_gex, [2500.0, -5000.0])
        self.assertEqual(result.call_gex, {95.0: 5000.0, 105.0: 2500.0})
        self.assertEqual(result.put_gex, {95.0: -2500.0, 105.0: -7500.0})
        self.assertEqual(result.call_wall, 95.0)
        self.assertEqual(result.put_wall, 105.0)
        self.assertEqual(result.gamma_flip, 95.0)
        self.assertEqual(result.regime, "negative")
        self.assertEqual(result.max_pos_gex, 2500.0)
        self.assertEqual(result.max_neg_gex, -5000.0)
        self.assertEqual(result.expiry_count, 1)

    def test_compute_gex_skips_missing_open_interest_or_gamma(self):
        expiry = date(2026, 8, 31)
        futu = FakeFutu(
            contracts=[
                FakeOptionContract("US.SPY|2026-08-31|95|C", "US.SPY|2026-08-31|95|C", expiry, 95.0, "C"),
                FakeOptionContract("US.SPY|2026-08-31|95|P", "US.SPY|2026-08-31|95|P", expiry, 95.0, "P"),
                FakeOptionContract("US.SPY|2026-08-31|105|C", "US.SPY|2026-08-31|105|C", expiry, 105.0, "C"),
            ],
            snapshots={
                "US.SPY|2026-08-31|95|C": FakeSnapshot(None, 0.5),
                "US.SPY|2026-08-31|95|P": FakeSnapshot(100.0, None),
                "US.SPY|2026-08-31|105|C": FakeSnapshot(100.0, 0.5),
            },
        )

        result = compute_gex(futu, "SPY", 100.0, as_of=date(2026, 8, 1))

        self.assertEqual(result.strikes, [105.0])
        self.assertEqual(result.net_gex, [5000.0])
        self.assertEqual(result.call_gex, {105.0: 5000.0})
        self.assertEqual(result.put_gex, {})
        self.assertEqual(result.expiry_count, 1)

    def test_compute_gex_filters_near_expiry_and_out_of_range_strikes(self):
        near = date(2026, 8, 3)
        far = date(2026, 8, 31)
        futu = FakeFutu(
            contracts=[
                FakeOptionContract("US.SPY|2026-08-03|95|C", "US.SPY|2026-08-03|95|C", near, 95.0, "C"),
                FakeOptionContract("US.SPY|2026-08-31|130|C", "US.SPY|2026-08-31|130|C", far, 130.0, "C"),
                FakeOptionContract("US.SPY|2026-08-31|95|C", "US.SPY|2026-08-31|95|C", far, 95.0, "C"),
            ],
            snapshots={
                "US.SPY|2026-08-03|95|C": FakeSnapshot(100.0, 0.5),
                "US.SPY|2026-08-31|130|C": FakeSnapshot(100.0, 0.5),
                "US.SPY|2026-08-31|95|C": FakeSnapshot(100.0, 0.5),
            },
        )

        result = compute_gex(futu, "SPY", 100.0, as_of=date(2026, 8, 1))

        self.assertEqual(result.strikes, [95.0])
        self.assertEqual(result.net_gex, [5000.0])
        self.assertEqual(result.expiry_count, 1)

    def test_compute_gex_returns_empty_when_chain_fails(self):
        futu = FakeFutu(chain_error=RuntimeError("down"))
        result = compute_gex(futu, "SPY", 100.0, as_of=date(2026, 8, 1))
        self.assertEqual(result.symbol, "SPY")
        self.assertEqual(result.strikes, [])
        self.assertEqual(result.net_gex, [])
        self.assertEqual(result.regime, "mixed")
        self.assertIsNone(result.call_wall)
        self.assertIsNone(result.put_wall)
        self.assertIsNone(result.gamma_flip)
        self.assertEqual(result.expiry_count, 0)

    def test_compute_gex_returns_empty_when_snapshots_empty(self):
        expiry = date(2026, 8, 31)
        futu = FakeFutu(
            contracts=[
                FakeOptionContract("US.SPY|2026-08-31|95|C", "US.SPY|2026-08-31|95|C", expiry, 95.0, "C"),
            ],
            snapshots={},
        )
        result = compute_gex(futu, "SPY", 100.0, as_of=date(2026, 8, 1))
        self.assertEqual(result.strikes, [])
        self.assertEqual(result.regime, "mixed")
        self.assertEqual(result.expiry_count, 0)


if __name__ == "__main__":
    unittest.main()
