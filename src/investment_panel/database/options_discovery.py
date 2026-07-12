"""Reproducible options discovery manifest and deterministic research gates."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Sequence

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


GATES = (
    "causal_exposure",
    "source_independence",
    "reference_class",
    "catalyst_hazard",
    "counterfactual",
    "edge_persistence",
    "falsifiability",
)


def materialize_discovery_foundation(
    runtime: DatabaseRuntime,
    run_id: Any,
    *,
    cutoff: datetime,
    contracts_evaluated: int,
    source_id: str | None = None,
    requested_scope: Sequence[str] | None = None,
) -> dict[str, int]:
    """Persist the exact evaluated universe and conservative discovery state."""
    evaluated_at = datetime.now(UTC)
    with runtime.read(JOB_PROFILE) as connection:
        rows = [dict(row) for row in connection.execute(
            _DISCOVERY_SQL,
            [run_id, run_id, cutoff, cutoff, cutoff, cutoff, cutoff, cutoff],
        ).fetchall()]
        evaluated_symbols = sorted(str(row["symbol"]) for row in connection.execute(
            """
            SELECT DISTINCT instrument.symbol
            FROM analysis.option_feature feature
            JOIN catalog.option_contract contract ON contract.id = feature.contract_id
            JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
            WHERE feature.run_id = %s ORDER BY instrument.symbol
            """,
            [run_id],
        ).fetchall())
        provider_runs = connection.execute(
            """
            SELECT DISTINCT ingest_run.id, ingest_run.summary, ingest_run.failure_detail,
                   snapshot.source_id, snapshot.market_session
            FROM raw.option_snapshot snapshot
            JOIN ingest.run ingest_run ON ingest_run.id = snapshot.ingest_run_id
            WHERE snapshot.observed_at = %s
              AND (CAST(%s AS text) IS NULL OR snapshot.source_id = %s)
            """,
            [cutoff, source_id, source_id],
        ).fetchall()
        contributing_runs = connection.execute(
            """
            SELECT DISTINCT snapshot.ingest_run_id AS id
            FROM analysis.option_feature feature
            JOIN raw.option_snapshot snapshot ON snapshot.id = feature.snapshot_id
            WHERE feature.run_id = %s ORDER BY snapshot.ingest_run_id
            """,
            [run_id],
        ).fetchall()
    provider_summaries = [dict(row["summary"] or {}) for row in provider_runs]
    requested_symbols = sorted({
        str(symbol).upper()
        for summary in provider_summaries
        for symbol in summary.get("symbols_requested") or []
    } | set(evaluated_symbols) | {
        str(symbol).strip().upper() for symbol in requested_scope or [] if str(symbol).strip()
    })
    with runtime.transaction(JOB_PROFILE) as connection:
        for symbol in requested_symbols:
            connection.execute(
                """
                INSERT INTO catalog.instrument (symbol, name, asset_class, category)
                VALUES (%s, %s, 'unknown', 'option-discovery')
                ON CONFLICT (symbol) DO NOTHING
                """,
                [symbol, symbol],
            )
    decision_set = {str(row["symbol"]) for row in rows}
    if requested_symbols:
        with runtime.read(JOB_PROFILE) as connection:
            placeholders = [dict(row) for row in connection.execute(
                _NO_CHAIN_SQL,
                [cutoff, cutoff, cutoff, cutoff, run_id, requested_symbols],
            ).fetchall()]
        rows.extend(row for row in placeholders if str(row["symbol"]) not in decision_set)
    manifest = {
        "symbols_requested": requested_symbols,
        "symbols_evaluated": evaluated_symbols,
        "provider_failures": sorted(
            error for summary in provider_summaries for error in summary.get("errors") or []
        ),
        "contributing_ingest_runs": [str(row["id"]) for row in contributing_runs],
        "inclusion_rule": "latest_regular_option_snapshot_at_or_before_cutoff",
        "exclusions": ["no_option_chain_at_cutoff"],
        "cutoff": cutoff.isoformat(),
        "counts_reproducible": True,
    }
    universe_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    run_provider = rows[0].get("source_id") if rows and rows[0].get("source_id") else (
        provider_runs[0]["source_id"] if provider_runs else source_id
    )
    run_session = rows[0].get("market_session") if rows and rows[0].get("market_session") else (
        provider_runs[0]["market_session"] if provider_runs else None
    )
    with runtime.transaction(JOB_PROFILE) as connection:
        connection.execute(
            """
            INSERT INTO analysis.option_discovery_run
                (run_id, universe_hash, started_at, completed_at, provider, market_session,
                 symbols_considered, symbols_with_chains, contracts_evaluated, manifest)
            VALUES (%s, %s, %s, now(), %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET completed_at = EXCLUDED.completed_at,
                symbols_considered = EXCLUDED.symbols_considered,
                symbols_with_chains = EXCLUDED.symbols_with_chains,
                contracts_evaluated = EXCLUDED.contracts_evaluated, manifest = EXCLUDED.manifest
            """,
            [
                run_id,
                universe_hash,
                evaluated_at,
                run_provider,
                run_session,
                len(requested_symbols),
                len(evaluated_symbols),
                contracts_evaluated,
                Jsonb(manifest),
            ],
        )
        for row in rows:
            candidate = _candidate(row, evaluated_at)
            connection.execute(
                """
                INSERT INTO analysis.option_discovery_candidate
                    (run_id, instrument_id, stage, discovery_score, surface_reason,
                     primary_edge, causal_exposure, catalyst_start, catalyst_end,
                     earliest_signal_at, timeliness, source_root_count,
                     evidence_completeness, data_readiness, execution_ready,
                     next_evidence, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, instrument_id) DO UPDATE SET
                    stage = EXCLUDED.stage, discovery_score = EXCLUDED.discovery_score,
                    surface_reason = EXCLUDED.surface_reason, data_readiness = EXCLUDED.data_readiness,
                    execution_ready = EXCLUDED.execution_ready, details = EXCLUDED.details
                """,
                [run_id, row["instrument_id"], *candidate],
            )
            for gate_code, passed, reason, evidence in _gate_results(row):
                connection.execute(
                    """
                    INSERT INTO analysis.option_gate_result
                        (run_id, instrument_id, gate_code, passed, reason, evidence)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, instrument_id, gate_code) DO UPDATE SET
                        passed = EXCLUDED.passed, reason = EXCLUDED.reason, evidence = EXCLUDED.evidence
                    """,
                    [run_id, row["instrument_id"], gate_code, passed, reason, Jsonb(evidence)],
                )
    return {"symbols_considered": len(requested_symbols), "symbols_with_chains": len(evaluated_symbols)}


def _candidate(row: dict[str, Any], evaluated_at: datetime) -> list[Any]:
    source_roots = int(row.get("source_root_count") or 0)
    has_catalyst = row.get("catalyst_start") is not None
    has_price_history = int(row.get("price_observations") or 0) >= 20
    quality_complete = bool(row.get("quality_complete"))
    bid_size = row.get("bid_size")
    ask_size = row.get("ask_size")
    quote_at = row.get("quote_observed_at")
    captured_at = row.get("captured_at") or quote_at
    last_trade_at = row.get("last_trade_at")
    live_ibkr_depth = row.get("source_id") == "ibkr" and row.get("market_data_status") == "live"
    age_minutes = _age_minutes(evaluated_at, captured_at)
    trade_age_minutes = _age_minutes(evaluated_at, last_trade_at)
    if (bid_size or 0) > 0 and (ask_size or 0) > 0 and age_minutes <= 5 and (
        trade_age_minutes <= 5 or live_ibkr_depth
    ):
        readiness = "A"
    elif age_minutes <= 20:
        readiness = "B"
    elif quote_at is not None and age_minutes <= 60 * 24 * 4:
        readiness = "C"
    else:
        readiness = "D"
    evidence = min(5, sum((source_roots >= 1, has_catalyst, has_price_history, quality_complete, readiness in {"A", "B"})))
    published = bool(row.get("published") and row.get("eligible") and quality_complete)
    stage = "DISCOVERED" if quote_at is None else "PUBLISHED" if published else "STRUCTURED" if quality_complete else "UNDERWRITING"
    reason_bits = []
    if source_roots:
        reason_bits.append(f"{source_roots} independent stored source root{'s' if source_roots != 1 else ''}")
    if has_catalyst:
        reason_bits.append("upcoming catalyst")
    reason_bits.append("current option chain" if quote_at is not None else "provider request returned no usable option chain")
    score = min(100.0, 25 + 12 * min(source_roots, 3) + (18 if has_catalyst else 0) + (12 if has_price_history else 0) + (9 if quality_complete else 0))
    direction = str(row.get("dominant_option_type") or "").lower()
    primary_edge = "direction" if direction in {"call", "put"} else "timing" if has_catalyst else "liquidity"
    next_evidence = _next_evidence(source_roots, has_catalyst, has_price_history, quality_complete)
    earliest = row.get("earliest_signal_at")
    if earliest is None:
        timeliness = "unknown"
    else:
        age_days = (evaluated_at - earliest).total_seconds() / 86400
        timeliness = "early" if age_days <= 3 else "timely" if age_days <= 30 else "late"
    details = {
        "quote_age_minutes": round(age_minutes, 2) if age_minutes != float("inf") else None,
        "last_trade_age_minutes": round(trade_age_minutes, 2) if trade_age_minutes != float("inf") else None,
        "provider": row.get("source_id"),
        "bid_size": bid_size,
        "ask_size": ask_size,
        "observed_vs_modeled": "provider_quote_with_provider_greeks",
        "freshness_basis": "live_depth" if live_ibkr_depth else "provider_last_trade",
        "probability_semantics": "provisional_uncalibrated",
        "module_status": {
            "reverse_expectations": "needs_underwriting",
            "reflexive_financing": "needs_underwriting",
            "special_situations": "needs_underwriting",
            "forensic_accounting": "needs_underwriting",
            "capital_cycle": "needs_underwriting",
            "factor_decomposition": "needs_underwriting",
            "superforecasting": "reference_class_required",
        },
    }
    causal = (
        "Stored company evidence plus an option chain; financial transmission still requires underwriting."
        if quote_at is not None
        else "Discovery evidence exists, but option-chain availability must be established before structuring."
    )
    return [
        stage, score, "; ".join(reason_bits), primary_edge, causal,
        row.get("catalyst_start"), row.get("catalyst_end"), earliest,
        timeliness, source_roots, evidence, readiness,
        False,
        next_evidence, Jsonb(details),
    ]


def _next_evidence(source_roots: int, has_catalyst: bool, has_price_history: bool, quality_complete: bool) -> str:
    if source_roots < 2:
        return "Add a second independent primary source and map the economic transmission."
    if not has_catalyst:
        return "Define a catalyst hazard window and probability of recognition before expiry."
    if not has_price_history:
        return "Build a point-in-time reference class before estimating payoff probabilities."
    if not quality_complete:
        return "Resolve deterministic quote, liquidity, and evidence gates."
    return "Complete counterfactual and falsification underwriting before execution staging."


def _age_minutes(evaluated_at: datetime, observed_at: datetime | None) -> float:
    if observed_at is None:
        return float("inf")
    age = (evaluated_at - observed_at).total_seconds() / 60
    return max(0.0, age) if age >= -1 else float("inf")


def _gate_results(row: dict[str, Any]) -> list[tuple[str, bool, str, dict[str, Any]]]:
    source_roots = int(row.get("source_root_count") or 0)
    checks = {
        "causal_exposure": (source_roots >= 1, "stored source evidence exists" if source_roots else "needs exposure attribution"),
        "source_independence": (source_roots >= 2, f"{source_roots} independent source roots"),
        "reference_class": (int(row.get("price_observations") or 0) >= 20, f"{int(row.get('price_observations') or 0)} point-in-time observations"),
        "catalyst_hazard": (row.get("catalyst_start") is not None, "catalyst window stored" if row.get("catalyst_start") else "no catalyst window"),
        "counterfactual": (False, "peer and negative-control comparison not yet stored"),
        "edge_persistence": (False, "counterparty or constraint not yet identified"),
        "falsifiability": (False, "objective invalidation requires underwriting"),
    }
    return [(code, checks[code][0], checks[code][1], {"deterministic": True}) for code in GATES]


_NO_CHAIN_SQL = """
WITH source_rollup AS (
    SELECT link.instrument_id, count(DISTINCT item.source_id) AS source_root_count,
           min(item.observed_at) AS earliest_signal_at
    FROM raw.content_item_instrument link
    JOIN raw.content_item item ON item.id = link.content_item_id
    WHERE item.observed_at <= %s GROUP BY link.instrument_id
), catalyst_rollup AS (
    SELECT instrument_id, min(starts_at)::date AS catalyst_start,
           (min(starts_at) + interval '7 days')::date AS catalyst_end
    FROM app.catalyst WHERE created_at <= %s AND starts_at >= %s GROUP BY instrument_id
), price_rollup AS (
    SELECT instrument_id, count(DISTINCT trading_date) AS price_observations
    FROM raw.price_bar WHERE interval = '1d' AND observed_at <= %s GROUP BY instrument_id
)
SELECT instrument.id AS instrument_id, instrument.symbol,
       false AS quality_complete, false AS eligible, NULL::double precision AS best_score,
       chain.option_type AS dominant_option_type, chain.quote_observed_at,
       chain.captured_at, chain.last_trade_at, chain.bid_size, chain.ask_size, chain.source_id,
       chain.market_session, false AS published, chain.market_data_status,
       coalesce(source_rollup.source_root_count, 0) AS source_root_count,
       source_rollup.earliest_signal_at, catalyst_rollup.catalyst_start,
       catalyst_rollup.catalyst_end, coalesce(price_rollup.price_observations, 0) AS price_observations,
       false AS calibrated
