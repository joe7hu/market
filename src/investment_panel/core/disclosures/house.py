"""Auto-split from core/disclosures.py — see ARCHITECTURE.md."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from investment_panel.core.house_disclosures import (
    fetch_house_pdf_text,
    parse_house_disclosure_text,
    search_house_member_filings,
)

from investment_panel.core.disclosures.prices import ensure_disclosure_symbol_prices
from investment_panel.core.disclosures.public_csv import ingest_public_disclosure_csvs, normalize_public_disclosure_transaction, upsert_public_disclosure_transaction
from investment_panel.core.disclosures.replica import rebuild_trader_replica_portfolios


def backfill_trader_disclosure_history(
    con: Any,
    trader: dict[str, Any],
    replace: bool = True,
) -> dict[str, int | str]:
    trader_name = str(trader["trader_name"])
    if replace:
        delete_trader_disclosure_rows(con, trader_name)
    official_result = ingest_official_house_disclosures_for_trader(con, trader)
    sources = list(trader.get("historical_csvs") or []) + list(trader.get("daily_csvs") or [])
    ingest_result = ingest_public_disclosure_csvs(con, sources)
    price_result = ensure_disclosure_symbol_prices(con, trader_names=[trader_name])
    rebuild_result = rebuild_trader_replica_portfolios(con, trader_names=[trader_name])
    return {
        "trader_name": trader_name,
        "replace": int(replace),
        "historical_files_configured": len(trader.get("historical_csvs") or []),
        "daily_files_configured": len(trader.get("daily_csvs") or []),
        **official_result,
        **ingest_result,
        **price_result,
        **rebuild_result,
    }


def ingest_official_house_disclosures_for_trader(con: Any, trader: dict[str, Any]) -> dict[str, int]:
    house = trader.get("official_house") or {}
    if not house:
        return {"official_house_filings_found": 0, "official_house_filings_ingested": 0, "official_house_rows_ingested": 0}
    filings = search_house_member_filings(
        house.get("last_name") or trader["trader_name"].split()[-1],
        int(house.get("start_year") or 2008),
        int(house.get("end_year") or date.today().year),
        house.get("user_agent") or "joehu-market-panel/0.1 contact:local",
        state=house.get("state"),
        district=str(house.get("district")) if house.get("district") else None,
    )
    wanted_types = set(house.get("filing_types") or ["PTR Original", "FD Original"])
    rows_ingested = 0
    filings_ingested = 0
    for filing in filings:
        if wanted_types and filing.get("filing_type") not in wanted_types:
            continue
        text = fetch_house_pdf_text(filing["url"], house.get("user_agent") or "joehu-market-panel/0.1 contact:local")
        for row in parse_house_disclosure_text(text, filing, trader["trader_name"]):
            normalized = normalize_public_disclosure_transaction(row, {"trader_name": trader["trader_name"], "filer_name": filing.get("name")})
            if not normalized:
                continue
            upsert_public_disclosure_transaction(con, normalized)
            rows_ingested += 1
        filings_ingested += 1
    return {
        "official_house_filings_found": len(filings),
        "official_house_filings_ingested": filings_ingested,
        "official_house_rows_ingested": rows_ingested,
    }


def delete_trader_disclosure_rows(con: Any, trader_name: str) -> None:
    con.execute(
        """
        DELETE FROM disclosures
        WHERE trader_name = ?
          AND source_type IN ('public_disclosure_transaction', 'trader_portfolio_model')
        """,
        [trader_name],
    )
