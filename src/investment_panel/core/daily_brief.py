"""Daily attention brief read model for the /today surface."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from investment_panel.core import brokers
from investment_panel.core.db import query_rows
from investment_panel.core.decision import canonical_quote_rows
from investment_panel.core.portfolio_intelligence import portfolio_risk_cards, review_actions


STALE_STATUSES = {"failed", "missing", "stale", "degraded"}
ACTIONABLE_GRADES = {"act", "research"}


def daily_brief(con: Any) -> list[dict[str, Any]]:
    """Return a ranked, cross-model daily brief for the Today page."""

    items: list[dict[str, Any]] = []
    items.extend(_portfolio_change_items(con))
    items.extend(_risk_items(con))
    items.extend(_opportunity_items(con))
    items.extend(_calendar_items(con))
    items.extend(_blocked_items(con))
    items.extend(_thesis_gap_items(con))
    return _rank_items(_dedupe(items))[:24]


def _portfolio_change_items(con: Any) -> list[dict[str, Any]]:
    quotes = {str(row.get("symbol") or "").upper(): row for row in canonical_quote_rows(con)}
    holdings = brokers.effective_portfolio_rows(con)
    priced: list[dict[str, Any]] = []
    for row in holdings:
        symbol = str(row.get("symbol") or "").upper()
        quote = quotes.get(symbol, {})
        price = _float(row.get("market_price")) or _float(quote.get("price"))
        quantity = _float(row.get("quantity")) or 0.0
        avg_cost = _float(row.get("avg_cost") or row.get("average_cost")) or 0.0
        market_value = _float(row.get("market_value"))
        if market_value is None and price is not None:
            market_value = quantity * price
        if market_value is None:
            market_value = quantity * avg_cost
        change_pct = _float(quote.get("change_pct"))
        change_abs = _float(quote.get("change_abs"))
        day_change_value = quantity * change_abs if change_abs is not None else None
        priced.append(
            {
                "symbol": symbol,
                "market_value": market_value or 0.0,
                "change_pct": change_pct,
                "day_change_value": day_change_value,
                "quote_freshness": quote.get("freshness_status") or "missing",
                "quote_source": quote.get("source"),
                "price": price,
            }
        )

    total = sum(float(row.get("market_value") or 0.0) for row in priced)
    output = []
    for row in sorted(priced, key=lambda item: abs(float(item.get("day_change_value") or 0.0)) + abs(float(item.get("change_pct") or 0.0)), reverse=True)[:5]:
        symbol = row["symbol"]
        weight = _weight(float(row.get("market_value") or 0.0), total)
        change_pct = _float(row.get("change_pct"))
        day_change_value = _float(row.get("day_change_value"))
        quote_freshness = str(row.get("quote_freshness") or "missing")
        missing_change = change_pct is None and day_change_value is None
        output.append(
            _item(
                category="top_portfolio_changes",
                item_id=f"portfolio-change:{symbol}",
                title=f"{symbol} moved portfolio exposure",
                reason=_portfolio_change_reason(symbol, weight, change_pct, day_change_value, missing_change),
                evidence=[
                    f"{weight:.1f}% portfolio weight",
                    _pct_evidence("quote change", change_pct),
                    _money_evidence("estimated day P/L", day_change_value),
                    f"quote {quote_freshness}",
                ],
                blocker="Fresh quote row is missing" if quote_freshness.lower() in STALE_STATUSES else "None",
                next_action=f"Open {symbol} only if the move changes the hold/add/trim rule; otherwise leave it alone.",
                symbols=[symbol],
                score=80 + min(20, abs(change_pct or 0) * 2 + min(10, weight / 5)),
                severity="watch" if quote_freshness.lower() in STALE_STATUSES else "info",
                source_models=["portfolio", "quotes"],
            )
        )
    return output


def _risk_items(con: Any) -> list[dict[str, Any]]:
    actions_by_card = {str(row.get("source_card_id") or ""): row for row in review_actions(con)}
    output = []
    for card in portfolio_risk_cards(con)[:8]:
        symbols = _symbols(card)
        card_id = str(card.get("card_id") or card.get("risk_type") or "risk")
        action = actions_by_card.get(card_id, {})
        output.append(
            _item(
                category="top_risks",
                item_id=f"risk:{card_id}",
                title=str(card.get("title") or "Portfolio risk"),
                reason=str(card.get("summary") or "Portfolio risk card requires review."),
                evidence=_list(card.get("evidence")) or [str(card.get("impact") or "portfolio risk")],
                blocker=_risk_blocker(card),
                next_action=str(action.get("suggested_next_step") or card.get("next_step") or card.get("review_action") or "Review the risk card."),
                symbols=symbols,
                score=_float(card.get("score")) or 60,
                severity=str(card.get("severity") or "watch"),
                source_models=_list(card.get("source_models")) or ["portfolio_risk_cards"],
            )
        )
    return output


def _opportunity_items(con: Any) -> list[dict[str, Any]]:
    primary = []
    fallback = []
    for row in _decision_queue(con):
        action = str(row.get("action_grade") or "").lower()
        blockers = _list(row.get("blocking_gates"))
        symbol = str(row.get("symbol") or "").upper()
        reasons = _list(row.get("inclusion_reasons"))
        evidence_count = int(row.get("evidence_count") or 0)
        freshness = str(row.get("freshness_status") or "unknown")
        item = _item(
            category="top_opportunities",
            item_id=f"opportunity:{symbol}",
            title=f"{symbol} is a {row.get('action_grade') or 'Watch'} review candidate",
            reason=reasons[0] if reasons else str(row.get("source_cluster") or "Decision queue ranked this symbol."),
            evidence=[
                f"rank {row.get('rank') or '-'}",
                f"score {_number_label(row.get('score'))}",
                f"{evidence_count} evidence rows",
                f"freshness {freshness}",
            ],
            blocker=", ".join(_format_gate(gate) for gate in blockers) if blockers else "None",
            next_action=_opportunity_next_action(row, blockers),
            symbols=[symbol],
            score=70 + min(25, _float(row.get("action_score")) or _float(row.get("score")) or 0),
            severity="good" if action == "act" and not blockers else "watch",
            source_models=["decision_queue"],
            as_of=row.get("as_of"),
        )
        if action in ACTIONABLE_GRADES or not blockers:
            primary.append(item)
        elif int(row.get("rank") or 999) <= 8:
            fallback.append(
                {
                    **item,
                    "title": f"{symbol} is a gated research item",
                    "severity": "watch",
                    "score": max(55, float(item["score"]) - 10),
                }
            )
    return (primary or fallback)[:8]


def _calendar_items(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        WITH calendar_rows AS (
            SELECT symbol, event_date, event, expected_impact, source, importance, verification_status, source_url
            FROM catalysts
            WHERE event_date IS NOT NULL AND event_date >= current_date AND event_date <= current_date + INTERVAL 14 DAYS
            UNION ALL
            SELECT symbol, event_date, event_type AS event,
                   'Upcoming earnings event' AS expected_impact,
                   source, 'medium' AS importance, 'watch' AS verification_status, CAST(NULL AS TEXT) AS source_url
            FROM earnings_events
            WHERE event_date IS NOT NULL AND event_date >= current_date AND event_date <= current_date + INTERVAL 14 DAYS
        )
        SELECT *
        FROM calendar_rows
        ORDER BY event_date ASC, importance DESC NULLS LAST, symbol
        LIMIT 8
        """,
    )
    output = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        event = str(row.get("event") or "Scheduled event")
        output.append(
            _item(
                category="top_opportunities",
                item_id=f"calendar:{symbol or 'market'}:{row.get('event_date')}:{event}",
                title=f"{symbol or 'Market'} catalyst on {row.get('event_date')}",
                reason=event,
                evidence=[
                    f"date {row.get('event_date')}",
                    str(row.get("expected_impact") or "calendar row"),
                    f"source {row.get('source') or 'local'}",
                    f"verification {row.get('verification_status') or 'unknown'}",
                ],
                blocker="Tentative or watch status" if str(row.get("verification_status") or "").lower() in {"watch", "tentative"} else "None",
                next_action=f"Before {row.get('event_date')}, decide what would change the thesis or position size.",
                symbols=[symbol] if symbol else [],
                score=72 if str(row.get("importance") or "").lower() == "high" else 62,
                severity="watch" if str(row.get("importance") or "").lower() == "high" else "info",
                source_models=["catalysts", "earnings_events"],
                as_of=row.get("event_date"),
            )
        )
    return output


