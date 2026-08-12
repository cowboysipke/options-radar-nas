from __future__ import annotations

import csv
import io
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple

from .models import BrokerSnapshot, PortfolioContext


HttpGet = Callable[[str, float], str]


def read_secret(env_name: str, file_env_name: str = "") -> str:
    value = os.getenv(env_name, "").strip()
    if value:
        return value
    path = os.getenv(file_env_name or f"{env_name}_FILE", "").strip()
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip()
    return ""


def _default_get(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "options-radar/0.2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _float(value: object) -> Optional[float]:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _timestamp(value: str) -> datetime:
    value = (value or "").strip()
    for pattern in ("%Y%m%d;%H%M%S", "%Y%m%d", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], pattern)
        except ValueError:
            continue
    return datetime.now(timezone.utc).replace(tzinfo=None)


class IBKRFlexClient:
    """Read-only IBKR Flex Web Service adapter.

    Only the token and pre-created query id are accepted. Account identifiers
    and holder names are discarded while parsing.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        query_id: Optional[str] = None,
        base_url: str = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService",
        timeout: float = 30.0,
        poll_seconds: float = 2.0,
        max_polls: int = 10,
        http_get: Optional[HttpGet] = None,
    ):
        self.token = token if token is not None else read_secret("IBKR_FLEX_TOKEN")
        self.query_id = query_id if query_id is not None else read_secret("IBKR_FLEX_QUERY_ID")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self.max_polls = max_polls
        self.http_get = http_get or _default_get

    @property
    def configured(self) -> bool:
        return bool(self.token and self.query_id)

    def _url(self, endpoint: str, params: Dict[str, str]) -> str:
        return f"{self.base_url}/{endpoint}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def _reference(payload: str) -> Tuple[str, str]:
        root = ET.fromstring(payload)
        status = (root.findtext("Status") or "").strip()
        code = (root.findtext("ReferenceCode") or "").strip()
        url = (root.findtext("Url") or "").strip()
        if status and status.lower() != "success":
            error = (root.findtext("ErrorMessage") or root.findtext("ErrorCode") or status).strip()
            raise RuntimeError(f"IBKR Flex request error: {error[:160]}")
        if not code:
            raise RuntimeError("IBKR Flex response has no reference code")
        return code, url

    def fetch(self) -> BrokerSnapshot:
        if not self.configured:
            return BrokerSnapshot(
                as_of=datetime.utcnow(), nav=None, cash=None, positions={},
                source="ibkr_flex", quality="missing",
            )
        response = self.http_get(self._url("SendRequest", {
            "t": self.token, "q": self.query_id, "v": "3",
        }), self.timeout)
        reference, download_url = self._reference(response)
        if download_url:
            separator = "&" if "?" in download_url else "?"
            endpoint = f"{download_url}{separator}{urllib.parse.urlencode({'t': self.token, 'q': reference, 'v': '3'})}"
        else:
            endpoint = self._url("GetStatement", {"t": self.token, "q": reference, "v": "3"})
        for attempt in range(self.max_polls):
            payload = self.http_get(endpoint, self.timeout)
            if "FlexStatementResponse" in payload or payload.lstrip().startswith("AccountId"):
                return self.parse_statement(payload)
            if "Statement generation in progress" not in payload and "1019" not in payload:
                try:
                    root = ET.fromstring(payload)
                    error = root.findtext("ErrorMessage") or root.findtext("ErrorCode")
                except ET.ParseError:
                    error = payload[:160]
                raise RuntimeError(f"IBKR Flex download error: {error}")
            if attempt + 1 < self.max_polls and self.poll_seconds:
                time.sleep(self.poll_seconds)
        raise RuntimeError("IBKR Flex statement polling timed out")

    @staticmethod
    def parse_statement(payload: str) -> BrokerSnapshot:
        if payload.lstrip().startswith("<"):
            return IBKRFlexClient._parse_xml(payload)
        return IBKRFlexClient._parse_csv(payload)

    @staticmethod
    def _parse_xml(payload: str) -> BrokerSnapshot:
        root = ET.fromstring(payload)
        statement = root.find(".//FlexStatement")
        as_of = _timestamp(statement.attrib.get("toDate", "") if statement is not None else "")
        nav = None
        cash = None
        for element in root.findall(".//ChangeInNAV"):
            nav = _float(element.attrib.get("endingValue") or element.attrib.get("endingNAV")) or nav
        for element in root.findall(".//EquitySummaryInBase"):
            nav = _float(element.attrib.get("total") or element.attrib.get("endingCash")) or nav
        for element in root.findall(".//CashReportCurrency"):
            if element.attrib.get("currency", "BASE").upper() in {"BASE", "USD"}:
                cash = _float(element.attrib.get("endingCash") or element.attrib.get("endingSettledCash")) or cash

        positions: Dict[str, Dict[str, float]] = {}
        for element in root.findall(".//OpenPosition"):
            symbol = (element.attrib.get("symbol") or element.attrib.get("underlyingSymbol") or "").upper().strip()
            asset = (element.attrib.get("assetCategory") or "STK").upper()
            if not symbol or asset not in {"STK", "OPT"}:
                continue
            quantity = _float(element.attrib.get("position")) or 0.0
            value = _float(element.attrib.get("positionValue") or element.attrib.get("markPrice"))
            cost = _float(element.attrib.get("costBasisMoney") or element.attrib.get("costBasisPrice"))
            key = symbol if asset == "STK" else (element.attrib.get("description") or symbol).strip()
            positions[key] = {
                "quantity": quantity,
                "market_value": value or 0.0,
                "cost_basis": cost or 0.0,
                "asset_is_option": 1.0 if asset == "OPT" else 0.0,
            }
        quality = "native" if nav is not None or positions else "missing"
        return BrokerSnapshot(as_of=as_of, nav=nav, cash=cash, positions=positions, quality=quality)

    @staticmethod
    def _parse_csv(payload: str) -> BrokerSnapshot:
        rows = list(csv.DictReader(io.StringIO(payload)))
        positions: Dict[str, Dict[str, float]] = {}
        nav = None
        cash = None
        as_of = datetime.utcnow()
        for row in rows:
            symbol = (row.get("Symbol") or row.get("UnderlyingSymbol") or "").upper().strip()
            if symbol:
                positions[symbol] = {
                    "quantity": _float(row.get("Position")) or 0.0,
                    "market_value": _float(row.get("PositionValue")) or 0.0,
                    "cost_basis": _float(row.get("CostBasisMoney")) or 0.0,
                    "asset_is_option": 1.0 if (row.get("AssetCategory") == "OPT") else 0.0,
                }
            nav = _float(row.get("EndingNAV") or row.get("EndingValue")) or nav
            cash = _float(row.get("EndingCash")) or cash
            if row.get("ToDate"):
                as_of = _timestamp(row["ToDate"])
        return BrokerSnapshot(
            as_of=as_of, nav=nav, cash=cash, positions=positions,
            quality="native" if nav is not None or positions else "missing",
        )

    @staticmethod
    def portfolio_contexts(
        snapshot: BrokerSnapshot, watchlist: Iterable[str]
    ) -> Dict[str, PortfolioContext]:
        watch = {symbol.upper() for symbol in watchlist}
        stock_positions = {
            symbol: values for symbol, values in snapshot.positions.items()
            if values.get("asset_is_option", 0.0) == 0.0
        }
        total = sum(abs(values.get("market_value", 0.0)) for values in stock_positions.values())
        result: Dict[str, PortfolioContext] = {}
        for symbol in watch | set(stock_positions):
            values = stock_positions.get(symbol, {})
            market_value = abs(values.get("market_value", 0.0))
            result[symbol] = PortfolioContext(
                symbol=symbol, in_watchlist=symbol in watch,
                held_quantity=values.get("quantity", 0.0),
                concentration=market_value / total if total else 0.0,
                nav=snapshot.nav, snapshot_at=snapshot.as_of,
            )
        return result
