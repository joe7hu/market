"""Disclosure ingestion for SEC 13F filings and public trader disclosures."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

from investment_panel.core import sec
from investment_panel.core.config import resolve_path
from investment_panel.core.db import json_dumps
from investment_panel.core.house_disclosures import (
    fetch_house_pdf_text,
    parse_house_disclosure_text,
    search_house_member_filings,
)
from investment_panel.core.prices import fetch_prices, upsert_prices


THIRTEEN_F_FORMS = {"13F-HR", "13F-HR/A"}
THIRTEEN_F_CAVEAT = (
    "Form 13F is a delayed quarterly disclosure, generally due up to 45 days after quarter end; "
    "it reports long positions in covered US securities as of the report date and does not show "
    "current holdings, shorts, many derivatives, cost basis, or full trade intent."
)
PUBLIC_DISCLOSURE_CAVEAT = (
    "Replica portfolios are deterministic estimates from public disclosure records. Congressional disclosures "
    "often report amount ranges, delayed filing dates, and transaction intent rather than exact live positions."
)


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def load_13f_trackers_from_config(config_path: str | Path | None = None) -> list[dict[str, Any]]:
    path = resolve_path(config_path or "config.yaml")
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return extract_13f_trackers(raw)


def extract_13f_trackers(raw_config: dict[str, Any]) -> list[dict[str, Any]]:
    disclosures = raw_config.get("disclosures") or {}
    global_ticker_map = normalize_13f_ticker_map(
        disclosures.get("13f_ticker_map")
        or disclosures.get("thirteen_f_ticker_map")
        or raw_config.get("13f_ticker_map")
        or raw_config.get("thirteen_f_ticker_map")
        or {}
    )
    tracker_rows = (
        disclosures.get("13f_trackers")
        or disclosures.get("thirteen_f_trackers")
        or raw_config.get("13f_trackers")
        or raw_config.get("thirteen_f_trackers")
        or []
    )
    trackers: list[dict[str, Any]] = []
    for row in tracker_rows:
        if not isinstance(row, dict) or not row.get("cik"):
            continue
        cik = str(row["cik"]).strip()
        trackers.append(
            {
                "cik": cik,
                "name": row.get("name") or row.get("trader_name") or row.get("filer_name") or cik,
                "max_filings": row.get("max_filings"),
                "ticker_map": global_ticker_map | normalize_13f_ticker_map(row.get("ticker_map") or row.get("cusip_ticker_map") or {}),
            }
        )
    return trackers


def normalize_13f_ticker_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        cusip = normalize_cusip(key)
        symbol = str(value or "").strip().upper()
        if cusip and symbol:
            normalized[cusip] = symbol
    return normalized


def normalize_cusip(value: Any) -> str:
    return str(value or "").strip().upper().replace(" ", "")


def load_public_disclosure_csvs_from_config(config_path: str | Path | None = None) -> list[dict[str, Any]]:
    path = resolve_path(config_path or "config.yaml")
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return extract_public_disclosure_csvs(raw, path.parent)


def load_tracked_traders_from_config(config_path: str | Path | None = None) -> list[dict[str, Any]]:
    path = resolve_path(config_path or "config.yaml")
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return extract_tracked_traders(raw, path.parent)


def extract_public_disclosure_csvs(raw_config: dict[str, Any], base: Path | None = None) -> list[dict[str, Any]]:
    disclosures = raw_config.get("disclosures") or {}
    source_rows = disclosures.get("public_disclosure_csvs") or raw_config.get("public_disclosure_csvs") or []
    sources = disclosure_csv_sources(source_rows, base or Path.cwd())
    for trader in extract_tracked_traders(raw_config, base):
        sources.extend(trader.get("daily_csvs", []))
    return sources


def extract_tracked_traders(raw_config: dict[str, Any], base: Path | None = None) -> list[dict[str, Any]]:
    disclosures = raw_config.get("disclosures") or {}
    trader_rows = disclosures.get("tracked_traders") or raw_config.get("tracked_traders") or []
    traders: list[dict[str, Any]] = []
    root = base or Path.cwd()
    for row in trader_rows:
        if not isinstance(row, dict) or not (row.get("name") or row.get("trader_name")):
            continue
        trader_name = str(row.get("trader_name") or row.get("name")).strip()
        filer_name = row.get("filer_name") or row.get("source_name") or trader_name
        default_source = {
            "trader_name": trader_name,
            "filer_name": filer_name,
            "source_kind": row.get("source_kind") or row.get("source_type") or "public_disclosure",
        }
        traders.append(
            {
                "trader_name": trader_name,
                "filer_name": filer_name,
                "source_kind": default_source["source_kind"],
                "official_house": row.get("official_house") or row.get("house") or {},
                "benchmark": row.get("benchmark") or {},
                "historical_csvs": disclosure_csv_sources(row.get("historical_csvs") or [], root, default_source),
                "daily_csvs": disclosure_csv_sources(row.get("daily_csvs") or row.get("incremental_csvs") or [], root, default_source),
            }
        )
    return traders


def disclosure_csv_sources(
    source_rows: list[Any],
    root: Path,
    defaults: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for row in source_rows:
        if isinstance(row, str):
            row = {"path": row}
        if not isinstance(row, dict) or not row.get("path"):
            continue
        row_defaults = defaults or {}
        sources.append(
            {
                "path": str(resolve_path(row["path"], root)),
                "trader_name": row.get("trader_name") or row.get("name") or row_defaults.get("trader_name") or "Tracked Trader",
                "source_type": row.get("source_type") or "public_disclosure_transaction",
                "filer_name": row.get("filer_name")
                or row.get("name")
                or row.get("trader_name")
                or row_defaults.get("filer_name")
                or "Public disclosure",
                "source_kind": row.get("source_kind") or row_defaults.get("source_kind") or "public_disclosure",
            }
        )
    return sources


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


def ensure_disclosure_symbol_prices(
    con: Any,
    trader_names: list[str] | None = None,
    lookback_days: int = 900,
    mode: str = "online",
) -> dict[str, Any]:
    params: list[Any] = []
    filter_sql = ""
    if trader_names:
        placeholders = ", ".join(["?"] * len(trader_names))
        filter_sql = f" AND trader_name IN ({placeholders})"
        params.extend(trader_names)
    rows = con.execute(
        f"""
        SELECT symbol, min(event_date) AS earliest_event_date
        FROM disclosures
        WHERE source_type = 'public_disclosure_transaction'
          AND symbol IS NOT NULL
          {filter_sql}
        GROUP BY symbol
        ORDER BY symbol
        """,
        params,
    ).fetchall()
    fetched = 0
    price_rows = 0
    price_errors: dict[str, str] = {}
    for symbol, earliest_event_date in rows:
        earliest_needed = str(earliest_event_date)[:10]
        existing = con.execute(
            "SELECT min(date), count(*) FROM prices_daily WHERE symbol = ?",
            [symbol],
        ).fetchone()
        if existing and existing[1] and str(existing[0]) <= earliest_needed:
            continue
        symbol_lookback_days = max(lookback_days, days_since(earliest_needed) + 14)
        try:
            frame = fetch_prices(symbol, symbol_lookback_days, mode)
        except Exception as exc:
            price_errors[symbol] = f"{type(exc).__name__}: {exc}"
            continue
        price_rows += upsert_prices(con, frame)
        fetched += 1
    return {"price_symbols_fetched": fetched, "price_rows_ingested": price_rows, "price_errors": price_errors}


def days_since(value: str) -> int:
    return max(1, (date.today() - datetime.strptime(value[:10], "%Y-%m-%d").date()).days)


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


def rebuild_trader_replica_portfolios(con: Any, trader_names: list[str] | None = None) -> dict[str, int]:
    params: list[Any] = []
    filter_sql = ""
    if trader_names:
        placeholders = ", ".join(["?"] * len(trader_names))
        filter_sql = f" AND trader_name IN ({placeholders})"
        params.extend(trader_names)
    rows = con.execute(
        f"""
        SELECT trader_name, filer_name, symbol, event_date, filed_date, action, raw, source_url
        FROM disclosures
        WHERE source_type = 'public_disclosure_transaction'
        {filter_sql}
        ORDER BY trader_name, event_date, filed_date, symbol
        """,
        params,
    ).fetchall()
    columns = [column[0] for column in con.description]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for values in rows:
        row = dict(zip(columns, values))
        row["raw"] = yaml.safe_load(row["raw"]) if isinstance(row.get("raw"), str) and row.get("raw") else row.get("raw")
        grouped.setdefault(str(row["trader_name"]), []).append(row)

    built = 0
    for trader_name, transactions in grouped.items():
        snapshot = build_replica_portfolio_snapshot(con, trader_name, transactions)
        upsert_replica_portfolio_snapshot(con, snapshot)
        built += 1
    return {"trader_replica_portfolios_built": built}


def build_replica_portfolio_snapshot(con: Any, trader_name: str, transactions: list[dict[str, Any]]) -> dict[str, Any]:
    lots: dict[str, float] = {}
    cost_basis: dict[str, float] = {}
    baseline_dates_applied: set[str] = set()
    gross_buys = 0.0
    gross_sells = 0.0
    normalized_transactions: list[dict[str, Any]] = []
    for row in transactions:
        raw = row.get("raw") or {}
        symbol = str(row.get("symbol") or "").upper()
        amount = float(raw.get("amount_mid") or 0)
        execution_price = price_on_or_before(con, symbol, row.get("event_date")) or 1.0
        quantity = amount / execution_price if execution_price > 0 else 0.0
        action = str(row.get("action") or "").upper()
        direction = -1 if action.startswith("S") else 1
        disclosed_quantity = disclosed_quantity_from_raw(raw, amount, execution_price)
        quantity = disclosed_quantity if disclosed_quantity is not None else quantity
        weight_before = allocation_weight(con, lots, symbol, row.get("event_date"))
        if action == "BASELINE":
            baseline_date = str(row.get("event_date"))
            if baseline_date not in baseline_dates_applied:
                lots.clear()
                cost_basis.clear()
                baseline_dates_applied.add(baseline_date)
            lots[symbol] = max(0.0, lots.get(symbol, 0.0) + quantity)
            cost_basis[symbol] = max(0.0, cost_basis.get(symbol, 0.0) + amount)
        else:
            if direction > 0:
                lots[symbol] = max(0.0, lots.get(symbol, 0.0) + quantity)
                cost_basis[symbol] = max(0.0, cost_basis.get(symbol, 0.0) + amount)
                gross_buys += amount
            else:
                current_quantity = lots.get(symbol, 0.0)
                sold_quantity = min(quantity, current_quantity)
                average_cost = (cost_basis.get(symbol, 0.0) / current_quantity) if current_quantity > 0 else 0.0
                lots[symbol] = max(0.0, current_quantity - sold_quantity)
                cost_basis[symbol] = max(0.0, cost_basis.get(symbol, 0.0) - sold_quantity * average_cost)
                gross_sells += amount
        weight_after = allocation_weight(con, lots, symbol, row.get("event_date"))
        normalized_transactions.append(
            {
                "symbol": symbol,
                "type": action or ("SELL" if direction < 0 else "BUY"),
                "quantity": quantity,
                "estimated_amount": amount,
                "price": execution_price,
                "date": str(row.get("event_date")),
                "filed_date": str(row.get("filed_date")),
                "weight_before": weight_before,
                "weight_after": weight_after,
                "source_url": row.get("source_url"),
                "comment": raw.get("comment"),
            }
        )

    holdings = []
    total_value = 0.0
    for symbol, quantity in lots.items():
        if quantity <= 0:
            continue
        latest_price = latest_price_for_symbol(con, symbol) or price_on_or_before(con, symbol, date.today().isoformat()) or 0.0
        market_value = quantity * latest_price
        total_value += market_value
        holdings.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "latest_price": latest_price,
                "market_value": market_value,
                "cost_basis": cost_basis.get(symbol, 0.0),
                "unrealized_pnl": market_value - cost_basis.get(symbol, 0.0),
                "weight": 0.0,
            }
        )
    for holding in holdings:
        holding["weight"] = (holding["market_value"] / total_value * 100) if total_value else 0.0
    holdings.sort(key=lambda item: item["weight"], reverse=True)
    current_cost_basis = sum(float(holding.get("cost_basis") or 0.0) for holding in holdings)
    performance = ((total_value - current_cost_basis) / current_cost_basis * 100) if current_cost_basis else 0.0
    portfolio_history = build_portfolio_history(con, transactions)
    return {
        "source_type": "trader_portfolio_model",
        "name": trader_name,
        "description": "Replica portfolio estimated from normalized public disclosure transactions.",
        "category": "public disclosures",
        "total_value": total_value,
        "estimated_invested_usd": current_cost_basis,
        "gross_buys_usd": gross_buys,
        "gross_sells_usd": gross_sells,
        "total_holdings": len(holdings),
        "last_updated": date.today().isoformat(),
        "performance_percent": performance,
        "performance_methodology": "Unrealized return on current reconstructed lots: (current market value - current cost basis) / current cost basis.",
        "metadata": {"riskLevel": "source-limited", "diversificationScore": diversification_score(holdings), "topSectors": []},
        "holdings": holdings,
        "transactions": normalized_transactions,
        "transactions_count": len(normalized_transactions),
        "portfolio_history": portfolio_history,
        "source_caveat": PUBLIC_DISCLOSURE_CAVEAT,
    }


def upsert_replica_portfolio_snapshot(con: Any, snapshot: dict[str, Any]) -> None:
    filed_date = str(snapshot["last_updated"])
    con.execute(
        """
        INSERT OR REPLACE INTO disclosures
        (id, source_type, trader_name, filer_name, symbol, event_date, filed_date, action, amount, raw, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            stable_id(f"trader-portfolio-model:{snapshot['name']}"),
            "trader_portfolio_model",
            snapshot["name"],
            "Market disclosure replica",
            None,
            filed_date,
            filed_date,
            "PORTFOLIO_MODEL",
            str(snapshot.get("total_value") or ""),
            json_dumps(snapshot),
            None,
        ],
    )


