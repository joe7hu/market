"""Auto-split from core/disclosures.py — see ARCHITECTURE.md."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
import yaml
from investment_panel.core.db import json_dumps

from investment_panel.core.disclosures.coerce import _float_or_none
from investment_panel.core.disclosures.constants import PUBLIC_DISCLOSURE_CAVEAT, stable_id
from investment_panel.core.disclosures.prices import latest_price_for_symbol, price_on_or_before


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


def diversification_score(holdings: list[dict[str, Any]]) -> int:
    if not holdings:
        return 0
    top_weight = max(float(row.get("weight") or 0) for row in holdings)
    return max(0, min(100, round(100 - top_weight)))
