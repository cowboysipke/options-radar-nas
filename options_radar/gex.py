"""Dealer gamma exposure (GEX) aggregation keyed by strike.

GEX surfaces the option strikes where market-maker gamma is most concentrated
(gamma walls), where the net exposure flips sign (gamma flip), and the overall
positive/negative/mixed regime.  The module depends only on a ``futu``-like
object exposing ``get_option_chain`` and ``get_snapshots`` so it can be tested
with a fake provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Set

from .futu_provider import FutuOptionContract

_MAX_BATCH_DAYS = 28
_DEFAULT_LOT_SIZE = 100.0
_MIXED_RATIO = 0.05


@dataclass
class GexResult:
    symbol: str
    spot: float
    strikes: List[float] = field(default_factory=list)
    net_gex: List[float] = field(default_factory=list)
    call_gex: Dict[float, float] = field(default_factory=dict)
    put_gex: Dict[float, float] = field(default_factory=dict)
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    gamma_flip: Optional[float] = None
    zero_gamma: Optional[float] = None
    regime: str = "mixed"
    max_pos_gex: float = 0.0
    max_neg_gex: float = 0.0
    expiry_count: int = 0


def _fetch_chain(
    futu: Any, symbol: str, start: date, end: date
) -> List[FutuOptionContract]:
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


def _gamma_flip(strikes: List[float], net_gex: List[float]) -> Optional[float]:
    """Macro gamma flip: the strike where cumulative GEX crosses zero.

    Per-strike GEX is jagged (OI concentrates at integer strikes), so a raw
    sign change between adjacent strikes is noise. The cumulative-crossing
    point is the meaningful dealer gamma flip.
    """
    cumulative = 0.0
    prev_cum: Optional[float] = None
    prev_strike: Optional[float] = None
    for strike, value in zip(strikes, net_gex):
        cumulative += value
        if prev_cum is not None and prev_strike is not None:
            if (prev_cum < 0.0 < cumulative) or (prev_cum > 0.0 > cumulative):
                return prev_strike if abs(prev_cum) <= abs(cumulative) else strike
        prev_cum = cumulative
        prev_strike = strike
    return None


def _closest_to_zero(strikes: List[float], net_gex: List[float]) -> Optional[float]:
    """Strike where cumulative GEX is closest to zero (approx zero-gamma)."""
    if not strikes:
        return None
    best = (float("inf"), None)
    acc = 0.0
    for strike, value in zip(strikes, net_gex):
        acc += value
        if abs(acc) < best[0]:
            best = (abs(acc), strike)
    return best[1]


def _regime(net_gex: List[float]) -> str:
    total = sum(net_gex)
    gross = sum(abs(value) for value in net_gex)
    if gross > 0.0 and abs(total) < _MIXED_RATIO * gross:
        return "mixed"
    if total > 0.0:
        return "positive"
    if total < 0.0:
        return "negative"
    return "mixed"


def compute_gex(
    futu: Any,
    symbol: str,
    spot: float,
    *,
    expiry_days: int = 60,
    strike_pct: float = 0.10,
    min_dte: int = 7,
    as_of: Optional[date] = None,
) -> GexResult:
    try:
        today = as_of or date.today()
        contracts = _fetch_chain(futu, symbol, today, today + timedelta(days=expiry_days))

        low = spot * (1.0 - strike_pct)
        high = spot * (1.0 + strike_pct)
        eligible: List[FutuOptionContract] = []
        seen: Set[str] = set()
        for contract in contracts:
            if contract.code in seen:
                continue
            seen.add(contract.code)
            if (contract.expiry - today).days < min_dte:
                continue
            if contract.strike < low or contract.strike > high:
                continue
            eligible.append(contract)

        if not eligible:
            return GexResult(symbol=symbol, spot=spot)

        snapshots = futu.get_snapshots([contract.code for contract in eligible])

        call_gex: Dict[float, float] = {}
        put_gex: Dict[float, float] = {}
        expiries: Set[date] = set()
        for contract in eligible:
            snapshot = snapshots.get(contract.code)
            if snapshot is None:
                continue
            open_interest = snapshot.open_interest
            gamma = snapshot.gamma
            if open_interest is None or gamma is None:
                continue
            lot_size = (
                contract.lot_size
                if contract.lot_size is not None
                else _DEFAULT_LOT_SIZE
            )
            # Futu gamma is always positive (BS convexity). SpotGamma sign
            # convention: call GEX positive, put GEX negative, so the net curve
            # crosses zero (gamma flip) between call-heavy and put-heavy strikes.
            if contract.option_type == "C":
                contribution = open_interest * lot_size * gamma
            else:
                contribution = -open_interest * lot_size * gamma
            bucket = call_gex if contract.option_type == "C" else put_gex
            bucket[contract.strike] = bucket.get(contract.strike, 0.0) + contribution
            expiries.add(contract.expiry)

        strikes = sorted(set(call_gex) | set(put_gex))
        net_gex = [
            call_gex.get(strike, 0.0) + put_gex.get(strike, 0.0)
            for strike in strikes
        ]

        call_wall: Optional[float] = None
        put_wall: Optional[float] = None
        if call_gex:
            peak = max(call_gex.values())
            if peak > 0.0:
                call_wall = max(call_gex, key=call_gex.get)
        if put_gex:
            trough = min(put_gex.values())
            if trough < 0.0:
                put_wall = min(put_gex, key=put_gex.get)

        max_pos_gex = max((value for value in net_gex if value > 0.0), default=0.0)
        max_neg_gex = min((value for value in net_gex if value < 0.0), default=0.0)

        flip = _gamma_flip(strikes, net_gex)
        zero_gamma = flip if flip is not None else _closest_to_zero(strikes, net_gex)

        return GexResult(
            symbol=symbol,
            spot=spot,
            strikes=strikes,
            net_gex=net_gex,
            call_gex=call_gex,
            put_gex=put_gex,
            call_wall=call_wall,
            put_wall=put_wall,
            gamma_flip=flip,
            zero_gamma=zero_gamma,
            regime=_regime(net_gex),
            max_pos_gex=max_pos_gex,
            max_neg_gex=max_neg_gex,
            expiry_count=len(expiries),
        )
    except Exception:
        # Degrade to an empty result so callers can tolerate provider outages.
        return GexResult(symbol=symbol, spot=spot)