def _blocked_items(con: Any) -> list[dict[str, Any]]:
    output = []
    for row in _decision_queue(con):
        blockers = _list(row.get("blocking_gates"))
        freshness = str(row.get("freshness_status") or "").lower()
        if not blockers and freshness not in STALE_STATUSES:
            continue
        symbol = str(row.get("symbol") or "").upper()
        output.append(
            _item(
                category="blocked_stale_items",
                item_id=f"blocked-decision:{symbol}",
                title=f"{symbol} is blocked by evidence gates",
                reason=str(row.get("source_cluster") or "Decision row is not actionable."),
                evidence=[
                    f"action {row.get('action_grade') or '-'}",
                    f"freshness {row.get('freshness_status') or 'unknown'}",
                    f"quote {row.get('quote_freshness') or 'unknown'}",
                    f"daily analysis {row.get('daily_analysis_freshness') or 'unknown'}",
                ],
                blocker=", ".join(_format_gate(gate) for gate in blockers) if blockers else f"Freshness is {freshness}",
                next_action=_opportunity_next_action(row, blockers),
                symbols=[symbol],
                score=75 + len(blockers) * 4,
                severity="bad" if freshness in {"failed", "missing", "stale"} else "watch",
                source_models=["decision_queue", "source_freshness"],
                as_of=row.get("as_of"),
            )
        )
    for row in _stale_sources(con):
        key = str(row.get("source_key") or "source")
        output.append(
            _item(
                category="blocked_stale_items",
                item_id=f"stale-source:{key}",
                title=f"{row.get('provider') or row.get('source_type') or key} source is {row.get('freshness_status')}",
                reason=str(row.get("detail") or "A required source freshness row is stale, failed, or missing."),
                evidence=[
                    f"source {key}",
                    f"type {row.get('source_type') or 'unknown'}",
                    f"last observed {row.get('last_observed_at') or 'never'}",
                    f"status {row.get('status') or row.get('freshness_status') or 'unknown'}",
                ],
                blocker=str(row.get("detail") or f"Source freshness is {row.get('freshness_status') or row.get('status') or 'unknown'}"),
                next_action=f"Refresh {row.get('provider') or row.get('source_type') or key} before acting on dependent symbols.",
                score=68,
                severity="bad",
                source_models=["source_freshness"],
                as_of=row.get("checked_at") or row.get("last_observed_at"),
            )
        )
    return output[:10]