FROM catalog.instrument instrument
LEFT JOIN LATERAL (
    SELECT contract.option_type, quote.observed_at AS quote_observed_at, quote.captured_at,
           quote.last_trade_at, quote.bid_size, quote.ask_size,
           snapshot.source_id, snapshot.market_session, quote.market_data_status
    FROM analysis.option_feature feature
    JOIN catalog.option_contract contract ON contract.id = feature.contract_id
    JOIN raw.option_quote quote ON quote.snapshot_id = feature.snapshot_id
      AND quote.contract_id = feature.contract_id AND quote.observed_at = feature.quote_observed_at
    JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
    WHERE feature.run_id = %s AND contract.underlying_instrument_id = instrument.id
    ORDER BY feature.liquidity_score DESC NULLS LAST, feature.id LIMIT 1
) chain ON true
LEFT JOIN source_rollup ON source_rollup.instrument_id = instrument.id
LEFT JOIN catalyst_rollup ON catalyst_rollup.instrument_id = instrument.id
LEFT JOIN price_rollup ON price_rollup.instrument_id = instrument.id
WHERE instrument.symbol = ANY(%s::text[])
ORDER BY instrument.symbol
"""


_DISCOVERY_SQL = """
WITH structure_best AS (
    SELECT decision.instrument_id, option_decision.structure, max(decision.score) AS score
    FROM analysis.decision decision
    JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
    WHERE decision.run_id = %s AND decision.state <> 'REJECTED'
    GROUP BY decision.instrument_id, option_decision.structure
), ranked_structure AS (
    SELECT structure_best.*,
           row_number() OVER (ORDER BY score DESC NULLS LAST, instrument_id, structure) AS shortlist_rank
    FROM structure_best
), published_instrument AS (
    SELECT instrument_id, bool_or(shortlist_rank <= 10) AS published
    FROM ranked_structure GROUP BY instrument_id
), decision_rollup AS (
    SELECT decision.instrument_id,
           bool_or(decision.quality_status = 'complete') AS quality_complete,
           bool_or(decision.state <> 'REJECTED') AS eligible,
           max(decision.score) AS best_score,
           (array_agg(contract.option_type ORDER BY decision.score DESC NULLS LAST))[1] AS dominant_option_type,
           (array_agg(quote.observed_at ORDER BY decision.score DESC NULLS LAST, decision.id))[1] AS quote_observed_at,
           (array_agg(quote.captured_at ORDER BY decision.score DESC NULLS LAST, decision.id))[1] AS captured_at,
           (array_agg(quote.last_trade_at ORDER BY decision.score DESC NULLS LAST, decision.id))[1] AS last_trade_at,
           (array_agg(quote.bid_size ORDER BY decision.score DESC NULLS LAST, decision.id))[1] AS bid_size,
           (array_agg(quote.ask_size ORDER BY decision.score DESC NULLS LAST, decision.id))[1] AS ask_size,
           (array_agg(snapshot.source_id ORDER BY quote.observed_at DESC))[1] AS source_id,
           (array_agg(snapshot.market_session ORDER BY quote.observed_at DESC))[1] AS market_session,
           (array_agg(quote.market_data_status ORDER BY decision.score DESC NULLS LAST, decision.id))[1] AS market_data_status,
           coalesce(published_instrument.published, false) AS published
    FROM analysis.decision decision
    JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
    JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
    JOIN raw.option_quote quote ON quote.snapshot_id = option_decision.snapshot_id
      AND quote.contract_id = option_decision.contract_id AND quote.observed_at = option_decision.quote_observed_at
    JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
    LEFT JOIN published_instrument ON published_instrument.instrument_id = decision.instrument_id
    WHERE decision.run_id = %s
    GROUP BY decision.instrument_id, published_instrument.published
), source_rollup AS (
    SELECT link.instrument_id, count(DISTINCT item.source_id) AS source_root_count,
           min(item.observed_at) AS earliest_signal_at
    FROM raw.content_item_instrument link
    JOIN raw.content_item item ON item.id = link.content_item_id
    WHERE item.observed_at <= %s
    GROUP BY link.instrument_id
), catalyst_rollup AS (
    SELECT instrument_id, min(starts_at)::date AS catalyst_start,
           (min(starts_at) + interval '7 days')::date AS catalyst_end
    FROM app.catalyst
    WHERE created_at <= %s AND starts_at >= %s
    GROUP BY instrument_id
), price_rollup AS (
    SELECT instrument_id, count(DISTINCT trading_date) AS price_observations
    FROM raw.price_bar
    WHERE interval = '1d' AND observed_at <= %s
    GROUP BY instrument_id
), calibration AS (
    SELECT decision.instrument_id, count(*) >= 30 AS calibrated
    FROM analysis.option_outcome outcome
    JOIN analysis.decision decision ON decision.id = outcome.decision_id
    WHERE outcome.maturity_state IN ('mature', 'expired')
      AND outcome.observed_through <= %s AND decision.as_of <= %s
    GROUP BY decision.instrument_id
)
SELECT instrument.id AS instrument_id, instrument.symbol,
       decision_rollup.*, coalesce(source_rollup.source_root_count, 0) AS source_root_count,
       source_rollup.earliest_signal_at, catalyst_rollup.catalyst_start,
       catalyst_rollup.catalyst_end, coalesce(price_rollup.price_observations, 0) AS price_observations,
       coalesce(calibration.calibrated, false) AS calibrated
FROM decision_rollup
JOIN catalog.instrument instrument ON instrument.id = decision_rollup.instrument_id
LEFT JOIN source_rollup ON source_rollup.instrument_id = instrument.id
LEFT JOIN catalyst_rollup ON catalyst_rollup.instrument_id = instrument.id
LEFT JOIN price_rollup ON price_rollup.instrument_id = instrument.id
LEFT JOIN calibration ON calibration.instrument_id = instrument.id
ORDER BY decision_rollup.best_score DESC NULLS LAST, instrument.symbol
"""