def disclosure_amount_range(row: dict[str, Any]) -> tuple[float | None, float | None]:
    low = _float_or_none(row.get("amount_min"))
    high = _float_or_none(row.get("amount_max"))
    if low is not None or high is not None:
        return low, high
    raw = str(row.get("amount") or row.get("amount_range") or "")
    cleaned = raw.replace("$", "").replace(",", "").replace("(", "").replace(")", "").replace("Over", "")
    numbers = [_float_or_none(part) for part in cleaned.split("-")]
    numbers = [number for number in numbers if number is not None]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return numbers[0], numbers[1]


def amount_midpoint(low: float | None, high: float | None) -> float | None:
    if low is None and high is None:
        return None
    if low is None:
        return high
    if high is None:
        return low
    return (low + high) / 2


def disclosed_quantity_from_raw(raw: dict[str, Any], amount: float, execution_price: float) -> float | None:
    shares = _float_or_none(raw.get("shares"))
    if shares is not None:
        return shares
    contracts = _float_or_none(raw.get("contracts"))
    if contracts is not None:
        return contracts * 100
    if execution_price > 0 and amount > 0:
        return amount / execution_price
    return None


def price_on_or_before(con: Any, symbol: str, as_of: Any) -> float | None:
    row = con.execute(
        """
        SELECT close FROM prices_daily
        WHERE symbol = ? AND date <= ?
        ORDER BY date DESC
        LIMIT 1
        """,
        [symbol, str(as_of)[:10]],
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def allocation_weight(con: Any, lots: dict[str, float], symbol: str, as_of: Any) -> float:
    values = holding_values_at(con, lots, as_of)
    total = sum(values.values())
    if total <= 0:
        return 0.0
    return values.get(symbol, 0.0) / total * 100


def holding_values_at(con: Any, lots: dict[str, float], as_of: Any) -> dict[str, float]:
    values: dict[str, float] = {}
    for symbol, quantity in lots.items():
        if quantity <= 0:
            continue
        price = price_on_or_before(con, symbol, as_of) or latest_price_for_symbol(con, symbol) or 0.0
        values[symbol] = quantity * price
    return values


def build_portfolio_history(con: Any, transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lots: dict[str, float] = {}
    cost_basis: dict[str, float] = {}
    baseline_dates_applied: set[str] = set()
    history: list[dict[str, Any]] = []
    for row in transactions:
        raw = row.get("raw") or {}
        symbol = str(row.get("symbol") or "").upper()
        amount = float(raw.get("amount_mid") or 0)
        execution_price = price_on_or_before(con, symbol, row.get("event_date")) or 1.0
        quantity = disclosed_quantity_from_raw(raw, amount, execution_price)
        if quantity is None:
            quantity = amount / execution_price if execution_price > 0 else 0.0
        action = str(row.get("action") or "").upper()
        if action == "BASELINE":
            baseline_date = str(row.get("event_date"))
            if baseline_date not in baseline_dates_applied:
                lots.clear()
                cost_basis.clear()
                baseline_dates_applied.add(baseline_date)
            lots[symbol] = max(0.0, lots.get(symbol, 0.0) + quantity)
            cost_basis[symbol] = max(0.0, cost_basis.get(symbol, 0.0) + amount)
        elif action.startswith("S"):
            current_quantity = lots.get(symbol, 0.0)
            sold_quantity = min(quantity, current_quantity)
            average_cost = (cost_basis.get(symbol, 0.0) / current_quantity) if current_quantity > 0 else 0.0
            lots[symbol] = max(0.0, current_quantity - sold_quantity)
            cost_basis[symbol] = max(0.0, cost_basis.get(symbol, 0.0) - sold_quantity * average_cost)
        else:
            lots[symbol] = max(0.0, lots.get(symbol, 0.0) + quantity)
            cost_basis[symbol] = max(0.0, cost_basis.get(symbol, 0.0) + amount)
        as_of = str(row.get("event_date"))[:10]
        values = holding_values_at(con, lots, as_of)
        total_value = sum(values.values())
        current_cost_basis = sum(cost_basis.values())
        performance = ((total_value - current_cost_basis) / current_cost_basis * 100) if current_cost_basis else 0.0
        history.append(
            {
                "date": as_of,
                "value": total_value,
                "cost_basis": current_cost_basis,
                "performance_percent": performance,
                "holdings_count": sum(1 for quantity in lots.values() if quantity > 0),
            }
        )
    today = date.today().isoformat()
    if lots and (not history or history[-1]["date"] != today):
        values = holding_values_at(con, lots, today)
        total_value = sum(values.values())
        current_cost_basis = sum(cost_basis.values())
        performance = ((total_value - current_cost_basis) / current_cost_basis * 100) if current_cost_basis else 0.0
        history.append(
            {
                "date": today,
                "value": total_value,
                "cost_basis": current_cost_basis,
                "performance_percent": performance,
                "holdings_count": sum(1 for quantity in lots.values() if quantity > 0),
            }
        )
    return compact_history(history)


def compact_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    for point in history:
        by_date[str(point["date"])] = point
    return [by_date[key] for key in sorted(by_date)]


def latest_price_for_symbol(con: Any, symbol: str) -> float | None:
    row = con.execute(
        "SELECT close FROM prices_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1",
        [symbol],
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def diversification_score(holdings: list[dict[str, Any]]) -> int:
    if not holdings:
        return 0
    top_weight = max(float(row.get("weight") or 0) for row in holdings)
    return max(0, min(100, round(100 - top_weight)))


def purge_direct_tracker_rows(con: Any) -> None:
    con.execute("DELETE FROM disclosures WHERE source_type = 'pelositracker_portfolio'")


def recent_13f_filings(submissions: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    filings: list[dict[str, Any]] = []
    for index, form in enumerate(forms):
        if form not in THIRTEEN_F_FORMS:
            continue
        accession_number = _recent_value(recent, "accessionNumber", index)
        if not accession_number:
            continue
        filings.append(
            {
                "accession_number": accession_number,
                "form": form,
                "filed_date": _recent_value(recent, "filingDate", index),
                "report_date": _recent_value(recent, "reportDate", index),
                "acceptance_datetime": _recent_value(recent, "acceptanceDateTime", index),
                "primary_document": _recent_value(recent, "primaryDocument", index),
                "primary_doc_description": _recent_value(recent, "primaryDocDescription", index),
                "filer_name": submissions.get("name"),
                "cik": str(submissions.get("cik") or "").zfill(10),
            }
        )
        if len(filings) >= limit:
            break
    return filings


def ingest_13f_trackers(
    con: Any,
    trackers: list[dict[str, Any]],
    user_agent: str,
    default_max_filings: int = 3,
    fetch_holdings: bool = True,
) -> dict[str, int]:
    filings_seen = 0
    filings_ingested = 0
    holdings_ingested = 0
    trackers_checked = 0
    for tracker in trackers:
        cik = str(tracker["cik"])
        trackers_checked += 1
        submissions = sec.company_submissions(cik, user_agent)
        max_filings = int(tracker.get("max_filings") or default_max_filings)
        for filing in recent_13f_filings(submissions, max_filings):
            filings_seen += 1
            holding_payload = {"holdings": [], "source_url": None, "status": "not_requested", "error": None}
            if fetch_holdings:
                holding_payload = fetch_13f_holding_payload(cik, filing["accession_number"], filing.get("primary_document"), user_agent)
            else:
                disclosure_id = stable_id(f"13f:{str(cik).zfill(10)}:{filing['accession_number']}")
                holding_payload = preserve_existing_13f_holdings_when_not_requested(con, disclosure_id, holding_payload)
            holdings = holding_payload["holdings"]
            holdings_ingested += len(holdings)
            upsert_13f_disclosure(con, tracker, filing, holding_payload)
            filings_ingested += 1
    return {
        "trackers_checked": trackers_checked,
        "filings_seen": filings_seen,
        "filings_ingested": filings_ingested,
        "holdings_ingested": holdings_ingested,
    }


def upsert_13f_disclosure(
    con: Any,
    tracker: dict[str, Any],
    filing: dict[str, Any],
    holding_payload: dict[str, Any],
) -> None:
    cik = str(tracker["cik"])
    accession_number = filing["accession_number"]
    disclosure_id = stable_id(f"13f:{str(cik).zfill(10)}:{accession_number}")
    holding_payload = preserve_existing_13f_holdings_when_not_requested(con, disclosure_id, holding_payload)
    holdings = resolve_13f_holding_tickers(holding_payload.get("holdings") or [], tracker.get("ticker_map") or {})
    total_value = sum(int(row.get("value_thousands") or 0) for row in holdings)
    source_url = holding_payload.get("source_url") or sec.filing_index_url(cik, accession_number)
    mapped_count = sum(1 for row in holdings if row.get("symbol"))
    raw = {
        "source_type": "13f",
        "tracker_name": tracker.get("name"),
        "cik": str(cik).zfill(10),
        "filer_name": filing.get("filer_name") or tracker.get("name"),
        "form": filing.get("form"),
        "accession_number": accession_number,
        "report_date": filing.get("report_date"),
        "filed_date": filing.get("filed_date"),
        "acceptance_datetime": filing.get("acceptance_datetime"),
        "primary_document": filing.get("primary_document"),
        "primary_doc_description": filing.get("primary_doc_description"),
        "lag_caveat": THIRTEEN_F_CAVEAT,
        "as_of_caveat": "Holdings are as of the filing report date, not the ingestion date.",
        "ticker_mapping_status": "mapped" if mapped_count == len(holdings) and holdings else "partial" if mapped_count else "unmapped",
        "ticker_mapping_count": mapped_count,
        "ticker_unmapped_count": len(holdings) - mapped_count,
        "holdings_parse_status": holding_payload.get("status"),
        "holdings_parse_error": holding_payload.get("error"),
        "holdings_source_url": holding_payload.get("source_url"),
        "holdings_count": len(holdings),
        "holdings_value_thousands": total_value if holdings else None,
        "next_filing_due_date": next_13f_filing_due_date(filing.get("report_date")),
        "holdings": holdings,
    }
    con.execute(
        """
        INSERT OR REPLACE INTO disclosures
        (id, source_type, trader_name, filer_name, symbol, event_date, filed_date, action, amount, raw, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            disclosure_id,
            "13f",
            tracker.get("name"),
            filing.get("filer_name") or tracker.get("name"),
            None,
            filing.get("report_date"),
            filing.get("filed_date"),
            filing.get("form"),
            str(total_value) if holdings else None,
            json_dumps(raw),
            source_url,
        ],
    )


def preserve_existing_13f_holdings_when_not_requested(con: Any, disclosure_id: str, holding_payload: dict[str, Any]) -> dict[str, Any]:
    if holding_payload.get("status") != "not_requested":
        return holding_payload
    row = con.execute("SELECT raw FROM disclosures WHERE id = ?", [disclosure_id]).fetchone()
    if not row:
        return holding_payload
    raw_value = row[0]
    if isinstance(raw_value, str):
        try:
            raw = json.loads(raw_value)
        except json.JSONDecodeError:
            return holding_payload
    elif isinstance(raw_value, dict):
        raw = raw_value
    else:
        return holding_payload
    holdings = raw.get("holdings")
    if not isinstance(holdings, list) or not holdings:
        return holding_payload
    return {
        "holdings": holdings,
        "source_url": raw.get("holdings_source_url"),
        "status": raw.get("holdings_parse_status") or "parsed",
        "error": raw.get("holdings_parse_error"),
    }


def resolve_13f_holding_tickers(holdings: list[dict[str, Any]], ticker_map: dict[str, str]) -> list[dict[str, Any]]:
    resolved = []
    for holding in holdings:
        row = dict(holding)
        cusip = normalize_cusip(row.get("cusip"))
        symbol = ticker_map.get(cusip)
        if symbol:
            row["symbol"] = symbol
            row["ticker_mapping_source"] = "configured_cusip_map"
        else:
            row["symbol"] = None
            row["ticker_mapping_source"] = "unresolved"
        resolved.append(row)
    return resolved


def next_13f_filing_due_date(report_date: Any) -> str | None:
    try:
        current = datetime.strptime(str(report_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    month = current.month + 3
    year = current.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    next_quarter_end = quarter_end_date(year, month)
    return (next_quarter_end + timedelta(days=45)).isoformat()


def quarter_end_date(year: int, month: int) -> date:
    quarter_end_month = ((month - 1) // 3 + 1) * 3
    if quarter_end_month == 12:
        return date(year, 12, 31)
    return date(year, quarter_end_month + 1, 1) - timedelta(days=1)


def fetch_13f_holding_payload(
    cik: str,
    accession_number: str,
    primary_document: str | None,
    user_agent: str,
) -> dict[str, Any]:
    try:
        index = sec.filing_directory_index(cik, accession_number, user_agent)
        candidates = information_table_candidates(index, primary_document)
        for filename in candidates:
            xml_text = sec.filing_document_text(cik, accession_number, filename, user_agent)
            holdings = parse_information_table_xml(xml_text)
            if holdings:
                return {
                    "holdings": holdings,
                    "source_url": sec.filing_document_url(cik, accession_number, filename),
                    "status": "parsed",
                    "error": None,
                }
        return fetch_13f_holdings_from_submission_text(cik, accession_number, user_agent)
    except Exception as exc:
        fallback = fetch_13f_holdings_from_submission_text(cik, accession_number, user_agent)
        if fallback["holdings"]:
            fallback["status"] = "parsed_from_submission_text_after_archive_error"
            fallback["error"] = str(exc)
            return fallback
        return {"holdings": [], "source_url": None, "status": "error", "error": str(exc)}


def fetch_13f_holdings_from_submission_text(cik: str, accession_number: str, user_agent: str) -> dict[str, Any]:
    try:
        text = sec.complete_submission_text(cik, accession_number, user_agent)
        for document in split_sec_documents(text):
            doc_type = document.get("type", "").lower()
            filename = document.get("filename")
            body = document.get("body", "")
            if "information table" in doc_type or "infotable" in (filename or "").lower() or "13f" in (filename or "").lower():
                holdings = parse_information_table_xml(body)
                if holdings:
                    return {
                        "holdings": holdings,
                        "source_url": sec.filing_document_url(cik, accession_number, f"{accession_number}.txt"),
                        "status": "parsed_from_submission_text",
                        "error": None,
                    }
        return {"holdings": [], "source_url": None, "status": "not_found", "error": None}
    except Exception as exc:
        return {"holdings": [], "source_url": None, "status": "submission_text_error", "error": str(exc)}


def information_table_candidates(index_json: dict[str, Any], primary_document: str | None = None) -> list[str]:
    items = ((index_json.get("directory") or {}).get("item") or [])
    primary = (primary_document or "").lower()
    xml_names = [
        str(item.get("name"))
        for item in items
        if isinstance(item, dict) and str(item.get("name", "")).lower().endswith(".xml")
    ]
    filtered = [name for name in xml_names if name.lower() not in {primary, "filingsummary.xml"}]
    return sorted(filtered, key=_information_table_rank)


def parse_information_table_xml(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text.encode("utf-8"))
    holdings: list[dict[str, Any]] = []
    for info_table in root.iter():
        if _local_name(info_table.tag) != "infoTable":
            continue
        row = _direct_text(info_table)
        shares = _first_child_text(info_table, "shrsOrPrnAmt")
        voting = _first_child_text(info_table, "votingAuthority")
        holdings.append(
            {
                "name": row.get("nameOfIssuer"),
                "title": row.get("titleOfClass"),
                "cusip": row.get("cusip"),
                "value_thousands": _int_or_none(row.get("value")),
                "shares_or_principal_amount": _int_or_none(shares.get("sshPrnamt")),
                "shares_or_principal_type": shares.get("sshPrnamtType"),
                "put_call": row.get("putCall"),
                "investment_discretion": row.get("investmentDiscretion"),
                "other_manager": row.get("otherManager"),
                "voting_authority": {
                    "sole": _int_or_none(voting.get("Sole")),
                    "shared": _int_or_none(voting.get("Shared")),
                    "none": _int_or_none(voting.get("None")),
                },
            }
        )
    return holdings


def split_sec_documents(submission_text: str) -> list[dict[str, str]]:
    documents: list[dict[str, str]] = []
    for chunk in submission_text.split("<DOCUMENT>")[1:]:
        body = chunk.split("</DOCUMENT>", 1)[0]
        documents.append(
            {
                "type": _tag_text(body, "TYPE"),
                "filename": _tag_text(body, "FILENAME"),
                "description": _tag_text(body, "DESCRIPTION"),
                "body": _body_text(body),
            }
        )
    return documents


def _recent_value(recent: dict[str, Any], key: str, index: int) -> Any:
    values = recent.get(key) or []
    if index >= len(values):
        return None
    return values[index]


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _tag_text(text: str, tag: str) -> str:
    marker = f"<{tag}>"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].splitlines()[0].strip()


def _body_text(text: str) -> str:
    if "<XML>" in text and "</XML>" in text:
        return text.split("<XML>", 1)[1].split("</XML>", 1)[0].strip()
    if "<TEXT>" in text and "</TEXT>" in text:
        return text.split("<TEXT>", 1)[1].split("</TEXT>", 1)[0].strip()
    return text


def _information_table_rank(filename: str) -> tuple[int, str]:
    lower = filename.lower()
    if "infotable" in lower or "info_table" in lower:
        return (0, lower)
    if "form13f" in lower or "13f" in lower:
        return (1, lower)
    if "primary" in lower:
        return (3, lower)
    return (2, lower)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_text(element: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for child in list(element):
        if list(child):
            continue
        if child.text and child.text.strip():
            values[_local_name(child.tag)] = child.text.strip()
    return values


def _first_child_text(element: ET.Element, child_name: str) -> dict[str, str]:
    for child in list(element):
        if _local_name(child.tag) == child_name:
            return _direct_text(child)
    return {}


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", ""))
    except ValueError:
        return None
