from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .db import Database
from .futu_client import FutuReadOnlyClient


def migrate_futu_watchlist(
    database: Database, host: str = "127.0.0.1", port: int = 11111
) -> List[str]:
    """One-time, read-only migration from local Futu OpenD to cloud watchlist."""
    client = FutuReadOnlyClient(host=host, port=port)
    contexts, _ = client.sync_portfolio()
    symbols = sorted(symbol for symbol, context in contexts.items() if context.in_watchlist)
    for symbol in symbols:
        database.upsert_watchlist(symbol, source="futu_migration", group_name="富途导入")
    return symbols


def import_watchlist_csv(database: Database, path: Path) -> List[str]:
    """NAS-friendly migration when OpenD runs on another computer.

    Accepts a CSV with a Symbol/Code/Ticker column, so the computer only needs
    to be opened once to export the list.
    """
    symbols: List[str] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            value = row.get("Symbol") or row.get("Code") or row.get("Ticker") or ""
            symbol = str(value).strip().upper().replace("US.", "")
            if symbol and symbol not in symbols:
                database.upsert_watchlist(symbol, source="csv_migration", group_name="富途导入")
                symbols.append(symbol)
    return symbols
