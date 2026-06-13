"""Auto-split from portfolio_intelligence.py — see ARCHITECTURE.md."""
from __future__ import annotations

from typing import Any

from investment_panel.core.portfolio_intelligence.coerce import _money
from investment_panel.core.portfolio_intelligence.correlation import correlation_edges
from investment_panel.core.portfolio_intelligence.exposure import exposure_clusters
from investment_panel.core.portfolio_intelligence.holdings import _portfolio_holdings, _symbol_evidence, _symbols_with_missing_thesis


def portfolio_risk_cards(con: Any) -> list[dict[str, Any]]:
    """Actionable risk cards derived from current portfolio read models."""

    holdings = _portfolio_holdings(con)
    if not holdings:
        return []
    cards: list[dict[str, Any]] = []
    largest = max(holdings, key=lambda row: float(row.get("portfolio_weight") or 0.0))
    largest_weight = float(largest.get("portfolio_weight") or 0.0)
    if largest_weight >= 35:
        quote_status = str(largest.get("quote_freshness") or "unknown")
        cards.append(
            _card(
                "largest-position",
                "concentration",
                "critical" if largest_weight >= 60 else "watch",
                f"{largest['symbol']} is {largest_weight:.1f}% of priced portfolio",
                f"Portfolio outcome is dominated by {largest['symbol']}; the next sizing decision should be a cap, trim, or hedge rule, not another adjacent add.",
                [largest["symbol"]],
                largest_weight,
                [f"{largest['symbol']} weight {largest_weight:.1f}%", f"market value {_money(float(largest.get('market_value') or 0.0))}", f"quote {quote_status}"],
                "Set a max target weight for the position and write the add/hold/trim rule into the thesis before increasing related exposure.",
                impact=f"{largest_weight:.1f}% of priced value",
                trigger="single position >= 60%" if largest_weight >= 60 else "single position >= 35%",
                next_step=f"Decide whether {largest['symbol']} should stay above {largest_weight:.1f}% or define the first trim/hedge threshold.",
            )
        )

    clusters = exposure_clusters(con)
    for cluster in clusters:
        if cluster.get("cluster_type") == "asset_class":
            continue
        weight = float(cluster.get("portfolio_weight") or 0.0)
        symbol_count = int(cluster.get("symbol_count") or 0)
        if bool(cluster.get("duplicate_exposure")) or (symbol_count > 1 and weight >= 35):
            cards.append(
                _card(
                    f"cluster-{cluster['cluster_id']}",
                    "cluster_concentration",
                    "watch" if symbol_count == 1 else "critical",
                    f"{cluster['cluster_name']} cluster is {weight:.1f}%",
                    str(cluster.get("risk_note") or "Cluster concentration needs review."),
                    list(cluster.get("symbols") or []),
                    weight,
                    list(cluster.get("evidence") or [f"{symbol_count} symbols", f"{cluster.get('cluster_type')} cluster"]),
                    str(cluster.get("next_step") or "Compare the cluster against intended portfolio themes and max exposure."),
                    impact=f"{weight:.1f}% across {symbol_count} symbols",
                    trigger="same sector/industry/category and multi-name weight >= 25%",
                    next_step=str(cluster.get("next_step") or "Set the intended max weight for this cluster."),
                )
            )
            break

    duplicate_edges = [row for row in correlation_edges(con) if row.get("edge_type") == "owned_owned" and float(row.get("abs_correlation") or 0.0) >= 0.55]
    if duplicate_edges:
        edge = duplicate_edges[0]
        cards.append(
            _card(
                f"correlation-{edge['edge_id']}",
                "hidden_duplicate_exposure",
                "critical" if float(edge.get("combined_weight") or 0.0) >= 50 else "watch",
                f"{edge['symbol']} and {edge['peer_symbol']} move together",
                str(edge.get("risk_note") or "Owned positions have high return correlation."),
                [str(edge.get("symbol")), str(edge.get("peer_symbol"))],
                float(edge.get("combined_weight") or 0.0),
                [f"corr={float(edge.get('correlation') or 0.0):.2f}", f"lookback={edge.get('lookback_days')}d"],
                "Treat the pair as one exposure until the thesis proves otherwise.",
                impact=f"{float(edge.get('combined_weight') or 0.0):.1f}% combined weight",
                trigger="owned-owned correlation >= 0.55",
                next_step=f"Decide whether {edge['symbol']} and {edge['peer_symbol']} deserve separate risk budgets.",
            )
        )

    stale = [row for row in holdings if str(row.get("quote_freshness") or "").lower() not in {"fresh", "ok"}]
    if stale:
        symbols = [str(row.get("symbol")) for row in stale]
        stale_weight = sum(float(row.get("portfolio_weight") or 0.0) for row in stale)
        cards.append(
            _card(
                "stale-owned-quotes",
                "data_freshness",
                "watch",
                f"{len(stale)} owned quotes are stale",
                f"Risk weights and P/L are being computed on stale quote rows covering {stale_weight:.1f}% of priced exposure.",
                symbols,
                stale_weight,
                [", ".join(symbols[:4]), f"{stale_weight:.1f}% affected", "quote freshness not fresh/ok"],
                "Refresh market data before making sizing decisions.",
                impact=f"{stale_weight:.1f}% affected",
                trigger="owned quote freshness not fresh/ok",
                next_step="Run the free-source market data refresh, then revisit concentration and P/L.",
            )
        )

    missing_thesis = _symbols_with_missing_thesis(con, [str(row.get("symbol")) for row in holdings])
    if missing_thesis:
        missing_thesis_weight = sum(float(row.get("portfolio_weight") or 0.0) for row in holdings if row.get("symbol") in missing_thesis)
        cards.append(
            _card(
                "missing-owned-theses",
                "thesis_gap",
                "watch",
                f"{len(missing_thesis)} owned theses are not investable",
                f"{', '.join(missing_thesis[:4])} lack a falsifiable thesis, risk list, invalidation, and catalyst path.",
                missing_thesis,
                missing_thesis_weight,
                [f"{missing_thesis_weight:.1f}% affected", "placeholder or empty thesis_json"],
                "Write or refresh thesis records for owned symbols.",
                impact=f"{missing_thesis_weight:.1f}% without thesis",
                trigger="owned thesis missing core thesis/risks/invalidation/catalysts",
                next_step=f"Write decision theses for {', '.join(missing_thesis[:3])}.",
            )
        )
    evidence = _symbol_evidence(con)
    missing_catalysts = [
        str(row.get("symbol"))
        for row in holdings
        if not evidence.get(str(row.get("symbol") or "").upper(), {}).get("catalyst_count")
    ]
    if missing_catalysts:
        catalyst_weight = sum(float(row.get("portfolio_weight") or 0.0) for row in holdings if row.get("symbol") in missing_catalysts)
        cards.append(
            _card(
                "missing-owned-catalysts",
                "catalyst_gap",
                "info" if catalyst_weight < 50 else "watch",
                f"{len(missing_catalysts)} owned positions have no catalyst path",
                f"No stored catalyst rows explain what should force a fresh decision for {', '.join(missing_catalysts[:4])}.",
                missing_catalysts,
                catalyst_weight,
                [f"{catalyst_weight:.1f}% affected", "0 catalyst rows for owned symbols"],
                "Add upcoming earnings, product, macro, or thesis-invalidation review dates.",
                impact=f"{catalyst_weight:.1f}% without catalyst",
                trigger="owned symbol has no catalyst rows",
                next_step=f"Add next review catalyst for {', '.join(missing_catalysts[:3])}.",
            )
        )
    return sorted(cards, key=lambda row: (int(row.get("score") or 0), float(row.get("portfolio_weight") or 0.0)), reverse=True)


