"""PostgreSQL-native daily brief and portfolio decision publication."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.agent_telemetry import AgentTelemetryRepository
from investment_panel.database.preopen_context import compact_preopen_context
from investment_panel.database.runtime import DatabaseRuntime


def refresh_today_publication(
    runtime: DatabaseRuntime, *, now: datetime | None = None,
    use_agent_narrative: bool = False, agent_model: str = "gpt-5.6-luna",
    reasoning_effort: str = "high",
) -> dict[str, Any]:
    as_of = now or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("today publication timestamp must be timezone-aware")
    with runtime.read() as connection:
        holdings = [
            dict(row)
            for row in connection.execute(
                """
                WITH positions AS MATERIALIZED (
                    SELECT instrument.id AS instrument_id, instrument.symbol,
                           position.quantity, position.average_cost, position.notes
                    FROM app.portfolio_position position
                    JOIN catalog.instrument instrument ON instrument.id = position.instrument_id
                ), current_prices AS MATERIALIZED (
                    SELECT *
                    FROM raw.current_price_at(
                        %s,
                        ARRAY(SELECT instrument_id FROM positions)::bigint[]
                    )
                )
                SELECT position.instrument_id, position.symbol, position.quantity,
                       position.average_cost, position.notes, quote.price,
                       quote.observed_at AS quote_observed_at
                FROM positions position
                LEFT JOIN current_prices quote ON quote.instrument_id = position.instrument_id
                ORDER BY position.symbol
                """,
                [as_of],
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
                WHERE catalyst.status = 'current'
                  AND catalyst.starts_at >= %s AND catalyst.starts_at < %s + interval '14 days'
                ORDER BY catalyst.starts_at LIMIT 20
                """,
                [as_of, as_of],
            ).fetchall()
        ]
        option_rows = [
            {
                **dict(row["payload"] or {}),
                "publication_id": str(row["publication_id"]),
                "publication_published_at": row["published_at"].isoformat() if row["published_at"] else None,
            }
            for row in connection.execute(
                """
                WITH latest AS (
                    SELECT id, published_at FROM app.publication
                    WHERE scope = 'options-radar' AND status = 'published'
                    ORDER BY published_at DESC NULLS LAST, created_at DESC LIMIT 1
                )
                SELECT item.payload, latest.id::text AS publication_id, latest.published_at
                FROM app.publication_content_item item
                JOIN latest ON latest.id = item.publication_id
                WHERE item.model_name = 'option_radar_opportunity'
                ORDER BY item.rank LIMIT 10
                """
            ).fetchall()
        ]
        qqq_history = [
            dict(row)
            for row in connection.execute(
                """
                SELECT date, close FROM (
                    SELECT DISTINCT ON (bar.trading_date)
                           bar.trading_date AS date, bar.close, bar.observed_at
                    FROM raw.price_bar bar
                    JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
                    WHERE instrument.symbol = 'QQQ' AND bar.interval = '1d'
                      AND bar.trading_date < %s
                    ORDER BY bar.trading_date DESC, bar.observed_at DESC
                ) daily
                ORDER BY date DESC
                LIMIT 280
                """,
                [as_of.date()],
            ).fetchall()
        ]
        prior_preopen_row = connection.execute(
            """
            SELECT item.payload
            FROM app.publication publication
            JOIN app.publication_content_item item ON item.publication_id = publication.id
            WHERE publication.scope = 'today' AND publication.status = 'published'
              AND item.model_name = 'preopen_daily_brief'
              AND item.payload->>'brief_date' = %s
            ORDER BY publication.published_at DESC NULLS LAST LIMIT 1
            """,
            [as_of.date().isoformat()],
        ).fetchone()
        source_changes = [
            dict(row)
            for row in connection.execute(
                """
                WITH ranked AS (
                    SELECT signal.id, instrument.id AS instrument_id, instrument.symbol,
                           signal.observed_at, signal.sentiment, signal.confidence,
                           signal.thesis, signal.antithesis, signal.invalidation,
                           signal.details, item.title, item.summary, item.url,
                           source.name AS source_name, item.source_id,
                           row_number() OVER (
                               PARTITION BY lower(source.name)
                               ORDER BY signal.observed_at DESC,
                                        signal.confidence DESC NULLS LAST, signal.id DESC
                           ) AS source_rank
                    FROM analysis.source_signal signal
                    JOIN raw.content_item item ON item.id = signal.content_item_id
                    JOIN catalog.instrument instrument ON instrument.id = signal.instrument_id
                    JOIN ingest.source source ON source.id = item.source_id
                    WHERE source.enabled
                      AND signal.observed_at <= %s
                      AND item.observed_at <= %s
                      AND COALESCE(item.published_at, item.observed_at) <= %s
                      AND (
                          EXISTS (
                              SELECT 1 FROM app.portfolio_position position
                              WHERE position.instrument_id = instrument.id
                          ) OR EXISTS (
                              SELECT 1 FROM app.watchlist_item watchlist
                              WHERE watchlist.instrument_id = instrument.id
                                AND watchlist.watch_state <> 'excluded'
                          )
                      )
                )
                SELECT id, instrument_id, symbol, observed_at, sentiment, confidence,
                       thesis, antithesis, invalidation, details, title, summary,
                       url, source_name
                FROM ranked
                WHERE source_rank <= 2
                ORDER BY observed_at DESC, confidence DESC NULLS LAST
                LIMIT 12
                """,
                [as_of, as_of, as_of],
            ).fetchall()
        ]

    portfolio_rows = [_portfolio_pulse(row, holdings) for row in holdings]
    review_rows = [_review_item(row) for row in reviews]
    option_items = [_option_item(row) for row in option_rows]
    source_items = [_source_change_item(row) for row in source_changes]
    catalyst_rows = [_catalyst_item(row, as_of) for row in catalysts]
    daily_brief = sorted(
        review_rows + source_items + catalyst_rows + portfolio_rows,
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
    prior_preopen = dict(prior_preopen_row["payload"] or {}) if prior_preopen_row else {}
    narrative, narrative_error, narrative_run_id = _agent_preopen_narrative(
        runtime=runtime,
        as_of=as_of, qqq_history=list(reversed(qqq_history)), catalysts=catalysts,
        source_changes=source_changes, option_rows=option_rows,
        enabled=use_agent_narrative and prior_preopen.get("status") != "agent_generated",
        model=agent_model, reasoning_effort=reasoning_effort,
    )
    preserved_agent_narrative = not narrative and prior_preopen.get("status") == "agent_generated"
    if preserved_agent_narrative:
        narrative = {key: prior_preopen.get(key) for key in (
            "headline", "summary", "macro_regime", "opening_scenario", "qqq_path", "risks", "watch_items"
        )}
        narrative["narrative"] = prior_preopen.get("summary")
        narrative_error = str(prior_preopen.get("error") or "")
        narrative_run_id = prior_preopen.get("agent_run_id")
    narrative_status = "agent_generated" if narrative else "deterministic_fallback"
    narrative_model = str(prior_preopen.get("model_name") or agent_model) if preserved_agent_narrative else agent_model
    narrative_effort = str(prior_preopen.get("reasoning_effort") or reasoning_effort) if preserved_agent_narrative else reasoning_effort
    preopen = [{
        "stable_key": as_of.date().isoformat(),
        "brief_date": as_of.date().isoformat(),
        "generated_at": as_of,
        "session": "premarket",
        "status": narrative_status,
        "model_name": narrative_model if narrative else "deterministic",
        "reasoning_effort": narrative_effort if narrative else "",
        "agent_run_id": narrative_run_id,
        "headline": (narrative or {}).get("headline") or f"{len(option_items) + len(review_rows)} decisions need attention",
        "summary": (narrative or {}).get("narrative") or f"{len(holdings)} holdings, {len(option_items)} option setups, {len(review_rows)} thesis reviews, {len(source_items)} source changes, and {len(catalyst_rows)} near-term catalysts.",
        "macro_regime": (narrative or {}).get("macro_regime"),
        "opening_scenario": (narrative or {}).get("opening_scenario"),
        "qqq_path": (narrative or {}).get("qqq_path"),
        "risks": (narrative or {}).get("risks") or [],
        "watch_items": (narrative or {}).get("watch_items") or [],
        "error": narrative_error,
        "option_publication_id": option_rows[0].get("publication_id") if option_rows else None,
        "option_publication_published_at": option_rows[0].get("publication_published_at") if option_rows else None,
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
            "source_signal_ids": [row.get("id") for row in source_changes],
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
            "decision_queue": decision_queue,
            "decision_readiness": decision_readiness,
            "symbol_decision_snapshots": decision_queue,
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
        "source_changes": len(source_items),
        "option_decisions": len(option_items),
        "option_actions": min(3, len(option_items)),
        "preopen_narrative": narrative_status,
    }


def _agent_preopen_narrative(
    *, runtime: DatabaseRuntime, as_of: datetime, qqq_history: list[dict[str, Any]], catalysts: list[dict[str, Any]],
    source_changes: list[dict[str, Any]], option_rows: list[dict[str, Any]], enabled: bool,
    model: str, reasoning_effort: str,
) -> tuple[dict[str, Any] | None, str, str | None]:
    if not enabled:
        return None, "", None
    telemetry = AgentTelemetryRepository(runtime)
    run_id: str | None = None
    try:
        from investment_panel.analysis.preopen_forecast import backtest_qqq_preopen_model, qqq_preopen_forecast
        from investment_panel.jobs.codex_preopen_brief import generate
        context = compact_preopen_context(
            brief_date=as_of.date().isoformat(),
            qqq_forecast=qqq_preopen_forecast(qqq_history),
            backtest=backtest_qqq_preopen_model(qqq_history),
            catalysts=catalysts, option_rows=option_rows, source_changes=source_changes,
        )
        provider = os.environ.get("MARKET_PREOPEN_BRIEF_PROVIDER", "codex").strip().lower()
        run_id = telemetry.start(
            workflow="preopen_narrative", provider=provider, model=model,
            trigger="scheduled_preopen", summary={"input_characters": len(str(context))},
        )
        result = generate(context, model=model, reasoning_effort=reasoning_effort)
        meta = result.pop("_meta", {}) if isinstance(result.get("_meta"), dict) else {}
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        telemetry.finish(run_id, status="succeeded", usage=usage)
        return result, "", run_id
    except Exception as exc:  # deterministic publication remains available
        error = f"{type(exc).__name__}: {exc}"[:1000]
        if run_id:
            meta = getattr(exc, "meta", {})
            usage = meta.get("usage") if isinstance(meta, dict) and isinstance(meta.get("usage"), dict) else {}
            telemetry.finish(run_id, status="failed", usage=usage, error=error)
        return None, error, run_id


def _source_change_item(row: dict[str, Any]) -> dict[str, Any]:
    details = dict(row.get("details") or {})
    confidence = _number(row.get("confidence"))
    sentiment = str(row.get("sentiment") or "neutral")
    summary = row.get("thesis") or row.get("summary") or row.get("title") or "Source evidence changed."
    return {
        "stable_key": f"source:{row['id']}",
        "instrument_id": row["instrument_id"],
        "category": "whats_changed",
        "symbol": row["symbol"],
        "headline": row.get("title") or f"{row['symbol']} source update",
        "summary": summary,
        "score": round((confidence if confidence is not None else 0.5) * 100, 2),
        "source": row.get("source_name"),
        "sentiment": sentiment,
        "antithesis": row.get("antithesis"),
        "invalidation": row.get("invalidation"),
        "evidence_refs": details.get("evidence_refs") or ([row["url"]] if row.get("url") else []),
        "observed_at": row.get("observed_at"),
        "next_action": "Review the source evidence against the current thesis before acting.",
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
        "headline": f"Review {symbol} {str(row.get('structure') or 'option').replace('_', ' ')}",
        "summary": "; ".join(row.get("top_reasons") or row.get("reasons") or []) or "Fresh option decision is available.",
        "action": row.get("state") or row.get("action") or "review",
        "score": _number(row.get("score")) or 0,
        "decision_id": row.get("decision_id"),
        "opportunity_id": row.get("opportunity_id"),
        "tier": row.get("tier"),
        "structure": row.get("structure"),
        "expiration": row.get("expiration"),
        "dte": row.get("dte"),
        "strike": row.get("strike"),
        "entry_price": row.get("entry_price") or row.get("ask"),
        "secured_cash": row.get("secured_cash"),
        "max_loss": row.get("max_loss"),
        "effective_assignment_price": row.get("effective_assignment_price"),
        "probability_profit": row.get("probability_profit"),
        "probability_assignment": row.get("probability_assignment"),
        "expected_value": row.get("expected_value"),
        "risk_adjusted_expectancy": row.get("risk_adjusted_expectancy"),
        "contract_version": row.get("contract_version"),
        "feature_version": row.get("feature_version"),
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
