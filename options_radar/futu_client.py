from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from .models import MarketSnapshot, PortfolioContext


def _number(row, names: Iterable[str]) -> Optional[float]:
    for name in names:
        if name in row and row[name] not in (None, "", "N/A"):
            try:
                return float(row[name])
            except (TypeError, ValueError):
                pass
    return None


class FutuReadOnlyClient:
    """Read-only adapter. It never imports or calls order placement methods."""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111, security_firm: str = "FUTUINC"):
        self.host = host
        self.port = int(port)
        self.security_firm = security_firm

    @staticmethod
    def _sdk():
        try:
            import futu  # type: ignore
            return futu
        except ImportError as exc:
            raise RuntimeError("futu-api package is not installed; install requirements.txt") from exc

    def sync_portfolio(self) -> Tuple[Dict[str, PortfolioContext], List[Dict[str, object]]]:
        futu = self._sdk()
        quote_ctx = futu.OpenQuoteContext(host=self.host, port=self.port)
        raw_records: List[Dict[str, object]] = []
        watch_symbols = set()
        try:
            ret, groups = quote_ctx.get_user_security_group(group_type=futu.UserSecurityGroupType.ALL)
            if ret == futu.RET_OK:
                for _, group in groups.iterrows():
                    group_name = str(group["group_name"])
                    ret_items, items = quote_ctx.get_user_security(group_name)
                    if ret_items != futu.RET_OK:
                        continue
                    for _, item in items.iterrows():
                        code = str(item.get("code", ""))
                        if code.startswith("US."):
                            watch_symbols.add(code.split(".", 1)[1])
                            raw_records.append({"kind": "watchlist", "group": group_name, "code": code})
        finally:
            quote_ctx.close()

        held: Dict[str, float] = {}
        market_values: Dict[str, float] = {}
        try:
            firm = getattr(futu.SecurityFirm, self.security_firm, futu.SecurityFirm.FUTUINC)
            trade_ctx = futu.OpenSecTradeContext(
                filter_trdmarket=futu.TrdMarket.US,
                host=self.host,
                port=self.port,
                security_firm=firm,
            )
            try:
                ret, positions = trade_ctx.position_list_query(
                    trd_env=futu.TrdEnv.REAL,
                    position_market=futu.TrdMarket.US,
                    refresh_cache=False,
                )
                if ret == futu.RET_OK:
                    for _, row in positions.iterrows():
                        code = str(row.get("code", ""))
                        if not code.startswith("US."):
                            continue
                        symbol = code.split(".", 1)[1]
                        quantity = float(row.get("qty", 0.0) or 0.0)
                        market_value = float(row.get("market_val", 0.0) or 0.0)
                        held[symbol] = held.get(symbol, 0.0) + quantity
                        market_values[symbol] = market_values.get(symbol, 0.0) + abs(market_value)
                        raw_records.append({"kind": "position", "code": code, "qty": quantity, "market_value": market_value})
            finally:
                trade_ctx.close()
        except Exception as exc:
            raw_records.append({"kind": "status", "positions": "degraded", "error": str(exc)[:300]})

        total_value = sum(market_values.values())
        contexts: Dict[str, PortfolioContext] = {}
        for symbol in watch_symbols | set(held):
            contexts[symbol] = PortfolioContext(
                symbol=symbol,
                in_watchlist=symbol in watch_symbols,
                held_quantity=held.get(symbol, 0.0),
                concentration=(market_values.get(symbol, 0.0) / total_value) if total_value else 0.0,
            )
        return contexts, raw_records

    @staticmethod
    def split_contract_key(key: str) -> Tuple[str, date, float, str]:
        symbol_part, expiry_text, strike_text, option_type = key.split("|")
        return symbol_part.split(".", 1)[1], date.fromisoformat(expiry_text), float(strike_text), option_type

    def exact_snapshot(self, key: str) -> MarketSnapshot:
        symbol, expiry, strike, option_type = self.split_contract_key(key)
        futu = self._sdk()
        quote_ctx = futu.OpenQuoteContext(host=self.host, port=self.port)
        now = datetime.utcnow()
        try:
            ret, chain = quote_ctx.get_option_chain(
                code=f"US.{symbol}", start=expiry.isoformat(), end=expiry.isoformat()
            )
            if ret != futu.RET_OK:
                return MarketSnapshot(contract_key=key, observed_at=now, data_status=f"chain_error:{str(chain)[:100]}")
            matches = chain[
                (chain["strike_price"].astype(float).sub(strike).abs() < 0.0001)
                & (chain["option_type"].astype(str).str.upper().str.startswith(option_type))
            ]
            if matches.empty:
                return MarketSnapshot(contract_key=key, observed_at=now, data_status="contract_not_found")
            code = str(matches.iloc[0]["code"])
            ret, frame = quote_ctx.get_market_snapshot([code])
            if ret != futu.RET_OK or frame.empty:
                return MarketSnapshot(contract_key=key, observed_at=now, futu_code=code, data_status="snapshot_error")
            row = frame.iloc[0]
            return MarketSnapshot(
                contract_key=key,
                observed_at=now,
                futu_code=code,
                bid=_number(row, ["bid_price"]),
                ask=_number(row, ["ask_price"]),
                last=_number(row, ["last_price"]),
                volume=_number(row, ["volume"]),
                open_interest=_number(row, ["option_open_interest", "open_interest"]),
                implied_volatility=_number(row, ["option_implied_volatility", "implied_volatility"]),
                delta=_number(row, ["option_delta", "delta"]),
                underlying_price=_number(row, ["option_owner_price", "stock_owner_price"]),
                data_status="ok",
            )
        except Exception as exc:
            return MarketSnapshot(contract_key=key, observed_at=now, data_status=f"error:{type(exc).__name__}:{str(exc)[:100]}")
        finally:
            quote_ctx.close()

    def execution_candidates(
        self,
        symbol: str,
        direction: str,
        min_dte: int = 14,
        max_dte: int = 90,
    ) -> List[MarketSnapshot]:
        futu = self._sdk()
        option_type = "CALL" if direction == "BULL" else "PUT"
        start = date.today() + timedelta(days=min_dte)
        end = date.today() + timedelta(days=max_dte)
        quote_ctx = futu.OpenQuoteContext(host=self.host, port=self.port)
        snapshots: List[MarketSnapshot] = []
        try:
            ret, chain = quote_ctx.get_option_chain(
                code=f"US.{symbol}", start=start.isoformat(), end=end.isoformat()
            )
            if ret != futu.RET_OK or chain.empty:
                return snapshots
            chain = chain[chain["option_type"].astype(str).str.upper().str.startswith(option_type[0])]
            codes = [str(code) for code in chain["code"].tolist()[:300]]
            for offset in range(0, len(codes), 100):
                ret, frame = quote_ctx.get_market_snapshot(codes[offset:offset + 100])
                if ret != futu.RET_OK:
                    continue
                for _, row in frame.iterrows():
                    code = str(row.get("code", ""))
                    chain_row = chain[chain["code"] == code]
                    if chain_row.empty:
                        continue
                    meta = chain_row.iloc[0]
                    expiry = date.fromisoformat(str(meta["strike_time"])[:10])
                    strike = float(meta["strike_price"])
                    key = f"US.{symbol}|{expiry.isoformat()}|{str(strike).rstrip('0').rstrip('.')}|{option_type[0]}"
                    snapshots.append(MarketSnapshot(
                        contract_key=key, observed_at=datetime.utcnow(), futu_code=code,
                        bid=_number(row, ["bid_price"]), ask=_number(row, ["ask_price"]),
                        last=_number(row, ["last_price"]), volume=_number(row, ["volume"]),
                        open_interest=_number(row, ["option_open_interest", "open_interest"]),
                        implied_volatility=_number(row, ["option_implied_volatility", "implied_volatility"]),
                        delta=_number(row, ["option_delta", "delta"]),
                        underlying_price=_number(row, ["option_owner_price", "stock_owner_price"]),
                        data_status="ok",
                    ))
            return snapshots
        finally:
            quote_ctx.close()
