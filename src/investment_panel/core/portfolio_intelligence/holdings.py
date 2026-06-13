"""Auto-split from portfolio_intelligence.py — see ARCHITECTURE.md."""
from __future__ import annotations

from typing import Any
from investment_panel.core import brokers
from investment_panel.core.db import query_rows
from investment_panel.core.decision import canonical_quote_rows

from investment_panel.core.portfolio_intelligence.coerce import BROAD_CATEGORIES, _float, _json_obj, _total_value, _weight


def _portfolio_holdings(con: Any) -> list[dict[str, Any]]:
    effective_rows = brokers.effective_portfolio_rows(con)
    if not effective_rows:
        return []
    symbols = [str(row.get("symbol") or "").upper() for row in effective_rows if row.get("symbol")]
    if not symbols:
        return []
    metadata = {
        str(row.get("symbol") or "").upper(): row
        for row in query_rows(
            con,
            """
            SELECT symbol, name, asset_class, sector, industry, category
            FROM instruments
            WHERE symbol IN ({})
            """.format(",".join("?" for _ in symbols)),
            symbols,
        )
    }
    quotes = {str(row.get("symbol") or "").upper(): row for row in canonical_quote_rows(con)}
    holdings: list[dict[str, Any]] = []
    for item in effective_rows:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        meta = metadata.get(symbol, {})
        quote = quotes.get(symbol, {})
        price = _float(item.get("market_price")) or _float(quote.get("price"))
        quantity = _float(item.get("quantity")) or 0.0
        avg_cost = _float(item.get("avg_cost") or item.get("average_cost")) or 0.0
        market_value = _float(item.get("market_value"))
        if market_value is None and price is not None:
            market_value = quantity * price
        if market_value is None:
            market_value = quantity * avg_cost
        holdings.append(
            {
                "symbol": symbol,
                "name": meta.get("name") or symbol,
                "asset_class": item.get("asset_class") or meta.get("asset_class") or "unclassified",
                "sector": meta.get("sector") or "",
                "industry": meta.get("industry") or "",
                "category": meta.get("category") or "",
                "quantity": quantity,
                "avg_cost": avg_cost,
                "price": price,
                "market_value": market_value or 0.0,
                "quote_freshness": quote.get("freshness_status") or "missing",
                "quote_source": quote.get("source"),
                "position_source": item.get("source"),
            }
        )
    total_value = _total_value(holdings)
    for row in holdings:
        row["portfolio_weight"] = _weight(float(row.get("market_value") or 0.0), total_value)
    return sorted(holdings, key=lambda row: float(row.get("portfolio_weight") or 0.0), reverse=True)


def _cluster_keys(holding: dict[str, Any]) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    sector = str(holding.get("sector") or "").strip()
    industry = str(holding.get("industry") or "").strip()
    category = str(holding.get("category") or "").strip()
    asset_class = str(holding.get("asset_class") or "").strip()
    if sector:
        keys.append(("sector", sector))
    if industry:
        keys.append(("industry", industry))
    if category.lower() not in BROAD_CATEGORIES:
        keys.append(("category", category))
    if asset_class:
        keys.append(("asset_class", asset_class))
    if not keys:
        keys.append(("cluster", "Unclassified"))
    return keys


def _symbol_evidence(con: Any) -> dict[str, dict[str, int]]:
    evidence: dict[str, dict[str, int]] = {}
    for key, sql in {
        "thesis_count": "SELECT symbol, count(*) AS count FROM theses GROUP BY symbol UNION ALL SELECT symbol, count(*) AS count FROM birdclaw_theses GROUP BY symbol",
        "catalyst_count": "SELECT symbol, count(*) AS count FROM catalysts WHERE symbol IS NOT NULL GROUP BY symbol",
        "disclosure_count": "SELECT symbol, count(*) AS count FROM disclosures WHERE symbol IS NOT NULL GROUP BY symbol",
    }.items():
        for row in query_rows(con, sql):
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            evidence.setdefault(symbol, {}).setdefault(key, 0)
            evidence[symbol][key] += int(row.get("count") or 0)
    return evidence


def _symbols_with_missing_thesis(con: Any, symbols: list[str]) -> list[str]:
    if not symbols:
        return []
    placeholders: dict[str, bool] = {symbol.upper(): True for symbol in symbols}
    rows = query_rows(
        con,
        "SELECT symbol, thesis_json FROM theses WHERE symbol IN ({})".format(",".join("?" for _ in symbols)),
        [symbol.upper() for symbol in symbols],
    )
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        thesis = _json_obj(row.get("thesis_json"))
        if _has_substantive_thesis(thesis):
            placeholders[symbol] = False
    for row in query_rows(
        con,
        "SELECT symbol, thesis_summary FROM birdclaw_theses WHERE symbol IN ({})".format(",".join("?" for _ in symbols)),
        [symbol.upper() for symbol in symbols],
    ):
        if str(row.get("thesis_summary") or "").strip():
            placeholders[str(row.get("symbol") or "").upper()] = False
    return sorted(symbol for symbol, missing in placeholders.items() if missing)


def _has_substantive_thesis(thesis: dict[str, Any]) -> bool:
    if not thesis:
        return False
    text_fields = [str(thesis.get("core_thesis") or ""), str(thesis.get("invalidation") or "")]
    list_fields = []
    for key in ("pillars", "risks", "catalysts"):
        value = thesis.get(key)
        if isinstance(value, list):
            list_fields.extend(str(item) for item in value)
    joined = " ".join(text_fields + list_fields).strip()
    return bool(joined and joined not in {"[]", "{}"})