def _thesis_gap_items(con: Any) -> list[dict[str, Any]]:
    output = []
    for card in portfolio_risk_cards(con):
        if str(card.get("risk_type") or "") not in {"thesis_gap", "catalyst_gap"}:
            continue
        symbols = _symbols(card)
        output.append(
            _item(
                category="blocked_stale_items",
                item_id=f"thesis-gap:{card.get('risk_type')}:{'-'.join(symbols[:4])}",
                title=str(card.get("title") or "Owned thesis gap"),
                reason=str(card.get("summary") or "Owned position lacks enough thesis/catalyst evidence."),
                evidence=_list(card.get("evidence")) or ["owned position evidence gap"],
                blocker=str(card.get("trigger") or "Missing thesis or catalyst support"),
                next_action=str(card.get("next_step") or "Write the missing thesis/catalyst row before adding exposure."),
                symbols=symbols,
                score=66,
                severity=str(card.get("severity") or "watch"),
                source_models=["theses", "catalysts", "portfolio_risk_cards"],
            )
        )
    return output


def _decision_queue(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, rank, action_grade, score, action_score,
               freshness_status, quote_freshness, daily_analysis_freshness,
               filing_freshness, thesis_freshness, source_cluster, evidence_count,
               inclusion_reasons, blocking_gates, decision_basis, next_event_at,
               catalyst_window, liquidity_grade, portfolio_impact, invalidation
        FROM decision_queue
        ORDER BY rank ASC NULLS LAST, action_score DESC NULLS LAST, score DESC NULLS LAST
        LIMIT 40
        """,
    )
    for row in rows:
        row["inclusion_reasons"] = _json(row.get("inclusion_reasons"), [])
        row["blocking_gates"] = _json(row.get("blocking_gates"), [])
        row["decision_basis"] = _json(row.get("decision_basis"), {})
        row["portfolio_impact"] = _json(row.get("portfolio_impact"), {})
    return rows


def _stale_sources(con: Any) -> list[dict[str, Any]]:
    return query_rows(
        con,
        """
        SELECT source_key, source_type, provider, last_observed_at, freshness_status, status, detail, checked_at
        FROM source_freshness
        WHERE COALESCE(docs_only, false) = false
          AND lower(COALESCE(freshness_status, status, '')) IN ('failed', 'missing', 'stale', 'degraded')
        ORDER BY
          CASE lower(COALESCE(freshness_status, status, ''))
            WHEN 'failed' THEN 0
            WHEN 'missing' THEN 1
            WHEN 'stale' THEN 2
            ELSE 3
          END,
          checked_at DESC NULLS LAST,
          source_key
        LIMIT 8
        """,
    )


def _rank_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    category_order = {
        "top_portfolio_changes": 0,
        "top_risks": 1,
        "top_opportunities": 2,
        "blocked_stale_items": 3,
    }
    counters: dict[str, int] = {}
    ranked = sorted(items, key=lambda item: (category_order.get(str(item.get("category")), 9), -float(item.get("score") or 0), str(item.get("title") or "")))
    for item in ranked:
        category = str(item.get("category") or "brief")
        counters[category] = counters.get(category, 0) + 1
        item["rank"] = counters[category]
    return ranked


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    output = []
    for item in items:
        key = (str(item.get("category") or ""), str(item.get("item_id") or item.get("title") or ""))
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _item(
    *,
    category: str,
    item_id: str,
    title: str,
    reason: str,
    evidence: list[str],
    blocker: str,
    next_action: str,
    score: float,
    severity: str,
    symbols: list[str] | None = None,
    source_models: list[str] | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    symbols = [symbol for symbol in (symbols or []) if symbol]
    return {
        "item_id": item_id,
        "category": category,
        "rank": None,
        "title": title,
        "symbol": symbols[0] if symbols else None,
        "symbols": symbols,
        "reason": reason or "Backend read model selected this item for today's brief.",
        "evidence": [item for item in evidence if item and item != "-"],
        "blocker": blocker or "None",
        "next_action": next_action or "Review the source rows before acting.",
        "score": round(float(score or 0), 2),
        "severity": severity or "info",
        "source_models": source_models or [],
        "as_of": as_of or datetime.now(UTC),
    }


def _portfolio_change_reason(symbol: str, weight: float, change_pct: float | None, day_change_value: float | None, missing_change: bool) -> str:
    if missing_change:
        return f"{symbol} is owned at {weight:.1f}% but has no current move row, so the daily P/L scan is incomplete."
    return f"{symbol} is owned at {weight:.1f}% and is one of the largest visible contributors to today's portfolio change."


def _risk_blocker(card: dict[str, Any]) -> str:
    risk_type = str(card.get("risk_type") or "")
    if risk_type == "data_freshness":
        return "Stale owned quote coverage"
    if risk_type == "thesis_gap":
        return "Missing falsifiable owned-position thesis"
    if risk_type == "catalyst_gap":
        return "Missing catalyst/review path"
    return "None"


def _opportunity_next_action(row: dict[str, Any], blockers: list[str]) -> str:
    symbol = str(row.get("symbol") or "").upper()
    if blockers:
        return f"Clear {', '.join(_format_gate(gate) for gate in blockers[:2])} for {symbol} before making a decision."
    action = str(row.get("action_grade") or "Watch")
    if action.lower() == "act":
        return f"Open {symbol}, verify risk sizing, and decide add/hold/pass."
    if action.lower() == "research":
        return f"Research {symbol}; fill the highest-impact missing evidence before any sizing decision."
    return f"Keep {symbol} on watch unless a catalyst or source refresh changes the decision grade."


def _format_gate(value: Any) -> str:
    text = str(value or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Unknown gate"


def _symbols(row: dict[str, Any]) -> list[str]:
    value = row.get("symbols")
    if isinstance(value, list):
        return [str(item).upper() for item in value if item]
    symbol = str(row.get("symbol") or "").upper()
    return [symbol] if symbol else []


def _list(value: Any) -> list[str]:
    parsed = _json(value, value)
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item not in (None, "")]
    if isinstance(parsed, str) and parsed:
        return [parsed]
    return []


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return fallback
    return value if value is not None else fallback


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _weight(value: float, total: float) -> float:
    return (value / total) * 100 if total else 0.0


def _number_label(value: Any) -> str:
    parsed = _float(value)
    return f"{parsed:.1f}" if parsed is not None else "-"


def _pct_evidence(label: str, value: float | None) -> str:
    return f"{label} {value:+.2f}%" if value is not None else f"{label} missing"


def _money_evidence(label: str, value: float | None) -> str:
    return f"{label} ${value:,.0f}" if value is not None else f"{label} unknown"
