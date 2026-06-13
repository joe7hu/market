"""Auto-split from core/disclosures.py — see ARCHITECTURE.md."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any
from investment_panel.core.db import json_dumps

from investment_panel.core.disclosures.coerce import _float_or_none, amount_midpoint, disclosure_amount_range
from investment_panel.core.disclosures.constants import PUBLIC_DISCLOSURE_CAVEAT, stable_id


def ingest_public_disclosure_csvs(con: Any, sources: list[dict[str, Any]]) -> dict[str, int]:
    files_checked = 0
    rows_ingested = 0
    for source in sources:
        path = Path(source["path"])
        files_checked += 1
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                normalized = normalize_public_disclosure_transaction(row, source)
                if not normalized:
                    continue
                upsert_public_disclosure_transaction(con, normalized)
                rows_ingested += 1
    return {"public_disclosure_files_checked": files_checked, "public_disclosure_rows_ingested": rows_ingested}


def normalize_public_disclosure_transaction(row: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
    transaction_date = row.get("transaction_date") or row.get("event_date") or row.get("date")
    transaction_type = str(row.get("transaction_type") or row.get("type") or row.get("action") or "").strip().upper()
    if not symbol or not transaction_date or not transaction_type:
        return None
    amount_min, amount_max = disclosure_amount_range(row)
    raw = {
        "source_type": "public_disclosure_transaction",
        "asset_name": row.get("asset_name") or row.get("security") or row.get("name"),
        "owner": row.get("owner"),
        "disclosure_type": row.get("disclosure_type") or row.get("form") or "public_disclosure",
        "transaction_type": transaction_type,
        "transaction_date": transaction_date,
        "filed_date": row.get("filed_date") or row.get("filing_date"),
        "amount_min": amount_min,
        "amount_max": amount_max,
        "amount_mid": amount_midpoint(amount_min, amount_max),
        "amount_raw": row.get("amount") or row.get("amount_range"),
        "source_url": row.get("source_url") or row.get("url"),
        "asset_type": row.get("asset_type"),
        "comment": row.get("comment"),
        "shares": _float_or_none(row.get("shares")),
        "contracts": _float_or_none(row.get("contracts")),
        "source_document_id": row.get("source_document_id"),
        "source_file": source.get("path"),
        "methodology": "Normalize each disclosed transaction, estimate notional from the disclosed range midpoint, then build a replica portfolio with local price history.",
        "source_caveat": PUBLIC_DISCLOSURE_CAVEAT,
    }
    return {
        "id": row.get("id")
        or stable_id(
            ":".join(
                [
                    str(source.get("trader_name")),
                    symbol,
                    str(transaction_date),
                    transaction_type,
                    str(row.get("amount") or row.get("amount_range") or ""),
                    str(row.get("source_url") or row.get("url") or row.get("source_document_id") or ""),
                ]
            )
        ),
        "source_type": "public_disclosure_transaction",
        "trader_name": row.get("trader_name") or source.get("trader_name"),
        "filer_name": row.get("filer_name") or source.get("filer_name"),
        "symbol": symbol,
        "event_date": transaction_date,
        "filed_date": row.get("filed_date") or row.get("filing_date") or transaction_date,
        "action": transaction_type,
        "amount": row.get("amount") or row.get("amount_range") or str(raw["amount_mid"] or ""),
        "raw": raw,
        "source_url": raw["source_url"],
    }


def upsert_public_disclosure_transaction(con: Any, row: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO disclosures
        (id, source_type, trader_name, filer_name, symbol, event_date, filed_date, action, amount, raw, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row["id"],
            row["source_type"],
            row["trader_name"],
            row["filer_name"],
            row["symbol"],
            row["event_date"],
            row["filed_date"],
            row["action"],
            row["amount"],
            json_dumps(row["raw"]),
            row["source_url"],
        ],
    )
