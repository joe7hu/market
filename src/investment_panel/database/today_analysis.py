"""PostgreSQL-native daily brief and portfolio decision publication."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime


def refresh_today_publication(runtime: DatabaseRuntime, *, now: datetime | None = None) -> dict[str, Any]:
    as_of = now or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("today publication timestamp must be timezone-aware")
    with runtime.read() as connection:
        holdings = [
            dict(row)
            for row in connection.execute(
                """
                SELECT instrument.id AS instrument_id, instrument.symbol, position.quantity,
                       position.average_cost, position.notes, quote.price,
                       quote.observed_at AS quote_observed_at
                FROM app.portfolio_position position
                JOIN catalog.instrument instrument ON instrument.id = position.instrument_id
                LEFT JOIN LATERAL (
                    SELECT price, observed_at FROM raw.quote
                    WHERE instrument_id = instrument.id ORDER BY observed_at DESC LIMIT 1
                ) quote ON true
                ORDER BY instrument.symbol
                """
            ).fetchall()
        ]
        reviews = [
            dict(row)
            for row in connection.execute(
                """
                SELECT instrument.id AS instrument_id, instrument.symbol, thesis.id::text AS thesis_id,
                       thesis.status, thesis.thesis->>'last_reviewed' AS last_reviewed_at,
                       thesis.thesis->>'core_thesis' AS thesis,
                       thesis.thesis->>'invalidation' AS invalidation
                FROM app.thesis thesis
                JOIN catalog.instrument instrument ON instrument.id = thesis.instrument_id
                WHERE thesis.status = 'current'
                  AND (
                    coalesce(thesis.thesis->>'core_thesis', '') = ''
                    OR thesis.thesis->>'last_reviewed' IS NULL
                    OR (thesis.thesis->>'last_reviewed')::timestamptz < %s - interval '45 days'
                  )
                ORDER BY thesis.thesis->>'last_reviewed' NULLS FIRST, instrument.symbol
                """,
                [as_of],
            ).fetchall()
        ]
        catalysts = [
            dict(row)
            for row in connection.execute(
                """
                SELECT catalyst.id::text AS event_id, instrument.id AS instrument_id,
                       instrument.symbol, catalyst.starts_at, catalyst.title,
                       catalyst.expected_impact, catalyst.notes
                FROM app.catalyst catalyst
                LEFT JOIN catalog.instrument instrument ON instrument.id = catalyst.instrument_id
                WHERE catalyst.starts_at >= %s AND catalyst.starts_at < %s + interval '14 days'
                ORDER BY catalyst.starts_at LIMIT 20
                """,
                [as_of, as_of],
            ).fetchall()
        ]
        option_rows = [
            dict(row["payload"])
            for row in connection.execute(
                """
                SELECT item.payload FROM app.publication publication
                JOIN app.publication_item item ON item.publication_id = publication.id
                WHERE publication.scope = 'options-radar'
                  AND publication.status = 'published'
                  AND item.model_name = 'option_radar_opportunity'
                ORDER BY item.rank LIMIT 10
                """
            ).fetchall()
        ]

    portfolio_rows = [_portfolio_pulse(row, holdings) for row in holdings]
    review_rows = [_review_item(row) for row in reviews]
    option_items = [_option_item(row) for row in option_rows]
    catalyst_rows = [_catalyst_item(row, as_of) for row in catalysts]
    daily_brief = sorted(
        option_items + review_rows + catalyst_rows + portfolio_rows,
        key=lambda row: (-float(row.get("score") or 0), str(row.get("symbol") or "")),
    )
    decision_queue = [
        {
            **row,
            "stable_key": f"decision:{row['stable_key']}",
            "readiness_status": "ready" if not row.get("blockers") else "blocked",
            "action_grade": row.get("action") or "review",
        }
        for row in daily_brief
        if row.get("category") == "decide_now"
    ]
    decision_readiness = [
        {
            "stable_key": f"readiness:{row['stable_key']}",
            "symbol": row.get("symbol"),
            "status": row["readiness_status"],
            "next_action": row.get("action_grade"),
            "blockers": row.get("blockers") or [],
            "score": row.get("score"),
        }
        for row in decision_queue
    ]
    preopen = [{
        "stable_key": as_of.date().isoformat(),
        "brief_date": as_of.date().isoformat(),
        "generated_at": as_of,
        "session": "premarket",
        "status": "ready",
        "headline": f"{len(option_items) + len(review_rows)} decisions need attention",
        "summary": f"{len(holdings)} holdings, {len(option_items)} option setups, {len(review_rows)} thesis reviews, and {len(catalyst_rows)} near-term catalysts.",
    }]
    analysis = AnalysisRepository(runtime)
    run_id = analysis.start_run(
        "today-publication",
        input_cutoff=as_of,
        code_version="postgres-today-v1",
        inputs={
            "holdings": holdings,
            "reviews": reviews,
            "catalysts": catalysts,
            "option_decision_keys": [row.get("opportunity_id") or row.get("decision_id") for row in option_rows],
        },
        feature_versions={"daily_brief": "v1"},
    )
    publication_id = analysis.publish(
        run_id,
        "today",
        {
            "preopen_daily_brief": preopen,
            "daily_brief": daily_brief,
            "portfolio_risk_cards": portfolio_rows,
            "review_actions": review_rows,
            "decision_queue": decision_queue,
            "decision_readiness": decision_readiness,
            "symbol_decision_snapshots": decision_queue,
            "opportunities_ranked": option_items,
            "candidates": option_items,
            "feed_signals": daily_brief,
        },
        validation={"raw_and_analysis_separated": True, "row_count": len(daily_brief)},
        complete_run_summary={"daily_brief": len(daily_brief), "holdings": len(holdings)},
    )
    return {
        "status": "ok",
        "publication_id": str(publication_id),
        "daily_brief": len(daily_brief),
        "portfolio_pulse": len(portfolio_rows),
        "thesis_reviews": len(review_rows),
        "catalysts": len(catalyst_rows),
        "option_decisions": len(option_items),
    }


def _portfolio_pulse(row: dict[str, Any], holdings: list[dict[str, Any]]) -> dict[str, Any]:
    price = _number(row.get("price"))
    quantity = _number(row.get("quantity")) or 0
    cost = _number(row.get("average_cost"))
    market_value = price * quantity if price is not None else None
    total_value = sum(
        (_number(item.get("price")) or 0) * (_number(item.get("quantity")) or 0)
        for item in holdings
    )
    pnl = (price - cost) * quantity if price is not None and cost is not None else None
    weight = market_value / total_value if market_value is not None and total_value else None
    return {
        "stable_key": f"portfolio:{row['symbol']}",
        "instrument_id": row["instrument_id"],
        "category": "portfolio_pulse",
        "symbol": row["symbol"],
        "headline": f"{row['symbol']} portfolio pulse",
        "summary": "Latest position value and unrealized result from the newest raw quote.",
        "score": round((weight or 0) * 100, 2),
        "quantity": quantity,
        "price": price,
        "average_cost": cost,
        "market_value": market_value,
        "unrealized_pnl": pnl,
        "weight": weight,
        "quote_observed_at": row.get("quote_observed_at"),
    }


def _review_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "stable_key": f"thesis:{row['thesis_id']}",
        "instrument_id": row["instrument_id"],
        "category": "decide_now",
        "symbol": row["symbol"],
        "headline": f"Review {row['symbol']} thesis",
        "summary": row.get("thesis") or "Active thesis is due for review.",
        "action": "review_thesis",
        "score": 85 if row.get("last_reviewed_at") is None else 70,
        "invalidation": row.get("invalidation"),
        "last_reviewed_at": row.get("last_reviewed_at"),
    }


def _option_item(row: dict[str, Any]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or row.get("ticker") or "")
    return {
        "stable_key": f"option:{row.get('opportunity_id') or row.get('decision_id') or symbol}",
        "category": "decide_now",
        "symbol": symbol,
        "headline": f"Review {symbol} option setup",
        "summary": "; ".join(row.get("reasons") or []) or "Fresh option decision is available.",
        "action": row.get("state") or row.get("action") or "review",
        "score": _number(row.get("score")) or 0,
        "decision_id": row.get("decision_id"),
        "opportunity_id": row.get("opportunity_id"),
        "tier": row.get("tier"),
        "blockers": row.get("blockers") or [],
    }


def _catalyst_item(row: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    starts_at = row["starts_at"]
    return {
        "stable_key": f"catalyst:{row['event_id']}",
        "instrument_id": row.get("instrument_id"),
        "category": "catalysts",
        "symbol": row.get("symbol"),
        "headline": row["title"],
        "summary": row.get("notes") or row.get("expected_impact") or "Scheduled catalyst",
        "score": 60,
        "starts_at": starts_at,
        "days_until": max(0, (starts_at.date() - as_of.date()).days),
    }


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
