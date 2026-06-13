"""Auto-split from portfolio_intelligence.py — see ARCHITECTURE.md."""
from __future__ import annotations

from typing import Any
from investment_panel.core.db import query_rows

from investment_panel.core.portfolio_intelligence.coerce import _compact_empty_fields, _float, _json_list
from investment_panel.core.portfolio_intelligence.holdings import _portfolio_holdings


def correlation_edges(con: Any) -> list[dict[str, Any]]:
    """Flatten stored correlation runs into portfolio-aware edges."""

    holdings = _portfolio_holdings(con)
    if not holdings:
        return []
    holdings_by_symbol = {str(row.get("symbol") or "").upper(): row for row in holdings}
    symbols = list(holdings_by_symbol)
    rows = query_rows(
        con,
        """
        SELECT target_symbol, as_of, lookback_days, peers, metrics
        FROM correlation_runs
        WHERE target_symbol IN ({})
        QUALIFY row_number() OVER (PARTITION BY target_symbol ORDER BY as_of DESC) = 1
        ORDER BY as_of DESC, target_symbol
        """.format(",".join("?" for _ in symbols)),
        symbols,
    )
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        source = str(row.get("target_symbol") or "").upper()
        source_holding = holdings_by_symbol.get(source)
        if not source_holding:
            continue
        for peer in _json_list(row.get("peers")):
            peer_symbol = str(peer.get("symbol") or "").upper()
            if not peer_symbol or peer_symbol == source:
                continue
            corr = _float(peer.get("correlation"))
            if corr is None:
                continue
            peer_holding = holdings_by_symbol.get(peer_symbol)
            key = tuple(sorted([source, peer_symbol]))
            combined_weight = float(source_holding.get("portfolio_weight") or 0.0) + float(peer_holding.get("portfolio_weight") or 0.0) if peer_holding else float(source_holding.get("portfolio_weight") or 0.0)
            edge = {
                "edge_id": f"{key[0]}:{key[1]}",
                "symbol": source,
                "peer_symbol": peer_symbol,
                "correlation": corr,
                "abs_correlation": abs(corr),
                "as_of": row.get("as_of"),
                "lookback_days": row.get("lookback_days"),
                "symbol_weight": source_holding.get("portfolio_weight"),
                "peer_weight": peer_holding.get("portfolio_weight") if peer_holding else None,
                "combined_weight": combined_weight,
                "edge_type": "owned_owned" if peer_holding else "owned_external",
                "risk_level": _correlation_level(corr, combined_weight, bool(peer_holding)),
                "risk_note": _correlation_note(source, peer_symbol, corr, combined_weight, bool(peer_holding)),
            }
            existing = edges.get(key)
            if not existing or abs(corr) > abs(float(existing.get("correlation") or 0.0)):
                edges[key] = edge
    return [_compact_empty_fields(row) for row in sorted(edges.values(), key=_correlation_sort_key, reverse=True)[:25]]


def _correlation_note(source: str, peer: str, corr: float, combined_weight: float, owned_peer: bool) -> str:
    owner = "owned pair" if owned_peer else "external peer"
    return f"{source}/{peer} {owner} correlation {corr:.2f}; associated portfolio weight {combined_weight:.1f}%."


def _correlation_level(corr: float, combined_weight: float, owned_peer: bool) -> str:
    if owned_peer and abs(corr) >= 0.75 and combined_weight >= 35:
        return "critical"
    if owned_peer and abs(corr) >= 0.55:
        return "watch"
    if abs(corr) >= 0.7:
        return "market_beta"
    return "context"


def _correlation_sort_key(row: dict[str, Any]) -> tuple[int, float, float]:
    risk_rank = {"critical": 3, "watch": 2, "market_beta": 1, "context": 0}.get(str(row.get("risk_level") or ""), 0)
    return (
        risk_rank,
        float(row.get("abs_correlation") or 0.0),
        float(row.get("combined_weight") or 0.0),
    )
