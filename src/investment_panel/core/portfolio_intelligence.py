"""Portfolio-level risk read models built from local Market data."""

from __future__ import annotations

import json
from typing import Any

from investment_panel.core import brokers
from investment_panel.core.db import query_rows
from investment_panel.core.decision import canonical_quote_rows


BROAD_CATEGORIES = {"", "owned-portfolio", "portfolio", "manual", "watchlist", "market"}


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
            }
        )
    return sorted(rows, key=lambda row: (float(row.get("portfolio_weight") or 0.0), int(row.get("symbol_count") or 0)), reverse=True)


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
    return sorted(edges.values(), key=_correlation_sort_key, reverse=True)[:25]


def portfolio_risk_cards(con: Any) -> list[dict[str, Any]]:
    """Actionable risk cards derived from current portfolio read models."""

    holdings = _portfolio_holdings(con)
    if not holdings:
        return []
    cards: list[dict[str, Any]] = []
    largest = max(holdings, key=lambda row: float(row.get("portfolio_weight") or 0.0))
    largest_weight = float(largest.get("portfolio_weight") or 0.0)
    if largest_weight >= 35:
        cards.append(
            _card(
                "largest-position",
                "concentration",
                "critical" if largest_weight >= 60 else "watch",
                f"{largest['symbol']} is {largest_weight:.1f}% of priced portfolio",
                "Single-name exposure dominates portfolio outcome.",
                [largest["symbol"]],
                largest_weight,
                [f"market_value={largest.get('market_value'):.2f}", f"quote={largest.get('quote_freshness') or 'unknown'}"],
                "Review target weight, hedge, or trim plan before adding adjacent exposure.",
            )
        )

    clusters = exposure_clusters(con)
    for cluster in clusters:
        if cluster.get("cluster_type") == "asset_class":
            continue
        weight = float(cluster.get("portfolio_weight") or 0.0)
        if weight >= 50:
            cards.append(
                _card(
                    f"cluster-{cluster['cluster_id']}",
                    "cluster_concentration",
                    "watch" if int(cluster.get("symbol_count") or 0) == 1 else "critical",
                    f"{cluster['cluster_name']} cluster is {weight:.1f}%",
                    str(cluster.get("risk_note") or "Cluster concentration needs review."),
                    list(cluster.get("symbols") or []),
                    weight,
                    [f"{cluster.get('symbol_count')} symbols", f"{cluster.get('cluster_type')} cluster"],
                    "Compare the cluster against intended portfolio themes and max exposure.",
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
            )
        )

    stale = [row for row in holdings if str(row.get("quote_freshness") or "").lower() not in {"fresh", "ok"}]
    if stale:
        symbols = [str(row.get("symbol")) for row in stale]
        cards.append(
            _card(
                "stale-owned-quotes",
                "data_freshness",
                "watch",
                f"{len(stale)} owned quote rows are not fresh",
                "Sizing, P/L, and cluster weights are only as good as the latest usable price.",
                symbols,
                sum(float(row.get("portfolio_weight") or 0.0) for row in stale),
                [", ".join(symbols[:4]), "quote_freshness != fresh"],
                "Refresh market data before making sizing decisions.",
            )
        )

    missing_thesis = _symbols_with_missing_thesis(con, [str(row.get("symbol")) for row in holdings])
    if missing_thesis:
        cards.append(
            _card(
                "missing-owned-theses",
                "thesis_gap",
                "watch",
                f"{len(missing_thesis)} owned positions have placeholder theses",
                "Owned exposure should have a falsifiable thesis, risks, invalidation, and catalyst path.",
                missing_thesis,
                sum(float(row.get("portfolio_weight") or 0.0) for row in holdings if row.get("symbol") in missing_thesis),
                ["theses.thesis_json is empty or placeholder"],
                "Write or refresh thesis records for owned symbols.",
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
            title = "Write owned-position theses"
            next_step = "Add thesis, risks, invalidation, and catalyst checklist for each owned symbol."
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
                "portfolio_weight": card.get("portfolio_weight"),
                "suggested_next_step": next_step,
                "source_card_id": card.get("card_id"),
            }
        )
    return actions[:12]


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
        "review_action": review_action,
        "source_models": ["portfolio", "quotes", "instruments", "theses", "catalysts", "disclosures"],
    }


def _priority(card: dict[str, Any]) -> str:
    if card.get("severity") == "critical":
        return "high"
    if float(card.get("portfolio_weight") or 0.0) >= 50:
        return "high"
    return "medium"


def _cluster_note(cluster_type: str, cluster_name: str, symbols: list[str], weight: float, duplicate_exposure: bool, concentration: bool) -> str:
    if cluster_type == "asset_class":
        return f"{cluster_name} is the portfolio asset-class allocation bucket, not hidden duplicate exposure."
    if duplicate_exposure:
        return f"{len(symbols)} owned symbols share {cluster_type} {cluster_name}; treat as overlapping exposure until differentiated."
    if concentration:
        return f"{cluster_name} is concentrated through {symbols[0] if symbols else 'one symbol'}."
    return f"{cluster_name} cluster is below concentration thresholds."


def _correlation_note(source: str, peer: str, corr: float, combined_weight: float, owned_peer: bool) -> str:
    owner = "owned pair" if owned_peer else "external peer"
    return f"{source}/{peer} {owner} correlation {corr:.2f}; associated portfolio weight {combined_weight:.1f}%."


def _concentration_level(weight: float, symbol_count: int) -> str:
    if weight >= 65:
        return "critical"
    if weight >= 35 or (symbol_count > 1 and weight >= 25):
        return "watch"
    return "normal"


def _can_be_duplicate_cluster(cluster_type: str) -> bool:
    return cluster_type in {"sector", "industry", "category"}


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


def _total_value(holdings: list[dict[str, Any]]) -> float:
    return sum(float(row.get("market_value") or 0.0) for row in holdings)


def _weight(value: float, total: float) -> float:
    return (value / total) * 100 if total else 0.0


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_list(value: Any) -> list[dict[str, Any]]:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}
