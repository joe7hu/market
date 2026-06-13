"""Auto-split from portfolio_intelligence.py — see ARCHITECTURE.md."""
from __future__ import annotations

from typing import Any

from investment_panel.core.portfolio_intelligence.coerce import _total_value, _weight
from investment_panel.core.portfolio_intelligence.holdings import _cluster_keys, _portfolio_holdings, _symbol_evidence


def exposure_clusters(con: Any) -> list[dict[str, Any]]:
    """Group owned exposure by useful metadata buckets."""

    holdings = _portfolio_holdings(con)
    if not holdings:
        return []
    total_value = _total_value(holdings)
    evidence = _symbol_evidence(con)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for holding in holdings:
        for cluster_type, cluster_name in _cluster_keys(holding):
            groups.setdefault((cluster_type, cluster_name), []).append(holding)

    rows: list[dict[str, Any]] = []
    for (cluster_type, cluster_name), members in groups.items():
        market_value = sum(float(row.get("market_value") or 0.0) for row in members)
        weight = _weight(market_value, total_value)
        symbols = sorted({str(row.get("symbol") or "") for row in members if row.get("symbol")})
        stale_count = sum(1 for row in members if str(row.get("quote_freshness") or "").lower() not in {"fresh", "ok"})
        thesis_count = sum(1 for symbol in symbols if evidence.get(symbol, {}).get("thesis_count"))
        catalyst_count = sum(1 for symbol in symbols if evidence.get(symbol, {}).get("catalyst_count"))
        disclosure_count = sum(1 for symbol in symbols if evidence.get(symbol, {}).get("disclosure_count"))
        duplicate_exposure = _can_be_duplicate_cluster(cluster_type) and len(symbols) > 1 and weight >= 25
        concentration = weight >= 35
        rows.append(
            {
                "cluster_id": f"{cluster_type}:{cluster_name}".lower().replace(" ", "-"),
                "cluster_type": cluster_type,
                "cluster_name": cluster_name,
                "symbols": symbols,
                "symbol_count": len(symbols),
                "market_value": market_value,
                "portfolio_weight": weight,
                "largest_symbol": max(members, key=lambda row: float(row.get("portfolio_weight") or 0.0)).get("symbol"),
                "largest_symbol_weight": max(float(row.get("portfolio_weight") or 0.0) for row in members),
                "stale_quote_count": stale_count,
                "thesis_count": thesis_count,
                "catalyst_count": catalyst_count,
                "disclosure_count": disclosure_count,
                "duplicate_exposure": duplicate_exposure,
                "concentration_level": _concentration_level(weight, len(symbols)),
                "risk_note": _cluster_note(cluster_type, cluster_name, symbols, weight, duplicate_exposure, concentration),
                "risk_readout": _cluster_readout(cluster_type, cluster_name, symbols, weight, stale_count, evidence),
                "next_step": _cluster_next_step(cluster_type, cluster_name, symbols, weight, duplicate_exposure, concentration),
                "evidence": _cluster_evidence(symbols, weight, stale_count, thesis_count, catalyst_count, disclosure_count),
                "is_actionable": cluster_type != "asset_class" and (duplicate_exposure or concentration),
            }
        )
    return sorted(rows, key=lambda row: (float(row.get("portfolio_weight") or 0.0), int(row.get("symbol_count") or 0)), reverse=True)


def _cluster_note(cluster_type: str, cluster_name: str, symbols: list[str], weight: float, duplicate_exposure: bool, concentration: bool) -> str:
    if cluster_type == "asset_class":
        return f"{cluster_name} is the portfolio asset-class allocation bucket, not hidden duplicate exposure."
    if duplicate_exposure:
        return f"{len(symbols)} owned symbols share {cluster_type} {cluster_name}; treat as overlapping exposure until differentiated."
    if concentration:
        return f"{cluster_name} is concentrated through {symbols[0] if symbols else 'one symbol'}."
    return f"{cluster_name} cluster is below concentration thresholds."


def _cluster_readout(cluster_type: str, cluster_name: str, symbols: list[str], weight: float, stale_count: int, evidence: dict[str, dict[str, int]]) -> str:
    if cluster_type == "asset_class":
        return f"{weight:.1f}% sits in {cluster_name}; useful for allocation, not duplicate-risk evidence."
    leader = symbols[0] if symbols else cluster_name
    if len(symbols) == 1:
        stale = " stale quote" if stale_count else ""
        return f"{cluster_name} exposure is really {leader} concentration at {weight:.1f}%{stale}."
    unsupported = [
        symbol
        for symbol in symbols
        if not evidence.get(symbol, {}).get("thesis_count") or not evidence.get(symbol, {}).get("catalyst_count")
    ]
    suffix = f"; {len(unsupported)} symbols need thesis/catalyst support" if unsupported else ""
    return f"{len(symbols)} names share {cluster_type} {cluster_name} for {weight:.1f}% of priced value{suffix}."


def _cluster_next_step(cluster_type: str, cluster_name: str, symbols: list[str], weight: float, duplicate_exposure: bool, concentration: bool) -> str:
    if cluster_type == "asset_class":
        return "Use this only as allocation context; inspect sector/industry rows for actionable risk."
    if duplicate_exposure:
        return f"Decide the max combined weight for {cluster_name} and name which symbol owns the core thesis."
    if concentration and symbols:
        return f"Set a target-weight ceiling for {symbols[0]} before adding adjacent {cluster_name} exposure."
    return "No action unless this bucket is part of an intended portfolio theme."


def _cluster_evidence(symbols: list[str], weight: float, stale_count: int, thesis_count: int, catalyst_count: int, disclosure_count: int) -> list[str]:
    evidence = [f"{weight:.1f}% weight", f"{len(symbols)} symbol{'s' if len(symbols) != 1 else ''}"]
    if stale_count:
        evidence.append(f"{stale_count} stale quote{'s' if stale_count != 1 else ''}")
    if thesis_count:
        evidence.append(f"{thesis_count} thesis row{'s' if thesis_count != 1 else ''}")
    if catalyst_count:
        evidence.append(f"{catalyst_count} catalyst row{'s' if catalyst_count != 1 else ''}")
    if disclosure_count:
        evidence.append(f"{disclosure_count} disclosure row{'s' if disclosure_count != 1 else ''}")
    return evidence


def _concentration_level(weight: float, symbol_count: int) -> str:
    if weight >= 65:
        return "critical"
    if weight >= 35 or (symbol_count > 1 and weight >= 25):
        return "watch"
    return "normal"


def _can_be_duplicate_cluster(cluster_type: str) -> bool:
    return cluster_type in {"sector", "industry", "category"}
