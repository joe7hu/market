"""Fast PostgreSQL-native option feature, decision, and publication pipeline."""
from __future__ import annotations
from datetime import UTC, datetime
from typing import Any, Sequence
from psycopg.types.json import Jsonb
from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.infrastructure.postgres.strategy_parameters import normalize_gates
from investment_panel.infrastructure.postgres.options_publication import publication_models, publish_degraded_if_needed
from investment_panel.infrastructure.postgres.options_expressions import (
    enrich_long_option_expectancy,
    insert_call_debit_spreads,
    insert_put_debit_spreads,
)
from investment_panel.infrastructure.postgres.options_calibration import calibration_profiles
from investment_panel.core.option_trade_ticket import calibrated_cohort_ready
from investment_panel.infrastructure.postgres.options_discovery import materialize_discovery_foundation
from investment_panel.infrastructure.postgres.options_cash_secured_put import (
    DEFAULT_PARAMETERS as DEFAULT_CASH_SECURED_PUT_PARAMETERS,
    insert_cash_secured_put_decisions,
)
from investment_panel.infrastructure.postgres.strategy_routes import apply_strategy_routes
from investment_panel.infrastructure.postgres.symbol_trends import refresh_symbol_trend_features
from investment_panel.infrastructure.postgres.event_studies import materialize_event_studies
FEATURE_VERSION = "option-professional-v3-ticket"
STRATEGY_KEY = "options-radar-core"
STRATEGY_REVISION = 3
IMPLEMENTATION_ID = "options_radar"
RADAR_QUOTE_SESSIONS = ("regular", "afterhours")
DEFAULT_PARAMETERS = {
    "feature_version": FEATURE_VERSION,
    "contract_version": 3,
    "shadow_only": True,
    "score_weights": {"liquidity": 0.65, "convexity": 0.35},
    "gates": {"max_spread_pct": 0.25, "min_open_interest": 50, "min_dte": 2, "max_dte": 900},
    "cash_secured_put": DEFAULT_CASH_SECURED_PUT_PARAMETERS,
}


def retain_reject_sample(connection: Any, run_id: Any, *, prune: bool = True) -> None:
    """Keep every one-blocker near miss and a stable 5% sample of other rejects."""
    sampled = "cardinality(decision.blockers) = 1 OR " \
        "mod(('x' || substr(md5(decision.decision_key), 1, 8))::bit(32)::bigint, 20) = 0"
    connection.execute(
        f"""
        INSERT INTO analysis.reject_summary
            (run_id, strategy_revision_id, instrument_id, gate_code, reject_count, sampled_decision_keys)
        SELECT decision.run_id, decision.strategy_revision_id, decision.instrument_id, blocker, count(*),
               COALESCE(array_agg(decision.decision_key ORDER BY decision.decision_key)
                   FILTER (WHERE {sampled}), '{{}}')
        FROM analysis.decision decision CROSS JOIN unnest(decision.blockers) blocker
        WHERE decision.run_id = %s AND decision.state = 'REJECTED'
        GROUP BY decision.run_id, decision.strategy_revision_id, decision.instrument_id, blocker
        """, [run_id],
    )
    if not prune:
        return
    connection.execute(
        f"""DELETE FROM analysis.option_decision option_decision USING analysis.decision decision
            WHERE option_decision.decision_id = decision.id AND decision.run_id = %s
              AND decision.state = 'REJECTED' AND NOT ({sampled})""", [run_id],
    )
    connection.execute(
        f"""DELETE FROM analysis.decision decision
            WHERE run_id = %s AND state = 'REJECTED' AND NOT ({sampled})""", [run_id],
    )


