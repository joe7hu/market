"""13F disclosure read model and enrichment."""

from __future__ import annotations
from typing import Any
from investment_panel.core.db import db, init_db, query_rows

from investment_panel.core.panel.coerce import _normalize_symbol_token, decode_fields



def disclosures(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        WITH recent_non_13f AS (
            SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
                   action, amount, raw, source_url
            FROM disclosures
            WHERE source_type != '13f'
            ORDER BY filed_date DESC NULLS LAST
            LIMIT 200
        ),
        all_13f AS (
            SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
                   action, amount, raw, source_url
            FROM disclosures
            WHERE source_type = '13f'
        )
        SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
               action, amount, raw, source_url
        FROM recent_non_13f
        UNION ALL
        SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
               action, amount, raw, source_url
        FROM all_13f
        ORDER BY filed_date DESC NULLS LAST
        """,
    )
    decoded = [decode_fields(row, ("raw",)) for row in rows]
    enrich_13f_disclosure_rows(decoded)
    for row in decoded:
        raw = row.get("raw") or {}
        if isinstance(raw, dict):
            _copy_nonempty_raw_fields(
                row,
                raw,
                (
                    "holdings_count",
                    "holdings_value_thousands",
                    "total_value",
                    "estimated_invested_usd",
                    "performance_percent",
                    "platform_stats",
                    "metadata",
                    "transactions_count",
                    "transactions",
                    "sp500_history",
                    "source_caveat",
                    "lag_caveat",
                    "next_filing_due_date",
                ),
            )
            portfolio_history = row.get("portfolio_history") or raw.get("portfolio_history")
            if portfolio_history not in (None, "", [], {}):
                row["portfolio_history"] = portfolio_history
            holdings = raw.get("holdings")
            if isinstance(holdings, list):
                row["holding_sample"] = sorted_13f_holdings(holdings)[:25] if row.get("source_type") == "13f" else holdings[:25]
                trimmed_raw = dict(raw)
                trimmed_raw.pop("holdings", None)
                row["raw"] = trimmed_raw
    return [_compact_empty_fields(row) for row in decoded]




def _copy_nonempty_raw_fields(row: dict[str, Any], raw: dict[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        value = raw.get(field)
        if value not in (None, "", [], {}):
            row[field] = value




def _compact_empty_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}




def enrich_13f_disclosure_rows(rows: list[dict[str, Any]]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        if row.get("source_type") != "13f" or not isinstance(raw, dict):
            continue
        key = str(row.get("trader_name") or row.get("filer_name") or raw.get("cik") or "")
        grouped.setdefault(key, []).append(row)

    for group_rows in grouped.values():
        ordered = sorted(group_rows, key=lambda row: str(row.get("event_date") or ""))
        previous_weights: dict[str, float] = {}
        filing_history = []
        for row in ordered:
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
            holdings = sorted_13f_holdings(raw.get("holdings") if isinstance(raw, dict) else [])
            current_weights = {holding_key(holding): float(holding.get("weight") or 0.0) for holding in holdings}
            filing_history.append(
                {
                    "date": str(row.get("event_date") or ""),
                    "filed_date": str(row.get("filed_date") or ""),
                    "value": float(raw.get("holdings_value_thousands") or sum(float(holding.get("market_value") or 0.0) for holding in holdings)),
                    "holdings_count": raw.get("holdings_count") or len(holdings),
                }
            )
            history = []
            for holding in holdings[:25]:
                key = holding_key(holding)
                weight = float(holding.get("weight") or 0.0)
                previous = previous_weights.get(key, 0.0)
                history.append(
                    {
                        "symbol": holding.get("symbol"),
                        "security": holding.get("name"),
                        "put_call": holding.get("put_call"),
                        "date": str(row.get("event_date") or ""),
                        "filed_date": str(row.get("filed_date") or ""),
                        "type": "ADD" if previous == 0 and weight > 0 else "INCREASE" if weight > previous else "DECREASE" if weight < previous else "UNCHANGED",
                        "quantity": holding.get("shares_or_principal_amount") or 0,
                        "estimated_amount": float(holding.get("market_value") or 0.0),
                        "price": None,
                        "weight_before": previous,
                        "weight_after": weight,
                    }
                )
            row["allocation_history"] = history
            row["portfolio_history"] = list(filing_history)
            previous_weights = current_weights




def sorted_13f_holdings(holdings: Any) -> list[dict[str, Any]]:
    if not isinstance(holdings, list):
        return []
    total_value = sum(float(row.get("value_thousands") or 0.0) for row in holdings if isinstance(row, dict))
    sorted_rows = []
    for holding in holdings:
        if not isinstance(holding, dict):
            continue
        value = float(holding.get("value_thousands") or 0.0)
        row = dict(holding)
        row["market_value"] = value
        row["weight"] = (value / total_value * 100) if total_value else 0.0
        sorted_rows.append(row)
    return sorted(sorted_rows, key=lambda row: float(row.get("weight") or 0.0), reverse=True)




def holding_key(holding: dict[str, Any]) -> str:
    return ":".join(
        [
            str(holding.get("symbol") or holding.get("cusip") or holding.get("name") or ""),
            str(holding.get("put_call") or ""),
            str(holding.get("title") or ""),
        ]
    )




def _allocation_key(holding: dict[str, Any]) -> str:
    return ":".join(
        [
            _normalize_symbol_token(holding.get("symbol")),
            str(holding.get("put_call") or ""),
            str(holding.get("security") or holding.get("name") or ""),
        ]
    )
