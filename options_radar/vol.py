"""Volatility surface: IV skew (25-delta) and term structure (ATM IV by expiry).

Used for seller judgement: put skew (is downside paying richer IV), call skew,
and front/back term structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from .futu_provider import FutuOptionContract

_MAX_BATCH_DAYS = 28


@dataclass
class VolSurface:
    symbol: str
    spot: float
    atm_iv: Optional[float] = None
    put_25d_iv: Optional[float] = None
    call_25d_iv: Optional[float] = None
    put_skew: Optional[float] = None
    call_skew: Optional[float] = None
    term_structure: Dict[str, float] = field(default_factory=dict)


def _fetch_chain(futu: Any, symbol: str, start: date, end: date) -> List[FutuOptionContract]:
    contracts: List[FutuOptionContract] = []
    batch_start = start
    while batch_start <= end:
        batch_end = min(batch_start + timedelta(days=_MAX_BATCH_DAYS), end)
        try:
            contracts.extend(futu.get_option_chain(symbol, batch_start, batch_end))
        except Exception:
            pass
        batch_start = batch_end + timedelta(days=1)
    return contracts


def compute_vol_surface(
    futu: Any,
    symbol: str,
    spot: float,
    *,
    expiry_days: int = 60,
    min_dte: int = 7,
    as_of: Optional[date] = None,
) -> VolSurface:
    try:
        today = as_of or date.today()
        contracts = _fetch_chain(futu, symbol, today, today + timedelta(days=expiry_days))
        eligible = [c for c in contracts if (c.expiry - today).days >= min_dte]
        if not eligible:
            return VolSurface(symbol=symbol, spot=spot)

        snapshots = futu.get_snapshots([c.code for c in eligible])

        def iv_of(contract: FutuOptionContract) -> Optional[float]:
            snap = snapshots.get(contract.code)
            return snap.implied_volatility if snap is not None else None

        by_expiry: Dict[date, List[FutuOptionContract]] = {}
        for contract in eligible:
            by_expiry.setdefault(contract.expiry, []).append(contract)

        # ATM IV of the nearest expiry (call/put averaged at the closest strike).
        atm_iv: Optional[float] = None
        nearest_expiry = min(by_expiry)
        near = by_expiry[nearest_expiry]
        min_dist = min(abs(c.strike - spot) for c in near)
        atm_cs = [c for c in near if abs(c.strike - spot) == min_dist]
        atm_ivs = [iv for iv in (iv_of(c) for c in atm_cs) if iv is not None]
        if atm_ivs:
            atm_iv = sum(atm_ivs) / len(atm_ivs)

        # 25-delta put / call by smallest delta distance.
        best_put = (999.0, None)
        best_call = (999.0, None)
        for contract in eligible:
            snap = snapshots.get(contract.code)
            if snap is None or snap.delta is None or snap.implied_volatility is None:
                continue
            if contract.option_type == "P":
                dist = abs(snap.delta - (-0.25))
                if dist < best_put[0]:
                    best_put = (dist, snap.implied_volatility)
            else:
                dist = abs(snap.delta - 0.25)
                if dist < best_call[0]:
                    best_call = (dist, snap.implied_volatility)

        put_25d_iv = best_put[1]
        call_25d_iv = best_call[1]
        put_skew = (put_25d_iv - atm_iv) if (put_25d_iv is not None and atm_iv is not None) else None
        call_skew = (call_25d_iv - atm_iv) if (call_25d_iv is not None and atm_iv is not None) else None

        term_structure: Dict[str, float] = {}
        for expiry in sorted(by_expiry):
            cs = by_expiry[expiry]
            min_d = min(abs(c.strike - spot) for c in cs)
            atm_cs = [c for c in cs if abs(c.strike - spot) == min_d]
            ivs = [iv for iv in (iv_of(c) for c in atm_cs) if iv is not None]
            if ivs:
                term_structure[expiry.isoformat()] = sum(ivs) / len(ivs)

        return VolSurface(
            symbol=symbol,
            spot=spot,
            atm_iv=atm_iv,
            put_25d_iv=put_25d_iv,
            call_25d_iv=call_25d_iv,
            put_skew=put_skew,
            call_skew=call_skew,
            term_structure=term_structure,
        )
    except Exception:
        return VolSurface(symbol=symbol, spot=spot)
