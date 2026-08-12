"""Provider registry and deterministic per-field market-data fusion."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .models import MarketSnapshot
from .provider_types import (
    CompositeMarketSnapshot, DataConflict, ProviderHealth, ProviderMarketSnapshot,
    QUALITY_ORDER, SourcedValue,
)


MARKET_FIELDS = (
    "bid", "ask", "last", "volume", "open_interest", "implied_volatility",
    "delta", "gamma", "vega", "theta", "rho", "underlying_price",
)


class ProviderRegistry:
    def __init__(
        self, providers: Optional[Mapping[str, Any]] = None, *,
        priority: Optional[Iterable[str]] = None,
        enabled: Optional[Mapping[str, bool]] = None,
        max_quote_age_seconds: int = 60,
        conflict_threshold_pct: float = 15.0,
    ):
        self.providers: Dict[str, Any] = dict(providers or {})
        configured = list(priority or self.providers)
        self.priority = configured + [name for name in self.providers if name not in configured]
        self.enabled = {name: bool((enabled or {}).get(name, True)) for name in self.providers}
        self.max_quote_age_seconds = max(1, int(max_quote_age_seconds))
        self.conflict_threshold_pct = max(0.0, float(conflict_threshold_pct))
        self._last_composites: Dict[str, CompositeMarketSnapshot] = {}

    def register(self, name: str, provider: Any, enabled: bool = True) -> None:
        self.providers[name] = provider
        self.enabled[name] = enabled
        if name not in self.priority:
            self.priority.append(name)

    def set_enabled(self, name: str, value: bool) -> Dict[str, Any]:
        if name not in self.providers:
            raise KeyError(f"unknown provider: {name}")
        self.enabled[name] = bool(value)
        return self.status(name)

    def set_priority(self, name: str, position: int) -> List[str]:
        if name not in self.providers:
            raise KeyError(f"unknown provider: {name}")
        values = [item for item in self.priority if item != name]
        values.insert(max(0, min(int(position), len(values))), name)
        self.priority = values
        return list(values)

    def status(self, name: str) -> Dict[str, Any]:
        provider = self.providers.get(name)
        if provider is None:
            return {"provider": name, "configured": False, "connected": False, "status": "missing"}
        try:
            health = provider.health()
            value = health.to_dict() if isinstance(health, ProviderHealth) else dict(health)
        except Exception as exc:
            value = {"provider": name, "configured": True, "connected": False,
                     "status": "error", "last_error": f"{type(exc).__name__}:{str(exc)[:160]}"}
        value["enabled"] = self.enabled.get(name, True)
        value["priority"] = self.priority.index(name) if name in self.priority else None
        try:
            capabilities = provider.capabilities()
            value["capabilities"] = capabilities.__dict__ if hasattr(capabilities, "__dict__") else dict(capabilities)
        except Exception:
            value["capabilities"] = {}
        return value

    def statuses(self) -> Dict[str, Any]:
        return {
            "market_priority": list(self.priority),
            "providers": [self.status(name) for name in self.priority if name in self.providers],
        }

    def test(self, name: str) -> Dict[str, Any]:
        provider = self.providers.get(name)
        if provider is None:
            return {"provider": name, "status": "missing"}
        test_method = getattr(provider, "test_connection", None)
        if callable(test_method):
            test_method()
        return self.status(name)

    @staticmethod
    def _point_fresh(point: SourcedValue, now: datetime, max_age: int) -> bool:
        timestamp = point.market_timestamp or point.received_at
        if timestamp.tzinfo and not now.tzinfo:
            now = now.replace(tzinfo=timezone.utc)
        elif now.tzinfo and not timestamp.tzinfo:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return max(0.0, (now - timestamp).total_seconds()) <= max_age

    def _rank(self, point: SourcedValue, now: datetime) -> tuple:
        quality = QUALITY_ORDER.get(point.quality, 0)
        freshness = 1 if self._point_fresh(point, now, self.max_quote_age_seconds) else 0
        priority = -self.priority.index(point.provider) if point.provider in self.priority else -999
        return quality, freshness, priority

    def _collect(self, contract_key: str) -> Dict[str, ProviderMarketSnapshot]:
        values: Dict[str, ProviderMarketSnapshot] = {}
        for name in self.priority:
            provider = self.providers.get(name)
            if provider is None or not self.enabled.get(name, True):
                continue
            try:
                method = getattr(provider, "get_snapshot", None)
                snapshot = method(contract_key) if callable(method) else provider.get_snapshots([contract_key]).get(contract_key)
                if snapshot:
                    values[name] = snapshot
            except Exception:
                continue
        return values

    def composite_snapshot(
        self, contract_key: str, snapshots: Optional[Mapping[str, ProviderMarketSnapshot]] = None,
        now: Optional[datetime] = None,
    ) -> CompositeMarketSnapshot:
        now = now or datetime.utcnow()
        candidates = dict(snapshots or self._collect(contract_key))
        fields: Dict[str, SourcedValue] = {}

        # Bid/ask are an atomic pair: never combine them across providers or qualities.
        quote_pairs = []
        for name, snapshot in candidates.items():
            bid, ask = snapshot.fields.get("bid"), snapshot.fields.get("ask")
            if bid and ask and bid.usable and ask.usable and bid.provider == ask.provider and bid.quality == ask.quality:
                if float(ask.value) >= float(bid.value):
                    quote_pairs.append((self._rank(bid, now), name, bid, ask))
        quote_pairs.sort(reverse=True, key=lambda item: item[0])
        quote_provider = None
        if quote_pairs:
            _, quote_provider, fields["bid"], fields["ask"] = quote_pairs[0]

        for field_name in MARKET_FIELDS:
            if field_name in {"bid", "ask"}:
                continue
            points = [snapshot.fields[field_name] for snapshot in candidates.values()
                      if field_name in snapshot.fields and snapshot.fields[field_name].usable]
            if points:
                fields[field_name] = max(points, key=lambda item: self._rank(item, now))

        conflicts: List[DataConflict] = []
        realtime_mids = []
        for _, name, bid, ask in quote_pairs:
            if bid.quality == "realtime" and self._point_fresh(bid, now, self.max_quote_age_seconds):
                realtime_mids.append((name, (float(bid.value) + float(ask.value)) / 2.0))
        if len(realtime_mids) >= 2:
            values = [item[1] for item in realtime_mids]
            base = min(values)
            difference = ((max(values) - base) / base * 100.0) if base > 0 else 100.0
            if difference > self.conflict_threshold_pct:
                conflicts.append(DataConflict(
                    "midpoint", [item[0] for item in realtime_mids], values, round(difference, 4),
                    self.conflict_threshold_pct, "实时供应商报价差异超过阈值，暂停生成入场限价",
                ))

        data_status = "missing"
        if quote_provider:
            quality = fields["bid"].quality
            fresh = self._point_fresh(fields["bid"], now, self.max_quote_age_seconds)
            if conflicts:
                data_status = "conflict"
            elif quality == "realtime" and fresh:
                data_status = "ok"
            elif quality == "realtime":
                data_status = "stale"
            else:
                data_status = quality
        elif any(point.usable for point in fields.values()):
            data_status = max(fields.values(), key=lambda item: QUALITY_ORDER.get(item.quality, 0)).quality

        result = CompositeMarketSnapshot(contract_key, fields, now, quote_provider, data_status, conflicts, candidates)
        self._last_composites[contract_key] = result
        return result

    def provenance(self, contract_key: str) -> Dict[str, Any]:
        value = self._last_composites.get(contract_key) or self.composite_snapshot(contract_key)
        return value.provenance()

    def compare(self, contract_key: str) -> Dict[str, Any]:
        value = self.composite_snapshot(contract_key)
        return {
            "composite": value.provenance(),
            "providers": {
                name: {field: point.to_dict() for field, point in snapshot.fields.items()}
                for name, snapshot in value.candidates.items()
            },
        }

    def aggregate_accounts(self) -> Dict[str, Any]:
        """Combine enabled broker accounts without retaining account numbers."""
        snapshots = []
        for name in self.priority:
            provider = self.providers.get(name)
            if provider is None or not self.enabled.get(name, True):
                continue
            method = getattr(provider, "sync_accounts", None)
            if not callable(method):
                continue
            try:
                snapshots.extend(method())
            except Exception:
                continue
        positions: Dict[str, Dict[str, float]] = {}
        nav_values, cash_values = [], []
        for snapshot in snapshots:
            if snapshot.nav is not None:
                nav_values.append(float(snapshot.nav))
            if snapshot.cash is not None:
                cash_values.append(float(snapshot.cash))
            for symbol, item in snapshot.positions.items():
                target = positions.setdefault(symbol, {
                    "quantity": 0.0, "market_value": 0.0, "cost_basis": 0.0,
                    "asset_is_option": float(item.get("asset_is_option", 0.0)),
                })
                quantity = float(item.get("quantity", 0.0))
                target["quantity"] += quantity
                target["market_value"] += float(item.get("market_value", 0.0))
                target["cost_basis"] += float(item.get("cost_basis", 0.0))
        return {
            "as_of": datetime.utcnow().isoformat(),
            "nav": sum(nav_values) if nav_values else None,
            "cash": sum(cash_values) if cash_values else None,
            "positions": positions,
            "sources": sorted({item.provider for item in snapshots}),
            "account_count": len(snapshots),
        }


def market_snapshot_from_composite(value: CompositeMarketSnapshot) -> MarketSnapshot:
    def number(name: str) -> Optional[float]:
        point = value.fields.get(name)
        return float(point.value) if point and point.value is not None else None

    provider = value.quote_provider or (
        next(iter(value.fields.values())).provider if value.fields else "unknown"
    )
    result = MarketSnapshot(
        contract_key=value.contract_key, observed_at=value.observed_at,
        bid=number("bid"), ask=number("ask"), last=number("last"),
        volume=number("volume"), open_interest=number("open_interest"),
        implied_volatility=number("implied_volatility"), delta=number("delta"),
        underlying_price=number("underlying_price"), data_status=value.data_status,
        provider=provider,
        field_quality={name: point.quality for name, point in value.fields.items()},
    )
    result.provenance = {name: point.to_dict() for name, point in value.fields.items()}
    result.data_conflicts = [item.message for item in value.conflicts]
    return result
