"""PostgreSQL-native daily brief and portfolio decision publication."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from typing import Any

from investment_panel.core.event_truth import build_options_decision_truth
from investment_panel.core.decision import MARKET_TZ
from investment_panel.database.analysis import AnalysisRepository, current_option_publication_rows
from investment_panel.database.agent_telemetry import AgentTelemetryRepository
from investment_panel.database.confirmed_daily_prices import confirmed_daily_bars
from investment_panel.database.portfolio_ledger import replay_portfolio_at
from investment_panel.database.preopen_context import compact_preopen_context
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.analysis.preopen_forecast import backtest_qqq_preopen_model, evaluate_qqq_forecast, qqq_preopen_forecast


def refresh_today_publication(
    runtime: DatabaseRuntime, *, now: datetime | None = None,
    use_agent_narrative: bool = False, agent_model: str = "gpt-5.6-luna",
    reasoning_effort: str = "high",
) -> dict[str, Any]:
    as_of = now or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("today publication timestamp must be timezone-aware")
    brief_date = as_of.astimezone(MARKET_TZ).date()
    with runtime.read() as connection:
        replay = replay_portfolio_at(None, as_of, connection=connection)
        replayed_positions = [dict(row) for row in replay.get("positions") or []]
        valid_instrument_ids = {
            int(row["id"])
            for row in connection.execute(
                "SELECT id FROM catalog.instrument "
                "WHERE id = ANY(%s::bigint[]) AND created_at <= %s",
                [[row["instrument_id"] for row in replayed_positions], as_of],
            ).fetchall()
        }
        holdings = [
            {**row, "average_cost": row.get("avg_cost")}
            for row in replayed_positions
            if int(row["instrument_id"]) in valid_instrument_ids
        ]
        held_instrument_ids = [int(row["instrument_id"]) for row in holdings]
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
                  AND instrument.created_at <= %s
                  AND thesis.created_at <= %s
                  AND thesis.updated_at <= %s
                  AND (
                    coalesce(thesis.thesis->>'core_thesis', '') = ''
                    OR thesis.thesis->>'last_reviewed' IS NULL
                    OR (thesis.thesis->>'last_reviewed')::timestamptz < %s - interval '45 days'
                  )
                ORDER BY thesis.thesis->>'last_reviewed' NULLS FIRST, instrument.symbol
                """,
                [as_of, as_of, as_of, as_of],
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
                LEFT JOIN LATERAL (
                    SELECT event_version.available_at, event_version.source_id,
                           event_version.title, event_version.starts_at
                    FROM raw.market_event_version event_version
                    JOIN ingest.run event_run ON event_run.id = event_version.ingest_run_id
                    WHERE event_version.market_event_id = catalyst.market_event_id
                      AND (catalyst.source_id IS NULL OR event_version.source_id = catalyst.source_id)
                      AND event_version.available_at <= %s
                      AND event_version.starts_at >= %s
                      AND event_run.status IN ('succeeded', 'partial')
                      AND event_run.finished_at IS NOT NULL
                      AND event_run.finished_at <= %s
                    ORDER BY event_version.available_at DESC, event_version.source_id,
                             event_version.id DESC
                    LIMIT 1
                ) event_lineage ON catalyst.market_event_id IS NOT NULL
                WHERE catalyst.created_at <= %s
                  AND (catalyst.superseded_at IS NULL OR catalyst.superseded_at > %s)
                  AND catalyst.starts_at >= %s AND catalyst.starts_at < %s + interval '14 days'
                  AND (
                    catalyst.market_event_id IS NULL
                    OR (
                      event_lineage.available_at IS NOT NULL
                      AND catalyst.title = event_lineage.title
                      AND catalyst.starts_at = event_lineage.starts_at
                    )
                  )
                  AND (
                    catalyst.instrument_id IS NULL
                    OR instrument.created_at <= %s
                  )
                ORDER BY catalyst.starts_at, event_lineage.available_at DESC,
                         event_lineage.source_id, catalyst.version, catalyst.id
                LIMIT 20
                """,
                [as_of, as_of, as_of, as_of, as_of, as_of, as_of, as_of],
            ).fetchall()
        ]
        option_rows = [
            {
                **dict(row["payload"] or {}),
                "publication_id": str(row["publication_id"]),
                "publication_published_at": row["published_at"].isoformat() if row["published_at"] else None,
            }
            for row in current_option_publication_rows(
                connection,
                scope="options-radar",
                model_name="option_radar_opportunity",
                cutoff=as_of,
            )
        ]
        option_rows.sort(
            key=lambda row: (
                _number(row.get("trade_rank")) is None,
                _number(row.get("trade_rank")) or 0,
            )
        )
        option_rows = option_rows[:10]
        qqq_instrument = connection.execute(
            "SELECT id FROM catalog.instrument WHERE symbol = 'QQQ' "
            "AND created_at <= %s",
            [as_of],
        ).fetchone()
        qqq_history = []
        if qqq_instrument is not None:
            qqq_market_date = brief_date
            qqq_history = [
                {"date": row["trading_date"], "close": row["close"]}
                for row in confirmed_daily_bars(
                    connection, [int(qqq_instrument["id"])], as_of=as_of, max_bars=281
                ).get(int(qqq_instrument["id"]), [])
                if row["trading_date"] < qqq_market_date
            ][-280:]
        qqq_actual_row = connection.execute(
            """
            SELECT quote.price, quote.observed_at, source.kind AS source_kind
            FROM raw.current_price_at(
                %s,
                ARRAY(
                    SELECT instrument.id FROM catalog.instrument instrument
                    WHERE instrument.symbol = 'QQQ'
                      AND instrument.created_at <= %s
                )::bigint[]
            ) quote
            JOIN catalog.instrument instrument ON instrument.id = quote.instrument_id
            JOIN ingest.source source ON source.id = quote.source_id
            WHERE instrument.symbol = 'QQQ'
              AND instrument.created_at <= %s
              AND quote.trading_date = %s
            LIMIT 1
            """,
            [as_of, as_of, as_of, brief_date],
        ).fetchone()
        prior_preopen_row = connection.execute(
            """
            SELECT item.payload
            FROM app.publication publication
            JOIN analysis.run publication_run ON publication_run.id = publication.analysis_run_id
            JOIN app.publication_content_item item ON item.publication_id = publication.id
            WHERE publication.scope = 'today' AND publication.status = 'published'
              AND publication.published_at IS NOT NULL
              AND publication.published_at <= %s
              AND publication_run.input_cutoff <= %s
              AND item.model_name = 'preopen_daily_brief'
              AND item.payload->>'brief_date' = %s
            ORDER BY publication.published_at DESC NULLS LAST LIMIT 1
            """,
            [as_of, as_of, brief_date.isoformat()],
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
                    JOIN analysis.run signal_run ON signal_run.id = signal.run_id
                    JOIN raw.content_item item ON item.id = signal.content_item_id
                    JOIN ingest.run content_run ON content_run.id = item.ingest_run_id
                    JOIN catalog.instrument instrument ON instrument.id = signal.instrument_id
                    JOIN ingest.source source ON source.id = item.source_id
                    WHERE source.enabled
                      AND source.operational_state = 'active'
                      AND source.created_at <= %s
                      AND instrument.created_at <= %s
                      AND signal.available_at IS NOT NULL
                      AND signal.available_at <= %s
                      AND signal.observed_at <= %s
                      AND (signal.event_at IS NULL OR signal.event_at <= %s)
                      AND (signal.published_at IS NULL OR signal.published_at <= %s)
                      AND signal_run.status IN ('succeeded', 'partial')
                      AND signal_run.input_cutoff <= %s
                      AND signal_run.finished_at IS NOT NULL
                      AND signal_run.finished_at <= %s
                      AND content_run.status IN ('succeeded', 'partial')
                      AND content_run.finished_at IS NOT NULL
                      AND content_run.finished_at <= %s
                      AND item.observed_at <= %s
                      AND COALESCE(item.published_at, item.observed_at) <= %s
                      AND (
                          instrument.id = ANY(%s::bigint[])
                          OR EXISTS (
                              SELECT 1 FROM app.watchlist_item watchlist
                              WHERE watchlist.instrument_id = instrument.id
                                AND watchlist.created_at <= %s
                                AND watchlist.updated_at <= %s
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
                [
                    as_of, as_of, as_of, as_of, as_of, as_of, as_of, as_of,
                    as_of, as_of, as_of, held_instrument_ids, as_of, as_of,
                ],
            ).fetchall()
        ]

    portfolio_rows = [_portfolio_pulse(row, holdings) for row in holdings]
    review_rows = [_review_item(row) for row in reviews]
    option_items = [_option_item(row) for row in option_rows]
    source_items = [_source_change_item(row) for row in source_changes]
    catalyst_rows = [_catalyst_item(row, as_of) for row in catalysts]
    forecast_history = qqq_history
    qqq_forecast = qqq_preopen_forecast(forecast_history)
    qqq_backtest = backtest_qqq_preopen_model(forecast_history)
    qqq_actual = dict(qqq_actual_row) if qqq_actual_row else None
    qqq_outcome = evaluate_qqq_forecast(qqq_forecast, qqq_actual)
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
        runtime=runtime, as_of=as_of, brief_date=brief_date, qqq_history=qqq_history, catalysts=catalysts,
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
        "stable_key": brief_date.isoformat(),
        "brief_date": brief_date.isoformat(),
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
        "forecast_as_of": as_of,
        "qqq_forecast": qqq_forecast,
        "backtest": qqq_backtest,
        "qqq_outcome": qqq_outcome,
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
    *, runtime: DatabaseRuntime, as_of: datetime, brief_date: date,
    qqq_history: list[dict[str, Any]], catalysts: list[dict[str, Any]],
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
            brief_date=brief_date.isoformat(),
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
    decision_truth = build_options_decision_truth(row, publication_id=row.get("publication_id"))
    return {
        "stable_key": f"option:{row.get('opportunity_id') or row.get('decision_id') or symbol}",
        "category": "decide_now",
        "symbol": symbol,
        "headline": f"Review {symbol} {str(row.get('structure') or 'option').replace('_', ' ')}",
        "summary": "; ".join(row.get("top_reasons") or row.get("reasons") or []) or "Fresh option decision is available.",
        "action": row.get("state") or row.get("action") or "review",
        "score": _number(row.get("score")) or 0,
        "ranking_version": row.get("ranking_version"),
        "research_rank": row.get("research_rank"),
        "trade_rank": row.get("trade_rank"),
        "trade_rank_unavailable_reason": row.get("trade_rank_unavailable_reason"),
        "execution_quality_score": row.get("execution_quality_score"),
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
        "decision_truth": decision_truth,
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


option_item = _option_item