def review_actions(con: Any) -> list[dict[str, Any]]:
    """Specific review actions generated from portfolio risk cards."""

    actions = []
    for index, card in enumerate(portfolio_risk_cards(con), start=1):
        risk_type = str(card.get("risk_type") or "")
        symbols = list(card.get("symbols") or [])
        if risk_type == "concentration":
            action_type = "sizing_review"
            title = f"Set target weight for {symbols[0]}"
            next_step = "Decide hold/add/trim threshold and record it in the ticker thesis."
        elif risk_type == "cluster_concentration":
            action_type = "cluster_review"
            title = "Review cluster max exposure"
            next_step = "Compare current cluster weight with intended portfolio construction."
        elif risk_type == "hidden_duplicate_exposure":
            action_type = "duplicate_exposure_review"
            title = "Validate correlated pair is intentional"
            next_step = "Document why both positions deserve separate risk budget or reduce one leg."
        elif risk_type == "data_freshness":
            action_type = "refresh_data"
            title = "Refresh owned quote coverage"
            next_step = "Run the market data refresh before acting on portfolio weights."
        elif risk_type == "thesis_gap":
            action_type = "thesis_review"
            title = f"Write thesis for {', '.join(symbols[:3])}"
            next_step = "Add core thesis, risks, invalidation, and catalyst checklist for each owned symbol."
        elif risk_type == "catalyst_gap":
            action_type = "catalyst_review"
            title = f"Add catalyst path for {', '.join(symbols[:3])}"
            next_step = "Record the next event or review date that would change the hold/add/trim decision."
        else:
            action_type = "portfolio_review"
            title = str(card.get("title") or "Review portfolio risk")
            next_step = str(card.get("review_action") or "Open the portfolio risk card.")
        actions.append(
            {
                "action_id": f"portfolio-risk-{index}",
                "priority": _priority(card),
                "status": "open",
                "action_type": action_type,
                "title": title,
                "symbols": symbols,
                "symbol": symbols[0] if symbols else None,
                "risk_type": risk_type,
                "rationale": card.get("summary"),
                "evidence": card.get("evidence"),
                "impact": card.get("impact"),
                "trigger": card.get("trigger"),
                "portfolio_weight": card.get("portfolio_weight"),
                "suggested_next_step": card.get("next_step") or next_step,
                "source_card_id": card.get("card_id"),
            }
        )
    return actions[:12]


def _card(
    card_id: str,
    risk_type: str,
    severity: str,
    title: str,
    summary: str,
    symbols: list[str],
    portfolio_weight: float,
    evidence: list[str],
    review_action: str,
    *,
    impact: str = "",
    trigger: str = "",
    next_step: str = "",
) -> dict[str, Any]:
    score = {"critical": 90, "watch": 65, "info": 40}.get(severity, 50)
    return {
        "card_id": card_id,
        "risk_type": risk_type,
        "severity": severity,
        "title": title,
        "summary": summary,
        "symbols": symbols,
        "symbol": symbols[0] if symbols else None,
        "portfolio_weight": portfolio_weight,
        "score": score,
        "evidence": evidence,
        "impact": impact or f"{portfolio_weight:.1f}% portfolio weight",
        "trigger": trigger,
        "next_step": next_step or review_action,
        "review_action": review_action,
        "source_models": ["portfolio", "quotes", "instruments", "theses", "catalysts", "disclosures"],
    }


def _priority(card: dict[str, Any]) -> str:
    if card.get("severity") == "critical":
        return "high"
    if float(card.get("portfolio_weight") or 0.0) >= 50:
        return "high"
    return "medium"
