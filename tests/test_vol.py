import unittest
from datetime import date

from options_radar.vol import compute_vol_surface


class FakeOptionContract:
    def __init__(self, code, expiry, strike, option_type):
        self.code = code
        self.contract_key = code
        self.symbol = "SPY"
        self.expiry = expiry
        self.strike = strike
        self.option_type = option_type
        self.name = ""
        self.lot_size = 100.0


class FakeSnapshot:
    def __init__(self, implied_volatility, delta):
        self.implied_volatility = implied_volatility
        self.delta = delta


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


class VolSurfaceTests(unittest.TestCase):
    def _contracts(self):
        aug = date(2026, 8, 31)
        sep = date(2026, 9, 30)
        return [
            FakeOptionContract("AUG100C", aug, 100.0, "C"),
            FakeOptionContract("AUG100P", aug, 100.0, "P"),
            FakeOptionContract("AUG95P", aug, 95.0, "P"),
            FakeOptionContract("AUG105C", aug, 105.0, "C"),
            FakeOptionContract("SEP100C", sep, 100.0, "C"),
            FakeOptionContract("SEP100P", sep, 100.0, "P"),
        ]

    def test_compute_vol_surface_skew_and_term_structure(self):
        futu = FakeFutu(
            contracts=self._contracts(),
            snapshots={
                "AUG100C": FakeSnapshot(30.0, 0.52),
                "AUG100P": FakeSnapshot(30.0, -0.48),
                "AUG95P": FakeSnapshot(40.0, -0.25),
                "AUG105C": FakeSnapshot(25.0, 0.25),
                "SEP100C": FakeSnapshot(32.0, 0.50),
                "SEP100P": FakeSnapshot(32.0, -0.50),
            },
        )
        result = compute_vol_surface(futu, "SPY", 100.0, as_of=date(2026, 8, 1))
        self.assertEqual(result.atm_iv, 30.0)
        self.assertEqual(result.put_25d_iv, 40.0)
        self.assertEqual(result.call_25d_iv, 25.0)
        self.assertEqual(result.put_skew, 10.0)
        self.assertEqual(result.call_skew, -5.0)
        self.assertEqual(result.term_structure, {"2026-08-31": 30.0, "2026-09-30": 32.0})

    def test_skips_missing_iv_or_delta(self):
        aug = date(2026, 8, 31)
        futu = FakeFutu(
            contracts=[FakeOptionContract("AUG100C", aug, 100.0, "C")],
            snapshots={"AUG100C": FakeSnapshot(None, 0.5)},
        )
        result = compute_vol_surface(futu, "SPY", 100.0, as_of=date(2026, 8, 1))
        self.assertIsNone(result.atm_iv)
        self.assertEqual(result.term_structure, {})

    def test_returns_empty_on_chain_failure(self):
        futu = FakeFutu(chain_error=RuntimeError("down"))
        result = compute_vol_surface(futu, "SPY", 100.0, as_of=date(2026, 8, 1))
        self.assertEqual(result.symbol, "SPY")
        self.assertIsNone(result.atm_iv)
        self.assertEqual(result.term_structure, {})


if __name__ == "__main__":
    unittest.main()
