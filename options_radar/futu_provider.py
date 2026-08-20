"""Read-only Futu OpenD market and portfolio adapter.

The adapter deliberately imports only quote/position APIs.  A SDK object and
context factories can be injected so the module remains testable on machines
where ``futu-api`` is not installed (for example the GitHub test runner).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from threading import RLock
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _records(value: Any) -> List[Dict[str, Any]]:
    """Convert pandas frames, mappings, and fixture lists into records."""
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        try:
            rows = value.to_dict("records")
            if isinstance(rows, list):
                return [dict(row) for row in rows]
        except (TypeError, ValueError):
            pass
    if hasattr(value, "iterrows"):
        return [dict(row) for _, row in value.iterrows()]
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [dict(row) for row in value]
    return []


def _float(row: Mapping[str, Any], *names: str) -> Optional[float]:
    for name in names:
        value = row.get(name)
        if value not in (None, "", "N/A", "--"):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _text(row: Mapping[str, Any], *names: str) -> Optional[str]:
    for name in names:
        value = row.get(name)
        if value not in (None, "", "N/A", "--"):
            return str(value)
    return None


def _timestamp(value: Any) -> Optional[datetime]:
    if value in (None, "", "N/A", "--"):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _enum(container: Any, enum_name: str, member: str, fallback: Any) -> Any:
    enum_type = getattr(container, enum_name, None)
    return getattr(enum_type, member, fallback)


def _normalise_code(symbol_or_code: str) -> str:
    value = str(symbol_or_code).strip().upper()
    return value if "." in value else f"US.{value}"


def _normalise_option_type(value: Any) -> str:
    text = str(value or "").upper()
    if "CALL" in text or text == "C":
        return "C"
    if "PUT" in text or text == "P":
        return "P"
    return ""


def _strike_text(value: float) -> str:
    return (f"{value:.8f}").rstrip("0").rstrip(".")


@dataclass(frozen=True)
class FutuHealth:
    status: str
    ready: bool
    qot_logged_in: bool
    trade_logged_in: bool
    server_version: Optional[str]
    market_us: Optional[str]
    checked_at: datetime
    message: str = ""


@dataclass(frozen=True)
class FutuQuoteRights:
    status: str
    qot_logged_in: bool
    total_used: int = 0
    own_used: int = 0
    remaining: int = 0
    subscriptions: Dict[str, List[str]] = field(default_factory=dict)
    security_firm: Optional[str] = None
    checked_at: datetime = field(default_factory=_utcnow)
    message: str = ""


@dataclass(frozen=True)
class WatchlistSnapshot:
    as_of: datetime
    groups: Dict[str, List[str]]
    symbols: List[str]


@dataclass(frozen=True)
class WatchlistMutationResult:
    """Machine-readable outcome for a Futu watchlist write."""

    status: str
    action: str
    code: str
    group: str
    changed: bool = False
    group_created: bool = False
    reason: str = ""
    message: str = ""


@dataclass(frozen=True)
class FutuPosition:
    code: str
    symbol: str
    quantity: float
    market_value: Optional[float] = None
    cost_price: Optional[float] = None
    nominal_price: Optional[float] = None
    security_type: Optional[str] = None
    currency: Optional[str] = None


@dataclass(frozen=True)
class FutuPortfolioSnapshot:
    as_of: datetime
    positions: List[FutuPosition]
    gross_market_value: float
    nav: Optional[float] = None
    cash: Optional[float] = None
    quality: str = "native"


@dataclass(frozen=True)
class FutuOptionContract:
    code: str
    contract_key: str
    symbol: str
    expiry: date
    strike: float
    option_type: str
    name: Optional[str] = None
    lot_size: Optional[float] = None


@dataclass(frozen=True)
class FutuMarketSnapshot:
    code: str
    observed_at: datetime
    market_timestamp: Optional[datetime]
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    volume: Optional[float]
    open_interest: Optional[float]
    implied_volatility: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]
    vega: Optional[float]
    theta: Optional[float]
    rho: Optional[float]
    underlying_price: Optional[float]
    data_type: Optional[str]
    field_quality: Dict[str, str]

    @property
    def midpoint(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None and self.ask >= self.bid:
            return (self.bid + self.ask) / 2.0
        return self.last


@dataclass(frozen=True)
class SubscriptionResult:
    active_codes: List[str]
    subscribed: List[str]
    unsubscribed: List[str]
    status: str
    message: str = ""


@dataclass(frozen=True)
class FutuHistoryBar:
    code: str
    timestamp: datetime
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[float]
    turnover: Optional[float]
    interval: str


class FutuProvider:
    """Futu OpenD provider restricted to reads and quote subscriptions."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        security_firm: str = "NONE",
        *,
        sdk: Any = None,
        quote_context_factory: Optional[Callable[..., Any]] = None,
        trade_context_factory: Optional[Callable[..., Any]] = None,
        now: Callable[[], datetime] = _utcnow,
        snapshot_batch_size: int = 100,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.security_firm = security_firm
        self._sdk_value = sdk
        self._quote_factory = quote_context_factory
        self._trade_factory = trade_context_factory
        self._now = now
        self.snapshot_batch_size = max(1, min(int(snapshot_batch_size), 400))
        self._subscription_context: Any = None
        self._active_codes: Set[str] = set()
        self._subscription_lock = RLock()

    @property
    def sdk(self) -> Any:
        if self._sdk_value is None:
            try:
                import futu  # type: ignore
            except ImportError as exc:
                raise RuntimeError("futu-api package is required for a live OpenD connection") from exc
            self._sdk_value = futu
        return self._sdk_value

    def _ok(self, ret: Any) -> bool:
        return ret == getattr(self.sdk, "RET_OK", 0)

    def _quote_context(self) -> Any:
        factory = self._quote_factory or self.sdk.OpenQuoteContext
        return factory(host=self.host, port=self.port)

    def _trade_context(self) -> Any:
        factory = self._trade_factory or self.sdk.OpenSecTradeContext
        kwargs = {
            "filter_trdmarket": _enum(self.sdk, "TrdMarket", "US", "US"),
            "host": self.host,
            "port": self.port,
            "security_firm": getattr(
                getattr(self.sdk, "SecurityFirm", object()),
                self.security_firm,
                self.security_firm,
            ),
        }
        return factory(**kwargs)

    @staticmethod
    def _close(context: Any) -> None:
        close = getattr(context, "close", None)
        if callable(close):
            close()

    def health(self) -> FutuHealth:
        checked_at = self._now()
        context = None
        try:
            context = self._quote_context()
            ret, data = context.get_global_state()
            if not self._ok(ret) or not isinstance(data, Mapping):
                return FutuHealth("error", False, False, False, None, None, checked_at, str(data)[:300])
            program_status = str(data.get("program_status_type", "UNKNOWN"))
            qot_logged_in = bool(data.get("qot_logined", False))
            ready = qot_logged_in and program_status.upper() == "READY"
            return FutuHealth(
                "ready" if ready else "degraded",
                ready,
                qot_logged_in,
                bool(data.get("trd_logined", False)),
                _text(data, "server_ver"),
                _text(data, "market_us"),
                checked_at,
                str(data.get("program_status_desc", ""))[:300],
            )
        except Exception as exc:
            return FutuHealth("error", False, False, False, None, None, checked_at, f"{type(exc).__name__}: {str(exc)[:250]}")
        finally:
            if context is not None:
                self._close(context)

    def quote_rights(self) -> FutuQuoteRights:
        """Return login and subscription quota facts reported by OpenD.

        The SDK does not expose a universal label for every market entitlement;
        actual option entitlement is therefore established by successful quote
        subscription/snapshot calls rather than inferred here.
        """
        checked_at = self._now()
        context = None
        try:
            context = self._quote_context()
            state_ret, state = context.get_global_state()
            sub_ret, sub = context.query_subscription(is_all_conn=True)
            if not self._ok(state_ret) or not isinstance(state, Mapping):
                return FutuQuoteRights("error", False, checked_at=checked_at, message=str(state)[:300])
            qot_logged_in = bool(state.get("qot_logined", False))
            if not self._ok(sub_ret) or not isinstance(sub, Mapping):
                return FutuQuoteRights("degraded", qot_logged_in, checked_at=checked_at, message=str(sub)[:300])
            subscriptions = {
                str(kind): sorted(str(code) for code in codes)
                for kind, codes in dict(sub.get("sub_list") or {}).items()
            }
            return FutuQuoteRights(
                "ready" if qot_logged_in else "degraded",
                qot_logged_in,
                int(sub.get("total_used", 0) or 0),
                int(sub.get("own_used", 0) or 0),
                int(sub.get("remain", 0) or 0),
                subscriptions,
                _text(sub, "own_security_firm"),
                checked_at,
            )
        except Exception as exc:
            return FutuQuoteRights("error", False, checked_at=checked_at, message=f"{type(exc).__name__}: {str(exc)[:250]}")
        finally:
            if context is not None:
                self._close(context)

    def sync_watchlists(self) -> WatchlistSnapshot:
        context = self._quote_context()
        groups: Dict[str, List[str]] = {}
        try:
            group_type = _enum(self.sdk, "UserSecurityGroupType", "ALL", "ALL")
            ret, data = context.get_user_security_group(group_type=group_type)
            if not self._ok(ret):
                raise RuntimeError(f"get_user_security_group failed: {str(data)[:300]}")
            for group_row in _records(data):
                group_name = str(group_row.get("group_name", "")).strip()
                if not group_name:
                    continue
                item_ret, items = context.get_user_security(group_name)
                if not self._ok(item_ret):
                    continue
                codes = sorted({
                    str(row.get("code", "")).upper()
                    for row in _records(items)
                    if str(row.get("code", "")).upper().startswith("US.")
                })
                groups[group_name] = codes
        finally:
            self._close(context)
        symbols = sorted({code.split(".", 1)[1] for codes in groups.values() for code in codes})
        return WatchlistSnapshot(self._now(), groups, symbols)

    def _watchlist_groups(self, context: Any) -> Tuple[bool, Any, List[Dict[str, Any]]]:
        group_type = _enum(self.sdk, "UserSecurityGroupType", "ALL", "ALL")
        ret, data = context.get_user_security_group(group_type=group_type)
        return self._ok(ret), data, _records(data) if self._ok(ret) else []

    def _create_watchlist_group(self, context: Any, group: str) -> Tuple[bool, bool, str]:
        """Use a group-creation extension only when the installed SDK exposes it.

        Futu API v10.9 documents security-list modification but no group creation
        method. Capability detection makes this forward compatible while
        returning an explicit status on current SDK releases.
        """
        creator = getattr(context, "create_user_security_group", None)
        if not callable(creator):
            creator = getattr(context, "create_watchlist_group", None)
        if not callable(creator):
            return False, False, "OpenD SDK does not expose custom-group creation"
        try:
            result = creator(group)
            if isinstance(result, tuple) and len(result) >= 2:
                ret, message = result[0], result[1]
                return self._ok(ret), True, str(message or "")[:300]
            return bool(result), True, ""
        except Exception as exc:
            return False, True, f"{type(exc).__name__}: {str(exc)[:250]}"

    def _mutate_watchlist(self, symbol: str, group: str, *, add: bool) -> WatchlistMutationResult:
        action = "add" if add else "remove"
        code = _normalise_code(symbol) if str(symbol).strip() else ""
        group = str(group).strip()
        if not code or not group:
            return WatchlistMutationResult(
                "invalid", action, code, group, reason="invalid_input",
                message="symbol and group are required",
            )
        context = self._quote_context()
        created = False
        try:
            groups_ok, groups_data, rows = self._watchlist_groups(context)
            if not groups_ok:
                return WatchlistMutationResult(
                    "error", action, code, group, reason="group_query_failed",
                    message=str(groups_data)[:300],
                )
            matching = [row for row in rows if str(row.get("group_name", "")) == group]
            if len(matching) > 1:
                return WatchlistMutationResult(
                    "blocked", action, code, group, reason="duplicate_group_name",
                    message="multiple watchlist groups have the same name",
                )
            if matching:
                group_kind = str(matching[0].get("group_type", "")).upper()
                if group_kind and "CUSTOM" not in group_kind and group_kind not in {"1", "GROUPTYPE_CUSTOM"}:
                    return WatchlistMutationResult(
                        "blocked", action, code, group, reason="system_group",
                        message="watchlist writes require a custom group",
                    )
            elif not add:
                return WatchlistMutationResult(
                    "unchanged", action, code, group, reason="group_missing",
                    message="custom watchlist group does not exist",
                )
            else:
                create_ok, supported, message = self._create_watchlist_group(context, group)
                if not supported:
                    return WatchlistMutationResult(
                        "needs_group", action, code, group, reason="group_creation_unsupported",
                        message=message,
                    )
                if not create_ok:
                    return WatchlistMutationResult(
                        "error", action, code, group, reason="group_creation_failed",
                        message=message,
                    )
                created = True

            item_ret, items = context.get_user_security(group)
            if not self._ok(item_ret):
                return WatchlistMutationResult(
                    "error", action, code, group, group_created=created,
                    reason="group_read_failed", message=str(items)[:300],
                )
            current = {
                str(row.get("code", "")).upper()
                for row in _records(items)
                if str(row.get("code", ""))
            }
            if (add and code in current) or (not add and code not in current):
                return WatchlistMutationResult(
                    "unchanged", action, code, group, group_created=created,
                    reason="already_present" if add else "already_absent",
                )

            op_name = "ADD" if add else "MOVE_OUT"
            fallback = "ADD" if add else _enum(self.sdk, "ModifyUserSecurityOp", "DEL", "DEL")
            operation = _enum(self.sdk, "ModifyUserSecurityOp", op_name, fallback)
            ret, message = context.modify_user_security(group, operation, [code])
            if not self._ok(ret):
                return WatchlistMutationResult(
                    "error", action, code, group, group_created=created,
                    reason="modify_failed", message=str(message)[:300],
                )
            return WatchlistMutationResult(
                "success", action, code, group, changed=True,
                group_created=created, message=str(message or "")[:300],
            )
        except Exception as exc:
            return WatchlistMutationResult(
                "error", action, code, group, group_created=created,
                reason="exception", message=f"{type(exc).__name__}: {str(exc)[:250]}",
            )
        finally:
            self._close(context)

    def add_watchlist(self, symbol: str, group: str = "Options Radar") -> WatchlistMutationResult:
        return self._mutate_watchlist(symbol, group, add=True)

    def remove_watchlist(self, symbol: str, group: str = "Options Radar") -> WatchlistMutationResult:
        return self._mutate_watchlist(symbol, group, add=False)

    def sync_positions(self) -> FutuPortfolioSnapshot:
        context = self._trade_context()
        try:
            real_env = _enum(self.sdk, "TrdEnv", "REAL", "REAL")
            accounts_ret, accounts_data = context.get_acc_list()
            if not self._ok(accounts_ret):
                raise RuntimeError("get_acc_list failed")
            account_id: Optional[int] = None
            for account in _records(accounts_data):
                environment = account.get("trd_env")
                if environment != real_env and "REAL" not in str(environment).upper():
                    continue
                status = str(account.get("acc_status", "ACTIVE")).upper()
                if "DISABLED" in status:
                    continue
                authorisations = account.get("trdmarket_auth", account.get("trd_market_auth", []))
                if authorisations and not any("US" in str(item).upper() for item in authorisations):
                    continue
                try:
                    account_id = int(account["acc_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                break
            if account_id is None:
                raise RuntimeError("active REAL US trading account not found")

            funds_kwargs: Dict[str, Any] = {
                "trd_env": real_env,
                "acc_id": account_id,
                "refresh_cache": False,
            }
            currency = _enum(self.sdk, "Currency", "USD", None)
            if currency is not None:
                funds_kwargs["currency"] = currency
            funds_ret, funds_data = context.accinfo_query(**funds_kwargs)
            if not self._ok(funds_ret):
                raise RuntimeError(f"accinfo_query failed: {str(funds_data)[:300]}")
            fund_rows = _records(funds_data)
            funds = fund_rows[0] if fund_rows else {}
            nav = _float(funds, "total_assets", "usd_assets")
            cash = _float(funds, "us_cash", "cash")

            ret, data = context.position_list_query(
                position_market=_enum(self.sdk, "TrdMarket", "US", "US"),
                trd_env=real_env,
                acc_id=account_id,
                refresh_cache=False,
            )
            if not self._ok(ret):
                raise RuntimeError(f"position_list_query failed: {str(data)[:300]}")
            positions: List[FutuPosition] = []
            for row in _records(data):
                code = str(row.get("code", "")).upper()
                if not code.startswith("US."):
                    continue
                positions.append(FutuPosition(
                    code=code,
                    symbol=code.split(".", 1)[1],
                    quantity=_float(row, "qty", "quantity") or 0.0,
                    market_value=_float(row, "market_val", "market_value"),
                    cost_price=_float(row, "cost_price"),
                    nominal_price=_float(row, "nominal_price", "price"),
                    security_type=_text(row, "stock_type", "security_type"),
                    currency=_text(row, "currency"),
                ))
            gross = sum(abs(item.market_value or 0.0) for item in positions)
            # account_id is intentionally transient and is never copied into the
            # snapshot, exception text, or logs.
            return FutuPortfolioSnapshot(self._now(), positions, gross, nav=nav, cash=cash)
        finally:
            self._close(context)

    def get_option_chain(
        self,
        symbol: str,
        start: date,
        end: date,
        option_type: Optional[str] = None,
    ) -> List[FutuOptionContract]:
        if start > end:
            raise ValueError("option-chain start date must not be after end date")
        requested_kind = _normalise_option_type(option_type) if option_type else ""
        if option_type and not requested_kind:
            raise ValueError("option_type must be CALL/C or PUT/P")
        context = self._quote_context()
        try:
            kwargs: Dict[str, Any] = {
                "code": _normalise_code(symbol),
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
            if requested_kind:
                kind = "CALL" if requested_kind == "C" else "PUT"
                kwargs["option_type"] = _enum(self.sdk, "OptionType", kind, kind)
            ret, data = context.get_option_chain(**kwargs)
            if not self._ok(ret):
                raise RuntimeError(f"get_option_chain failed: {str(data)[:300]}")
            underlying = _normalise_code(symbol).split(".", 1)[1]
            contracts: List[FutuOptionContract] = []
            for row in _records(data):
                code = str(row.get("code", "")).upper()
                kind = _normalise_option_type(row.get("option_type"))
                expiry_text = _text(row, "strike_time", "expiry_date")
                strike = _float(row, "strike_price")
                if not code or not kind or not expiry_text or strike is None or (requested_kind and kind != requested_kind):
                    continue
                try:
                    expiry = date.fromisoformat(expiry_text[:10])
                except ValueError:
                    continue
                contracts.append(FutuOptionContract(
                    code=code,
                    contract_key=f"US.{underlying}|{expiry.isoformat()}|{_strike_text(strike)}|{kind}",
                    symbol=underlying,
                    expiry=expiry,
                    strike=strike,
                    option_type=kind,
                    name=_text(row, "name"),
                    lot_size=_float(row, "lot_size"),
                ))
            return contracts
        finally:
            self._close(context)

    def get_underlying_overview(self, symbols: Iterable[str]) -> Dict[str, Dict[str, Optional[float]]]:
        """Underlying-level option stats (IV rank / HV) for premium judgement."""
        codes = list(dict.fromkeys(_normalise_code(s) for s in symbols))
        if not codes:
            return {}
        context = self._quote_context()
        try:
            ret, data = context.get_option_underlying_overview(codes)
            if not self._ok(ret):
                return {}
            output: Dict[str, Dict[str, Optional[float]]] = {}
            for row in _records(data):
                code = str(row.get("code", "")).upper()
                output[code] = {
                    "iv": _float(row, "iv"),
                    "iv_rank": _float(row, "iv_rank"),
                    "iv_percentile": _float(row, "iv_percentile"),
                    "hv_30d": _float(row, "hv_30d"),
                    "call_open_interest": _float(row, "call_open_interest"),
                    "put_open_interest": _float(row, "put_open_interest"),
                }
            return output
        finally:
            self._close(context)

    def get_snapshots(self, contract_codes: Iterable[str]) -> Dict[str, FutuMarketSnapshot]:
        codes = list(dict.fromkeys(_normalise_code(code) for code in contract_codes))
        if not codes:
            return {}
        # Order-book reads must use the same long-lived connection that owns the
        # subscription.  Plain snapshots can use a short-lived context.
        self._subscription_lock.acquire()
        try:
            persistent = self._subscription_context is not None
            context = self._subscription_context if persistent else self._quote_context()
        except Exception:
            self._subscription_lock.release()
            raise
        output: Dict[str, FutuMarketSnapshot] = {}
        try:
            for offset in range(0, len(codes), self.snapshot_batch_size):
                ret, data = context.get_market_snapshot(codes[offset:offset + self.snapshot_batch_size])
                if not self._ok(ret):
                    raise RuntimeError(f"get_market_snapshot failed: {str(data)[:300]}")
                received_at = self._now()
                for row in _records(data):
                    code = str(row.get("code", "")).upper()
                    book_bid: Optional[float] = None
                    book_ask: Optional[float] = None
                    book_timestamp: Optional[datetime] = None
                    if code in self._active_codes and hasattr(context, "get_order_book"):
                        book_ret, book = context.get_order_book(code, num=1)
                        if self._ok(book_ret) and isinstance(book, Mapping):
                            bids, asks = book.get("Bid") or [], book.get("Ask") or []
                            if bids:
                                try:
                                    book_bid = float(bids[0][0])
                                except (TypeError, ValueError, IndexError):
                                    pass
                            if asks:
                                try:
                                    book_ask = float(asks[0][0])
                                except (TypeError, ValueError, IndexError):
                                    pass
                            book_timestamp = _timestamp(
                                book.get("svr_recv_time_bid_timestamp")
                                or book.get("svr_recv_time_ask_timestamp")
                                or book.get("svr_recv_time_bid")
                                or book.get("svr_recv_time_ask")
                            )
                    fields = {
                        "bid": book_bid if book_bid is not None else _float(row, "bid_price"),
                        "ask": book_ask if book_ask is not None else _float(row, "ask_price"),
                        "last": _float(row, "last_price"), "volume": _float(row, "volume"),
                        "open_interest": _float(row, "option_open_interest", "open_interest"),
                        "implied_volatility": _float(row, "option_implied_volatility", "implied_volatility"),
                        "delta": _float(row, "option_delta", "delta"),
                        "gamma": _float(row, "option_gamma", "gamma"),
                        "vega": _float(row, "option_vega", "vega"),
                        "theta": _float(row, "option_theta", "theta"),
                        "rho": _float(row, "option_rho", "rho"),
                        "underlying_price": _float(row, "option_owner_price", "stock_owner_price"),
                    }
                    output[code] = FutuMarketSnapshot(
                        code=code,
                        observed_at=received_at,
                        market_timestamp=book_timestamp or _timestamp(row.get("update_time") or row.get("data_timestamp")),
                        bid=fields["bid"], ask=fields["ask"], last=fields["last"],
                        volume=fields["volume"], open_interest=fields["open_interest"],
                        implied_volatility=fields["implied_volatility"], delta=fields["delta"],
                        gamma=fields["gamma"], vega=fields["vega"], theta=fields["theta"],
                        rho=fields["rho"], underlying_price=fields["underlying_price"],
                        data_type=_text(row, "data_type"),
                        field_quality={name: ("native" if value is not None else "missing") for name, value in fields.items()},
                    )
            return output
        finally:
            try:
                if not persistent:
                    self._close(context)
            finally:
                self._subscription_lock.release()

    def subscribe_candidates(self, contract_codes: Iterable[str]) -> SubscriptionResult:
        """Make the supplied set the active QUOTE + ORDER_BOOK subscription set."""
        desired = {_normalise_code(code) for code in contract_codes}
        with self._subscription_lock:
            if self._subscription_context is None:
                self._subscription_context = self._quote_context()
            context = self._subscription_context
            subtypes = [
                _enum(self.sdk, "SubType", "QUOTE", "QUOTE"),
                _enum(self.sdk, "SubType", "ORDER_BOOK", "ORDER_BOOK"),
            ]
            removed = sorted(self._active_codes - desired)
            added = sorted(desired - self._active_codes)
            if removed:
                ret, message = context.unsubscribe(removed, subtypes)
                if not self._ok(ret):
                    return SubscriptionResult(sorted(self._active_codes), [], [], "error", str(message)[:300])
                self._active_codes.difference_update(removed)
            if added:
                ret, message = context.subscribe(
                    added, subtypes, is_first_push=False, subscribe_push=False
                )
                if not self._ok(ret):
                    return SubscriptionResult(sorted(self._active_codes), [], removed, "error", str(message)[:300])
                self._active_codes.update(added)
            return SubscriptionResult(sorted(self._active_codes), added, removed, "ready")

    def get_history(
        self,
        code: str,
        start: date,
        end: date,
        interval: str = "K_5M",
        *,
        max_count: int = 1000,
        max_pages: int = 100,
    ) -> List[FutuHistoryBar]:
        context = self._quote_context()
        bars: List[FutuHistoryBar] = []
        page_key: Any = None
        ktype = _enum(self.sdk, "KLType", interval, interval)
        autype = _enum(self.sdk, "AuType", "NONE", "NONE")
        normalised_code = _normalise_code(code)
        try:
            for _ in range(max(1, max_pages)):
                result = context.request_history_kline(
                    normalised_code,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    ktype=ktype,
                    autype=autype,
                    max_count=max(1, min(int(max_count), 1000)),
                    page_req_key=page_key,
                )
                if not isinstance(result, tuple) or len(result) < 3:
                    raise RuntimeError("request_history_kline returned an unexpected response")
                ret, data, next_key = result[0], result[1], result[2]
                if not self._ok(ret):
                    raise RuntimeError(f"request_history_kline failed: {str(data)[:300]}")
                for row in _records(data):
                    timestamp = _timestamp(row.get("time_key") or row.get("timestamp"))
                    if timestamp is None:
                        continue
                    bars.append(FutuHistoryBar(
                        code=str(row.get("code", normalised_code)).upper(), timestamp=timestamp,
                        open=_float(row, "open"), high=_float(row, "high"),
                        low=_float(row, "low"), close=_float(row, "close"),
                        volume=_float(row, "volume"), turnover=_float(row, "turnover"),
                        interval=interval,
                    ))
                if next_key is None:
                    break
                page_key = next_key
            return bars
        finally:
            self._close(context)

    def close(self) -> None:
        with self._subscription_lock:
            if self._subscription_context is not None:
                self._close(self._subscription_context)
                self._subscription_context = None
            self._active_codes.clear()

    def __enter__(self) -> "FutuProvider":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