def refresh_options_radar(
    runtime: DatabaseRuntime,
    *,
    source_id: str | None = None,
    symbols: Sequence[str] | None = None,
    code_version: str = "working-tree",
    options_risk_sleeve_capital: float | None = None,
    config: object | None = None,
    candidate_revision_id: int | None = None,
) -> dict[str, Any]:
    from investment_panel.infrastructure.postgres.options_experiments import experiment_candidate, experiment_identity, incumbent_identity, seed_experiment_shadows

    repository = AnalysisRepository(runtime)
    candidate = None
    scope = "options-radar"
    if candidate_revision_id is None:
        strategy_id, strategy_parameters = _active_strategy(runtime)
    else:
        with runtime.read(JOB_PROFILE) as connection:
            candidate = experiment_candidate(connection, candidate_revision_id, as_of=datetime.now(UTC))
        strategy_id, strategy_parameters = candidate["id"], candidate["parameters"]
        scope = f"options-paper-experiment:{strategy_id}"
    cutoff = _latest_snapshot_time(runtime, source_id=source_id, symbols=symbols)
    if cutoff is None:
        if candidate:
            return {"status": "skipped", "reason": "fresh_prospective_quote_required"}
        return publish_degraded_if_needed(repository, code_version, FEATURE_VERSION, STRATEGY_KEY)
    if candidate:
        if not max(candidate["created_at"], candidate["proposal_created_at"]) <= cutoff <= datetime.now(UTC) or (datetime.now(UTC) - cutoff).total_seconds() > 300:
            return {"status": "skipped", "reason": "fresh_prospective_quote_required"}
        with runtime.read(JOB_PROFILE) as connection:
            previous = connection.execute(
                "SELECT publication.id::text FROM app.publication publication JOIN analysis.run run ON run.id = publication.analysis_run_id "
                "WHERE publication.scope = %s AND publication.status = 'published' AND run.status = 'succeeded' "
                "AND run.input_cutoff = %s AND run.strategy_revision_id = %s AND run.code_version = %s",
                [scope, cutoff, strategy_id, code_version],
            ).fetchone()
        if previous:
            seed_experiment_shadows(runtime, repository.publication_rows(scope, "option_paper_experiment"), publication_id=previous["id"])
            return {"status": "ok", "publication_id": previous["id"], "reused": True}
    run_id = repository.start_run(
        "options-paper-experiment" if candidate else "options-radar",
        input_cutoff=cutoff,
        code_version=code_version,
        inputs={"source_id": source_id, "symbols": list(symbols or []), "cutoff": cutoff.isoformat(),
                **({"experiment": experiment_identity(candidate)} if candidate else {"observation": incumbent_identity(strategy_id)})},
        feature_versions={"option": FEATURE_VERSION},
        strategy_revision_id=strategy_id,
    )
    try:
        calibration = calibration_profiles(
            runtime,
            strategy_id,
            feature_version=FEATURE_VERSION,
        )
        calibrated_ready = {
            str(row["structure"]) for row in calibration if calibrated_cohort_ready(row)
        }
        feature_count = _insert_features(
            runtime,
            run_id,
            cutoff,
            source_id=source_id,
            symbols=symbols,
        )
        trend_features = refresh_symbol_trend_features(runtime, run_id, as_of=cutoff)
        decision_count = _insert_decisions(runtime, run_id, strategy_id, strategy_parameters)
        regime = dict(trend_features["market_regime"])
        regime_known = regime.get("quality_status") == "complete" and regime.get("state") not in {None, "", "unknown", "unavailable"}
        try:
            regime_at = datetime.fromisoformat(str(regime.get("as_of") or "").replace("Z", "+00:00"))
        except ValueError:
            regime_at = None
        regime_known = regime_known and regime_at == cutoff
        unavailable_regime = {**regime, "quality_status": "unavailable", "reason_codes": sorted(set(
            list(regime.get("reason_codes") or []) + ["decision_cutoff_precedes_or_lacks_market_regime"]
        ))}
        with runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """UPDATE analysis.option_decision option_decision
                   SET market_regime = CASE WHEN decision.as_of = %s AND %s THEN %s ELSE NULL END,
                       market_regime_detail = CASE WHEN decision.as_of = %s AND %s THEN %s ELSE %s END
                   FROM analysis.decision decision
                   WHERE option_decision.decision_id = decision.id AND decision.run_id = %s
                     AND option_decision.structure IN ('long_call', 'long_put')""",
                [cutoff, regime_known, regime.get("state"), cutoff, regime_known,
                 Jsonb(regime), Jsonb(unavailable_regime), run_id],
            )
        empirical_long_options = enrich_long_option_expectancy(runtime, run_id, calibrated_ready)
        call_debit_spreads = 0 if candidate else insert_call_debit_spreads(runtime, repository, run_id, strategy_id, calibrated_ready)
        decision_count += call_debit_spreads
        put_debit_spreads = 0 if candidate else insert_put_debit_spreads(runtime, repository, run_id, strategy_id, calibrated_ready)
        decision_count += put_debit_spreads
        cash_secured_puts = 0 if candidate else insert_cash_secured_put_decisions(
            runtime, repository, run_id, strategy_id, strategy_parameters, calibrated_ready,
            evaluated_at=cutoff,
        )
        decision_count += cash_secured_puts
        try:
            event_studies = {
                "count": materialize_event_studies(runtime, run_id=run_id, as_of=cutoff),
                "state": "complete",
            }
        except Exception as error:
            event_studies = {"count": 0, "state": "unavailable", "error": type(error).__name__}
        strategy_routes = {"status": "experiment_core_gates_only"} if candidate else apply_strategy_routes(
            runtime,
            run_id,
            market_regime=dict(trend_features["market_regime"]),
            config=config,
            options_risk_sleeve_capital=options_risk_sleeve_capital,
        )
        shadow_trades = 0 if candidate else _ensure_shadow_trades(runtime, run_id)
        discovery = materialize_discovery_foundation(
            runtime, run_id, cutoff=cutoff, contracts_evaluated=feature_count,
            source_id=source_id, requested_scope=symbols,
        )
        previous_opportunities = repository.publication_rows_before(
            "options-radar", "option_radar_opportunity", cutoff=cutoff, source_id=source_id
        )
        models = publication_models(
            runtime,
            run_id,
            feature_version=FEATURE_VERSION,
            strategy_revision=STRATEGY_REVISION,
            scanned_contracts=feature_count,
            options_risk_sleeve_capital=options_risk_sleeve_capital,
            config=config,
            calibration=calibration,
            market_regime=dict(trend_features["market_regime"]),
            previous_opportunities=previous_opportunities,
        )
        actionable_count = len(models["option_radar_opportunity"])
        models["option_calibration"] = calibration
        experiment = experiment_identity(candidate, str(run_id)) if candidate else incumbent_identity(strategy_id, str(run_id))
        experiment_rows = [
            {**row, "experiment": experiment, "ticket": {**dict(row.get("ticket") or {}), "experiment": experiment}}
            for row in models["candidate_event"]
            if row.get("structure") in {"long_call", "long_put"}
        ]
        if candidate:
            models = {"option_paper_experiment": experiment_rows, "option_calibration": calibration}
        publication_id = repository.publish(
            run_id,
            scope,
            models,
            validation={
                "feature_count": feature_count,
                "decision_count": decision_count,
                "cash_secured_puts": cash_secured_puts,
                "empirical_long_options": empirical_long_options,
                "call_debit_spreads": call_debit_spreads,
                "put_debit_spreads": put_debit_spreads,
                "trend_features": trend_features,
                "strategy_routes": strategy_routes,
                "event_studies": event_studies,
                "shadow_trades": shadow_trades,
                "discovery": discovery,
                "raw_payload_duplicated": False,
                "feature_version": FEATURE_VERSION,
            },
            complete_run_summary={
                "option_features": feature_count,
                "decisions": decision_count,
                "publication_models": {key: len(value) for key, value in models.items()},
            },
            strategy_root_key=None if candidate else STRATEGY_KEY,
        )
    except Exception as exc:
        repository.finish_run(run_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
        raise
    observation_id = publication_id if candidate else repository.publish(
        run_id, f"options-paper-incumbent:{strategy_id}", {"option_paper_experiment": experiment_rows},
        validation={"observation_only": True, "version": experiment["version"]}, strategy_root_key=STRATEGY_KEY,
    )
    observation_scope = scope if candidate else f"options-paper-incumbent:{strategy_id}"
    shadow_trades += seed_experiment_shadows(
        runtime, repository.publication_rows(observation_scope, "option_paper_experiment"), publication_id=str(observation_id),
    )
    return {
        "status": "ok",
        "analysis_run_id": str(run_id),
        "publication_id": str(publication_id),
        "option_features": feature_count,
        "decisions": decision_count,
        "cash_secured_puts": cash_secured_puts,
        "empirical_long_options": empirical_long_options,
        "call_debit_spreads": call_debit_spreads,
        "put_debit_spreads": put_debit_spreads,
        "trend_features": trend_features,
        "strategy_routes": strategy_routes,
        "event_studies": event_studies,
        "shadow_trades": shadow_trades,
        "discovery": discovery,
        "actionable": actionable_count,
    }


def published_options_radar_rows(runtime: DatabaseRuntime, model_name: str) -> list[dict[str, Any]]:
    return AnalysisRepository(runtime).publication_rows("options-radar", model_name)


def _latest_snapshot_time(
    runtime: DatabaseRuntime,
    *,
    source_id: str | None,
    symbols: Sequence[str] | None,
) -> datetime | None:
    normalized = [str(symbol).strip().upper() for symbol in symbols or [] if str(symbol).strip()]
    with runtime.read() as connection:
        if not normalized:
            row = connection.execute(
                """
                SELECT max(snapshot.observed_at) AS observed_at
                FROM raw.option_snapshot snapshot
                WHERE snapshot.market_session = ANY(%s)
                  AND (CAST(%s AS text) IS NULL OR snapshot.source_id = %s)
                """,
                [list(RADAR_QUOTE_SESSIONS), source_id, source_id],
            ).fetchone()
            return row["observed_at"] if row else None

        # Most current collectors persist their requested symbol manifest.  Use
        # that bounded snapshot/run path first and only scan quote membership as
        # a compatibility fallback for older captures without a manifest.
        row = connection.execute(
            """
            SELECT max(snapshot.observed_at) AS observed_at
            FROM raw.option_snapshot snapshot
            JOIN ingest.run ingest_run ON ingest_run.id = snapshot.ingest_run_id
            WHERE snapshot.market_session = ANY(%s)
              AND (CAST(%s AS text) IS NULL OR snapshot.source_id = %s)
              AND COALESCE(ingest_run.summary->'symbols_requested', '[]'::jsonb) ?| %s::text[]
            """,
            [list(RADAR_QUOTE_SESSIONS), source_id, source_id, normalized],
        ).fetchone()
        if row and row["observed_at"] is not None:
            return row["observed_at"]
        row = connection.execute(
            """
            SELECT max(snapshot.observed_at) AS observed_at
            FROM raw.option_snapshot snapshot
            WHERE snapshot.market_session = ANY(%s)
              AND (CAST(%s AS text) IS NULL OR snapshot.source_id = %s)
              AND EXISTS (
                  SELECT 1
                  FROM raw.option_quote quote
                  JOIN catalog.option_contract contract ON contract.id = quote.contract_id
                  JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
                  WHERE quote.snapshot_id = snapshot.id
                    AND instrument.symbol = ANY(%s::text[])
              )
            """,
            [list(RADAR_QUOTE_SESSIONS), source_id, source_id, normalized],
        ).fetchone()
        return row["observed_at"] if row else None


def _insert_features(
    runtime: DatabaseRuntime,
    run_id: Any,
    cutoff: datetime,
    *,
    source_id: str | None,
    symbols: Sequence[str] | None,
) -> int:
    # Symbols only scope the triggering cutoff; replacement publications retain
    # each other symbol's latest valid snapshot.
    del symbols
    with runtime.transaction(JOB_PROFILE) as connection:
        result = connection.execute(
            """
            WITH latest_symbol_snapshot AS (
                SELECT DISTINCT ON (instrument.id)
                       instrument.id AS instrument_id, snapshot.id AS snapshot_id
                FROM raw.option_snapshot snapshot
                JOIN raw.option_quote quote ON quote.snapshot_id = snapshot.id
                JOIN catalog.option_contract contract ON contract.id = quote.contract_id
                JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
                WHERE snapshot.observed_at <= %s
                  AND (CAST(%s AS text) IS NULL OR snapshot.source_id = %s)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM raw.option_snapshot attempted
                      JOIN ingest.run attempted_run ON attempted_run.id = attempted.ingest_run_id
                      WHERE attempted.observed_at = %s
                        AND (CAST(%s AS text) IS NULL OR attempted.source_id = %s)
                        AND COALESCE(attempted_run.summary->'symbols_requested', '[]'::jsonb) ? instrument.symbol
                        AND NOT EXISTS (
                            SELECT 1 FROM raw.option_quote attempted_quote
                            JOIN catalog.option_contract attempted_contract
                              ON attempted_contract.id = attempted_quote.contract_id
                            WHERE attempted_quote.snapshot_id = attempted.id
                              AND attempted_contract.underlying_instrument_id = instrument.id
                        )
                  )
                ORDER BY instrument.id, snapshot.observed_at DESC, snapshot.id DESC
            )
            INSERT INTO analysis.option_feature (
                run_id, snapshot_id, contract_id, quote_observed_at, feature_version,
                modeled_iv, modeled_delta, modeled_gamma, modeled_theta, modeled_vega,
                dte, spread_pct, liquidity_score, convexity_score,
                required_2x_price, required_5x_price, required_10x_price,
                required_move_pct, metrics
            )
            SELECT
                %s, snapshot.id, quote.contract_id, quote.observed_at, %s,
                quote.provider_iv, quote.provider_delta, quote.provider_gamma,
                quote.provider_theta, quote.provider_vega,
                GREATEST(0, contract.expiration - quote.observed_at::date),
                CASE WHEN quote.mid > 0 AND quote.ask >= quote.bid
                     THEN (quote.ask - quote.bid) / quote.mid END,
                GREATEST(0, LEAST(100,
                    40 * (1 - LEAST(COALESCE((quote.ask - quote.bid) / NULLIF(quote.mid, 0), 1), 1))
                    + 30 * LEAST(COALESCE(quote.open_interest, 0)::double precision / 1000, 1)
                    + 30 * LEAST(COALESCE(quote.volume, 0)::double precision / 100, 1)
                )),
                GREATEST(0, LEAST(100,
                    5 * ABS(COALESCE(quote.provider_delta, 0)) * quote.underlying_price / NULLIF(quote.mid, 0)
                )),
                CASE WHEN contract.option_type = 'call' THEN contract.strike + 2 * quote.mid
                     WHEN contract.strike - 2 * quote.mid >= 0 THEN contract.strike - 2 * quote.mid END,
                CASE WHEN contract.option_type = 'call' THEN contract.strike + 5 * quote.mid
                     WHEN contract.strike - 5 * quote.mid >= 0 THEN contract.strike - 5 * quote.mid END,
                CASE WHEN contract.option_type = 'call' THEN contract.strike + 10 * quote.mid
                     WHEN contract.strike - 10 * quote.mid >= 0 THEN contract.strike - 10 * quote.mid END,
                ABS(
                    (CASE WHEN contract.option_type = 'call' THEN contract.strike + 10 * quote.mid
                          WHEN contract.strike - 10 * quote.mid >= 0 THEN contract.strike - 10 * quote.mid END) - quote.underlying_price
                ) / NULLIF(quote.underlying_price, 0),
                jsonb_build_object(
                    'pricing_model', 'strike_plus_premium_proxy_v1',
                    'source_id', snapshot.source_id,
                    'market_session', snapshot.market_session
                )
            FROM raw.option_snapshot snapshot
            JOIN raw.option_quote quote ON quote.snapshot_id = snapshot.id
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
            JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
            JOIN latest_symbol_snapshot latest
              ON latest.snapshot_id = snapshot.id AND latest.instrument_id = instrument.id
            ON CONFLICT (run_id, snapshot_id, contract_id, feature_version) DO NOTHING
            """,
            [cutoff, source_id, source_id, cutoff, source_id, source_id, run_id, FEATURE_VERSION],
        )
    return int(result.rowcount)

def _active_strategy(runtime: DatabaseRuntime) -> tuple[int, dict[str, Any]]:
    """Return the promoted strategy in the core lineage without rewriting it."""
    with runtime.transaction() as connection:
        connection.execute(
            """
            SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))
            """,
            [f"strategy:{STRATEGY_KEY}"],
        )
        current = connection.execute(
            "SELECT id, strategy_key, revision, parameters FROM analysis.strategy_revision "
            "WHERE authority_group = %s AND status = 'active' FOR UPDATE",
            [STRATEGY_KEY],
        ).fetchall()
        professional = [row for row in current if int(dict(row["parameters"] or {}).get("contract_version") or 0) >= 3]
        external_active = [row for row in current if str(row["strategy_key"]) != STRATEGY_KEY]
        if not professional and not external_active:
            connection.execute(
                "UPDATE analysis.strategy_revision SET status = 'superseded' "
                "WHERE authority_group = %s AND status = 'active'",
                [STRATEGY_KEY],
            )
        if not professional and not external_active:
            existing = connection.execute(
                "SELECT implementation_id, implementation_version FROM analysis.strategy_revision "
                "WHERE strategy_key = %s AND revision = %s",
                [STRATEGY_KEY, STRATEGY_REVISION],
            ).fetchone()
            if existing is not None and (
                existing["implementation_id"] != IMPLEMENTATION_ID
                or existing["implementation_version"] != FEATURE_VERSION
            ):
                raise ValueError("options radar strategy implementation identity is immutable")
            connection.execute(
                """
                INSERT INTO analysis.strategy_revision
                    (strategy_key, revision, name, status, parameters, authority_group,
                     implementation_id, implementation_version, promoted_at)
                VALUES (%s, %s, 'Professional options radar', 'active', %s, %s, %s, %s, now())
                ON CONFLICT (strategy_key, revision) DO UPDATE
                SET name = EXCLUDED.name, status = 'active', parameters = EXCLUDED.parameters,
                    authority_group = EXCLUDED.authority_group,
                    promoted_at = COALESCE(analysis.strategy_revision.promoted_at, now())
                """,
                [STRATEGY_KEY, STRATEGY_REVISION, Jsonb(DEFAULT_PARAMETERS), STRATEGY_KEY, IMPLEMENTATION_ID, FEATURE_VERSION],
            )
        row = connection.execute(
            """
            SELECT revision.id, revision.parameters
            FROM analysis.strategy_revision revision
            WHERE revision.authority_group = %s AND revision.status = 'active'
            ORDER BY revision.promoted_at DESC NULLS LAST, revision.id DESC
            """,
            [STRATEGY_KEY],
        ).fetchall()
    if len(row) != 1:
        raise RuntimeError(
            f"options radar requires exactly one active strategy revision; found {len(row)}"
        )
    row = row[0]
    return int(row["id"]), dict(row["parameters"] or {})


def _insert_decisions(
    runtime: DatabaseRuntime,
    run_id: Any,
    strategy_id: int,
    parameters: dict[str, Any],
) -> int:
    weights = dict(parameters.get("score_weights") or {})
    liquidity_weight = float(weights.get("liquidity", 0.65))
    convexity_weight = float(weights.get("convexity", 0.35))
    gates = normalize_gates(parameters)
    max_spread = gates.get("max_spread_pct", 0.25)
    min_open_interest = gates.get("min_open_interest", 50)
    min_volume = gates.get("min_volume")
    min_dte = gates.get("min_dte", 14)
    max_dte = gates.get("max_dte", 900)
    delta_min = gates.get("delta_min")
    delta_max = gates.get("delta_max")
    max_required_move = gates.get("max_required_move_pct")
    max_iv_percentile = gates.get("max_iv_percentile")
    with runtime.transaction(JOB_PROFILE) as connection:
        result = connection.execute(
            """
            WITH scored AS (
                SELECT feature.*,
                       instrument.id AS instrument_id,
                       contract.option_type,
                       analysis_run.feature_versions,
                       quote.mid, quote.bid, quote.ask, quote.open_interest, quote.volume,
                       %s * feature.liquidity_score + %s * feature.convexity_score AS score,
                       array_remove(ARRAY[
                           CASE WHEN quote.underlying_price IS NULL OR quote.underlying_price <= 0 THEN 'missing_underlying' END,
                           CASE WHEN quote.bid IS NULL OR quote.ask IS NULL THEN 'incomplete_market' END,
                           CASE WHEN quote.bid < 0 OR quote.ask <= 0 OR quote.bid > quote.ask THEN 'crossed_or_empty_market' END,
                           CASE WHEN quote.mid IS NULL OR quote.mid <= 0 THEN 'missing_premium' END,
                           CASE WHEN NOT quote.standard_contract_verified
                             OR quote.contract_style <> 'american'
                             OR quote.contract_settlement <> 'physical'
                             OR quote.contract_deliverable_key IS NULL
                             THEN 'standard_contract_terms_unverified' END,
                           CASE WHEN snapshot.market_session <> 'regular' THEN 'not_regular_session' END,
                           CASE WHEN quote.observed_at < analysis_run.input_cutoff - interval '90 minutes' THEN 'stale_quote' END,
                           CASE WHEN feature.spread_pct IS NULL THEN 'missing_spread' END,
                           CASE WHEN feature.spread_pct > %s THEN 'spread_too_wide' END,
                           CASE WHEN COALESCE(quote.open_interest, 0) < %s THEN 'open_interest_too_low' END,
                           CASE WHEN %s::double precision IS NOT NULL AND COALESCE(quote.volume, 0) < %s::double precision THEN 'volume_too_low' END,
                           CASE WHEN feature.dte < %s OR feature.dte > %s THEN 'dte_out_of_range' END,
                           CASE WHEN %s::double precision IS NOT NULL AND (feature.modeled_delta IS NULL OR ABS(feature.modeled_delta) < %s::double precision) THEN 'delta_too_low' END,
                           CASE WHEN %s::double precision IS NOT NULL AND (feature.modeled_delta IS NULL OR ABS(feature.modeled_delta) > %s::double precision) THEN 'delta_too_high' END,
                           CASE WHEN %s::double precision IS NOT NULL AND (feature.required_move_pct IS NULL OR feature.required_move_pct > %s::double precision) THEN 'required_move_too_high' END,
                           CASE WHEN %s::double precision IS NOT NULL AND (feature.iv_percentile IS NULL OR feature.iv_percentile > %s::double precision) THEN 'iv_percentile_too_high' END
                       ], NULL) AS blockers
                FROM analysis.option_feature feature
                JOIN raw.option_quote quote
                  ON quote.snapshot_id = feature.snapshot_id
                 AND quote.contract_id = feature.contract_id
                 AND quote.observed_at = feature.quote_observed_at
                JOIN catalog.option_contract contract ON contract.id = feature.contract_id
                JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
                JOIN raw.option_snapshot snapshot ON snapshot.id = feature.snapshot_id
                JOIN analysis.run analysis_run ON analysis_run.id = feature.run_id
                WHERE feature.run_id = %s
            ), ranked AS (
                SELECT scored.*,
                       row_number() OVER (ORDER BY score DESC, contract_id) AS decision_rank
                FROM scored
            )
            INSERT INTO analysis.decision (
                run_id, decision_key, kind, instrument_id, as_of, state, rank, score,
                quality_status, strategy_revision_id, reasons, blockers, input_hash,
                lane, episode_key, sample_eligible, quarantine_reason, calibration_cohort
            )
            SELECT %s, contract_id::text, 'option', instrument_id, quote_observed_at,
                   CASE WHEN cardinality(blockers) > 0 THEN 'REJECTED'
                        WHEN score >= 85 THEN 'SETUP'
                        WHEN score >= 70 THEN 'SETUP'
                        WHEN score >= 55 THEN 'WATCH'
                        ELSE 'REJECTED' END,
                   decision_rank, round(score::numeric, 2),
                   CASE WHEN cardinality(blockers) = 0 THEN 'complete' ELSE 'gated' END,
                   %s,
                   array_remove(ARRAY[
                       CASE WHEN liquidity_score >= 70 THEN 'liquidity_supported' END,
                       CASE WHEN convexity_score >= 70 THEN 'convexity_supported' END
                   ], NULL),
                   blockers,
                   encode(digest(concat_ws('|', %s::text, contract_id::text, score::text), 'sha256'), 'hex'),
                   'radar',
                   concat_ws(
                       ':', 'radar', instrument_id::text,
                       concat('long_', option_type),
                       to_char((quote_observed_at AT TIME ZONE 'America/New_York')::date, 'YYYYMMDD')
                   ),
                   cardinality(blockers) = 0,
                   CASE WHEN cardinality(blockers) = 0 THEN NULL ELSE 'quality_gated' END,
                   concat('option-scorecard-truth-v1:', coalesce(feature_versions->>'option', 'unknown'))
            FROM ranked
            """,
            [
                liquidity_weight, convexity_weight, max_spread, min_open_interest,
                min_volume, min_volume, min_dte, max_dte,
                delta_min, delta_min, delta_max, delta_max,
                max_required_move, max_required_move,
                max_iv_percentile, max_iv_percentile,
                run_id, run_id, strategy_id, run_id,
            ],
        )
        connection.execute(
            """
            INSERT INTO analysis.option_decision (
                decision_id, contract_id, snapshot_id, quote_observed_at,
                premium_mid, fill_assumption, required_move_pct, buy_under, tier,
                structure, entry_price, exit_cost_estimate, max_loss,
                data_confidence, execution_confidence, details
            )
            SELECT decision.id, feature.contract_id, feature.snapshot_id, feature.quote_observed_at,
                   quote.mid, quote.ask, feature.required_move_pct,
                   CASE WHEN quote.bid IS NOT NULL AND quote.ask IS NOT NULL
                        THEN quote.bid + 0.35 * (quote.ask - quote.bid) ELSE quote.mid END,
                   CASE WHEN decision.state = 'SETUP' THEN 'setup'
                        WHEN decision.state = 'WATCH' THEN 'watch' ELSE 'rejected' END,
                   CASE WHEN contract.option_type = 'call' THEN 'long_call' ELSE 'long_put' END,
                   quote.ask,
                   CASE WHEN quote.bid IS NOT NULL AND quote.ask IS NOT NULL THEN quote.ask - quote.bid END,
                   quote.ask * COALESCE(contract.multiplier, 100),
                   CASE WHEN quote.provider_iv IS NOT NULL AND quote.provider_delta IS NOT NULL THEN 0.8 ELSE 0.5 END,
                   GREATEST(0, LEAST(1, 1 - COALESCE(feature.spread_pct, 1))),
                   jsonb_build_object(
                       'contract_version', 3,
                       'feature_version', feature.feature_version,
                       'probability_semantics', 'provisional_uncalibrated',
                       'provider_local_quote', true
                   )
            FROM analysis.decision decision
            JOIN analysis.option_feature feature
              ON feature.run_id = decision.run_id AND feature.contract_id::text = decision.decision_key
            JOIN catalog.option_contract contract ON contract.id = feature.contract_id
            JOIN raw.option_quote quote
              ON quote.snapshot_id = feature.snapshot_id AND quote.contract_id = feature.contract_id
             AND quote.observed_at = feature.quote_observed_at
            WHERE decision.run_id = %s
            """,
            [run_id],
        )
        # The first opportunity of each symbol/structure/session remains in the
        # prospective denominator even when every contract fails a gate.
        retain_reject_sample(connection, run_id, prune=False)
        # Candidate and incumbent revisions must expose the same contracts to
        # expectancy scoring. Independent observations are selected afterwards.
        connection.execute(
            """
            WITH ranked AS (
                SELECT id, row_number() OVER (
                    PARTITION BY instrument_id ORDER BY (state = 'REJECTED'), score DESC NULLS LAST, decision_key
                ) AS symbol_rank, row_number() OVER (
                    PARTITION BY episode_key ORDER BY (state = 'REJECTED'), score DESC NULLS LAST, decision_key
                ) AS episode_rank
                FROM analysis.decision WHERE run_id = %s
            )
            DELETE FROM analysis.option_decision option_decision
            USING ranked WHERE option_decision.decision_id = ranked.id
              AND ranked.symbol_rank > 12 AND ranked.episode_rank > 1
            """,
            [run_id],
        )
        connection.execute(
            """
            WITH ranked AS (
                SELECT id, row_number() OVER (
                    PARTITION BY instrument_id ORDER BY (state = 'REJECTED'), score DESC NULLS LAST, decision_key
                ) AS symbol_rank, row_number() OVER (
                    PARTITION BY episode_key ORDER BY (state = 'REJECTED'), score DESC NULLS LAST, decision_key
                ) AS episode_rank
                FROM analysis.decision WHERE run_id = %s
            )
            DELETE FROM analysis.decision decision
            USING ranked WHERE decision.id = ranked.id AND ranked.symbol_rank > 12 AND ranked.episode_rank > 1
            """,
            [run_id],
        )
        connection.execute(
            """
            DELETE FROM analysis.option_feature feature
            WHERE feature.run_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM analysis.option_decision option_decision
                  JOIN analysis.decision decision ON decision.id = option_decision.decision_id
                  WHERE decision.run_id = feature.run_id
                    AND option_decision.contract_id = feature.contract_id
                    AND option_decision.snapshot_id = feature.snapshot_id
                    AND option_decision.quote_observed_at = feature.quote_observed_at
              )
            """,
            [run_id],
        )
        actionable = connection.execute(
            "SELECT count(*) AS count FROM analysis.decision WHERE run_id = %s AND state <> 'REJECTED'", [run_id]
        ).fetchone()["count"]
    return int(actionable)



def _ensure_shadow_trades(runtime: DatabaseRuntime, run_id: Any) -> int:
    """Open one immutable paper observation for every retained signal."""

    with runtime.transaction(JOB_PROFILE) as connection:
        result = connection.execute(
            """
            INSERT INTO analysis.shadow_trade
                (decision_id, entry_at, entry_price, status, metrics)
            SELECT decision.id, decision.as_of,
                   COALESCE(option_decision.entry_price, option_decision.fill_assumption,
                            option_decision.premium_mid),
                   'observing',
                   jsonb_build_object(
                       'structure', option_decision.structure,
                       'secured_cash', option_decision.secured_cash,
                       'entry_basis', CASE
                           WHEN option_decision.structure = 'cash_secured_put' THEN 'provider_bid'
                           ELSE 'provider_ask'
                       END,
                       'provider_local_quote', true
                   )
            FROM analysis.decision decision
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            WHERE decision.run_id = %s AND decision.state IN ('WATCH', 'SETUP', 'READY')
              AND option_decision.structure NOT IN ('long_call', 'long_put')
              AND COALESCE(option_decision.entry_price, option_decision.fill_assumption,
                           option_decision.premium_mid) > 0
            ON CONFLICT (decision_id) DO NOTHING
            """,
            [run_id],
        )
    return int(result.rowcount)
