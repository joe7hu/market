"""Fast PostgreSQL-native option feature, decision, and publication pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from psycopg.types.json import Jsonb

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.database.strategy_parameters import normalize_gates
from investment_panel.database.options_publication import publication_models, publish_degraded_if_needed
from investment_panel.database.options_expressions import enrich_long_option_expectancy, insert_call_debit_spreads
from investment_panel.database.options_calibration import calibration_profiles, ready_structures
from investment_panel.database.options_retention import retain_reject_sample
from investment_panel.analysis.cash_secured_put import CashSecuredPutInputs, evaluate_cash_secured_put


FEATURE_VERSION = "option-professional-v2"
STRATEGY_KEY = "options-radar-core"
STRATEGY_REVISION = 2
DEFAULT_PARAMETERS = {
    "feature_version": FEATURE_VERSION,
    "contract_version": 2,
    "shadow_only": True,
    "score_weights": {"liquidity": 0.65, "convexity": 0.35},
    "gates": {"max_spread_pct": 0.25, "min_open_interest": 50, "min_dte": 2, "max_dte": 900},
    "cash_secured_put": {
        "min_dte": 21, "max_dte": 60, "delta_min": 0.15, "delta_max": 0.30,
        "max_ticker_nav_pct": 0.05, "max_aggregate_nav_pct": 0.15,
    },
}


def refresh_options_radar(
    runtime: DatabaseRuntime,
    *,
    source_id: str | None = None,
    symbols: Sequence[str] | None = None,
    code_version: str = "working-tree",
) -> dict[str, Any]:
    repository = AnalysisRepository(runtime)
    strategy_id, strategy_parameters = _active_strategy(runtime)
    cutoff = _latest_snapshot_time(runtime, source_id=source_id, symbols=symbols)
    if cutoff is None:
        return publish_degraded_if_needed(repository, code_version, FEATURE_VERSION, STRATEGY_KEY)
    run_id = repository.start_run(
        "options-radar",
        input_cutoff=cutoff,
        code_version=code_version,
        inputs={"source_id": source_id, "symbols": list(symbols or []), "cutoff": cutoff.isoformat()},
        feature_versions={"option": FEATURE_VERSION},
        strategy_revision_id=strategy_id,
    )
    try:
        calibrated_ready = ready_structures(runtime, strategy_id)
        feature_count = _insert_features(
            runtime,
            run_id,
            cutoff,
            source_id=source_id,
            symbols=symbols,
        )
        decision_count = _insert_decisions(runtime, run_id, strategy_id, strategy_parameters)
        empirical_long_options = enrich_long_option_expectancy(runtime, run_id, calibrated_ready)
        call_debit_spreads = insert_call_debit_spreads(runtime, repository, run_id, strategy_id, calibrated_ready)
        decision_count += call_debit_spreads
        cash_secured_puts = _insert_cash_secured_put_decisions(
            runtime, repository, run_id, strategy_id, strategy_parameters, calibrated_ready
        )
        decision_count += cash_secured_puts
        shadow_trades = _ensure_shadow_trades(runtime, run_id)
        models = publication_models(
            runtime,
            run_id,
            feature_version=FEATURE_VERSION,
            strategy_revision=STRATEGY_REVISION,
            scanned_contracts=feature_count,
        )
        models["option_calibration"] = calibration_profiles(runtime, strategy_id)
        publication_id = repository.publish(
            run_id,
            "options-radar",
            models,
            validation={
                "feature_count": feature_count,
                "decision_count": decision_count,
                "cash_secured_puts": cash_secured_puts,
                "empirical_long_options": empirical_long_options,
                "call_debit_spreads": call_debit_spreads,
                "shadow_trades": shadow_trades,
                "raw_payload_duplicated": False,
                "feature_version": FEATURE_VERSION,
            },
            complete_run_summary={
                "option_features": feature_count,
                "decisions": decision_count,
                "publication_models": {key: len(value) for key, value in models.items()},
            },
            strategy_root_key=STRATEGY_KEY,
        )
    except Exception as exc:
        repository.finish_run(run_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
        raise
    return {
        "status": "ok",
        "analysis_run_id": str(run_id),
        "publication_id": str(publication_id),
        "option_features": feature_count,
        "decisions": decision_count,
        "cash_secured_puts": cash_secured_puts,
        "empirical_long_options": empirical_long_options,
        "call_debit_spreads": call_debit_spreads,
        "shadow_trades": shadow_trades,
        "actionable": len(models["option_radar_opportunity"]),
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
        row = connection.execute(
            """
            SELECT max(snapshot.observed_at) FILTER (WHERE snapshot.market_session = 'regular') AS observed_at
            FROM raw.option_snapshot snapshot
            WHERE (CAST(%s AS text) IS NULL OR snapshot.source_id = %s)
              AND (
                  cardinality(%s::text[]) = 0 OR EXISTS (
                      SELECT 1 FROM raw.option_quote quote
                      JOIN catalog.option_contract contract ON contract.id = quote.contract_id
                      JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
                      WHERE quote.snapshot_id = snapshot.id AND instrument.symbol = ANY(%s::text[])
                  )
              )
            """,
            [source_id, source_id, normalized, normalized],
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
    # A publication is a complete replacement. ``symbols`` only scopes the
    # freshness cutoff that triggered this run; rebuilding must include every
    # symbol's latest snapshot so an incremental provider batch cannot erase
    # unchanged radar rows.
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
            [cutoff, source_id, source_id, run_id, FEATURE_VERSION],
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
        professional = [row for row in current if int(dict(row["parameters"] or {}).get("contract_version") or 0) >= 2]
        external_active = [row for row in current if str(row["strategy_key"]) != STRATEGY_KEY]
        if not professional and not external_active:
            connection.execute(
                "UPDATE analysis.strategy_revision SET status = 'superseded' "
                "WHERE authority_group = %s AND status = 'active'",
                [STRATEGY_KEY],
            )
        if not professional and not external_active:
            connection.execute(
                """
                INSERT INTO analysis.strategy_revision
                    (strategy_key, revision, name, status, parameters, authority_group, promoted_at)
                VALUES (%s, %s, 'Professional options radar', 'active', %s, %s, now())
                ON CONFLICT (strategy_key, revision) DO UPDATE
                SET status = 'active', promoted_at = COALESCE(analysis.strategy_revision.promoted_at, now())
                """,
                [STRATEGY_KEY, STRATEGY_REVISION, Jsonb(DEFAULT_PARAMETERS), STRATEGY_KEY],
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
                       quote.mid, quote.bid, quote.ask, quote.open_interest, quote.volume,
                       %s * feature.liquidity_score + %s * feature.convexity_score AS score,
                       array_remove(ARRAY[
                           CASE WHEN quote.underlying_price IS NULL OR quote.underlying_price <= 0 THEN 'missing_underlying' END,
                           CASE WHEN quote.bid IS NULL OR quote.ask IS NULL THEN 'incomplete_market' END,
                           CASE WHEN quote.bid < 0 OR quote.ask <= 0 OR quote.bid > quote.ask THEN 'crossed_or_empty_market' END,
                           CASE WHEN quote.mid IS NULL OR quote.mid <= 0 THEN 'missing_premium' END,
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
                quality_status, strategy_revision_id, reasons, blockers, input_hash
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
                   encode(digest(concat_ws('|', %s::text, contract_id::text, score::text), 'sha256'), 'hex')
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
                       'contract_version', 2,
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
        retain_reject_sample(connection, run_id)
        connection.execute(
            """
            WITH ranked AS (
                SELECT id, row_number() OVER (
                    PARTITION BY instrument_id ORDER BY (state = 'REJECTED'), score DESC NULLS LAST, id
                ) AS symbol_rank
                FROM analysis.decision WHERE run_id = %s
            )
            DELETE FROM analysis.option_decision option_decision
            USING ranked WHERE option_decision.decision_id = ranked.id
              AND ranked.symbol_rank > 12
            """,
            [run_id],
        )
        connection.execute(
            """
            WITH ranked AS (
                SELECT id, row_number() OVER (
                    PARTITION BY instrument_id ORDER BY (state = 'REJECTED'), score DESC NULLS LAST, id
                ) AS symbol_rank
                FROM analysis.decision WHERE run_id = %s
            )
            DELETE FROM analysis.decision decision
            USING ranked WHERE decision.id = ranked.id AND ranked.symbol_rank > 12
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


def _insert_cash_secured_put_decisions(
    runtime: DatabaseRuntime,
    repository: AnalysisRepository,
    run_id: Any,
    strategy_id: int,
    parameters: dict[str, Any],
    calibrated_ready: set[str],
) -> int:
    """Create the shadow-only cash-secured-put lane from the same quote cutoff."""

    csp = dict(parameters.get("cash_secured_put") or DEFAULT_PARAMETERS["cash_secured_put"])
    min_dte = int(csp.get("min_dte", 21))
    max_dte = int(csp.get("max_dte", 60))
    delta_min = float(csp.get("delta_min", 0.15))
    delta_max = float(csp.get("delta_max", 0.30))
    max_ticker_nav_pct = float(csp.get("max_ticker_nav_pct", 0.05))
    with runtime.read(JOB_PROFILE) as connection:
        account = connection.execute(
            """
            SELECT net_liquidation, cash_balance, buying_power, observed_at
            FROM raw.broker_account_snapshot
            ORDER BY observed_at DESC, id DESC LIMIT 1
            """
        ).fetchone()
        rows = connection.execute(
            """
            SELECT feature.snapshot_id, feature.contract_id, feature.quote_observed_at,
                   feature.dte, feature.spread_pct, feature.liquidity_score,
                   quote.underlying_price, quote.bid, quote.ask, quote.provider_iv,
                   quote.provider_delta, quote.open_interest, quote.volume,
                   contract.strike, contract.expiration, contract.multiplier,
                   instrument.id AS instrument_id, instrument.symbol, instrument.asset_class,
                   instrument.category,
                   EXISTS (
                       SELECT 1 FROM raw.fundamental_observation fundamental
                       WHERE fundamental.instrument_id = instrument.id
                   ) AS has_fundamentals,
                   (SELECT fundamental.values FROM raw.fundamental_observation fundamental
                    WHERE fundamental.instrument_id = instrument.id
                    ORDER BY fundamental.observed_at DESC, fundamental.id DESC LIMIT 1) AS quality_values,
                   (SELECT count(*) FROM raw.price_bar bar
                    WHERE bar.instrument_id = instrument.id AND bar.interval = '1d') AS history_observations,
                   EXISTS (
                       SELECT 1 FROM raw.market_event event
                       WHERE event.instrument_id = instrument.id AND event.event_kind = 'earnings'
                         AND event.starts_at::date > feature.quote_observed_at::date
                         AND event.starts_at::date <= contract.expiration
                   ) AS earnings_before_expiry
            FROM analysis.option_feature feature
            JOIN raw.option_quote quote
              ON quote.snapshot_id = feature.snapshot_id
             AND quote.contract_id = feature.contract_id
             AND quote.observed_at = feature.quote_observed_at
            JOIN catalog.option_contract contract ON contract.id = feature.contract_id
            JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
            WHERE feature.run_id = %s AND contract.option_type = 'put'
              AND feature.dte BETWEEN %s AND %s
              AND ABS(quote.provider_delta) BETWEEN %s AND %s
            ORDER BY feature.liquidity_score DESC, feature.contract_id
            """,
            [run_id, min_dte, max_dte, delta_min, delta_max],
        ).fetchall()

    created = 0
    for raw_row in rows:
        row = dict(raw_row)
        hard_blockers: list[str] = []
        if not row.get("has_fundamentals") and str(row.get("asset_class") or "") != "etf":
            hard_blockers.append("missing_quality_evidence")
        quality_values = dict(row.get("quality_values") or {})
        if str(quality_values.get("quality_status") or "").lower() in {"bad", "rejected", "unsafe"}:
            hard_blockers.append("company_quality_rejected")
        if int(row.get("history_observations") or 0) < 60:
            hard_blockers.append("insufficient_price_history")
        if row.get("earnings_before_expiry"):
            hard_blockers.append("earnings_before_expiry")
        if float(row.get("open_interest") or 0) < 50:
            hard_blockers.append("open_interest_too_low")
        spread_pct = _float(row.get("spread_pct"))
        if spread_pct is None or spread_pct > 0.25:
            hard_blockers.append("spread_too_wide")
        evaluation = evaluate_cash_secured_put(
            CashSecuredPutInputs(
                spot=_float(row.get("underlying_price")) or 0,
                strike=_float(row.get("strike")) or 0,
                dte=int(row.get("dte") or 0),
                bid=_float(row.get("bid")) or 0,
                ask=_float(row.get("ask")) or 0,
                delta=_float(row.get("provider_delta")) or 0,
                multiplier=int(row.get("multiplier") or 100),
                annualized_volatility=_float(row.get("provider_iv")),
            )
        )
        if evaluation is None:
            hard_blockers.append("invalid_cash_secured_put_market")
        if hard_blockers or evaluation is None:
            continue

        net_liquidation = _float(account.get("net_liquidation")) if account else None
        cash_balance = _float(account.get("cash_balance")) if account else None
        buying_power = _float(account.get("buying_power")) if account else None
        available_cash = min(value for value in (cash_balance, buying_power) if value is not None) if any(
            value is not None for value in (cash_balance, buying_power)
        ) else None
        sizing_blockers: list[str] = []
        if net_liquidation is None or available_cash is None:
            sizing_blockers.append("missing_cash_context")
        elif evaluation.secured_cash > available_cash:
            sizing_blockers.append("insufficient_cash_collateral")
        if net_liquidation and evaluation.secured_cash / net_liquidation > max_ticker_nav_pct:
            sizing_blockers.append("one_contract_exceeds_ticker_limit")
        max_contracts = 0
        if net_liquidation and available_cash is not None:
            max_contracts = max(
                0,
                int(min(available_cash, net_liquidation * max_ticker_nav_pct) // evaluation.secured_cash),
            )

        tail_ratio = evaluation.tail_cvar / evaluation.secured_cash
        expected_value = evaluation.entry_credit * (1 - evaluation.probability_assignment) - evaluation.tail_cvar * 0.05
        risk_adjusted = expected_value / evaluation.secured_cash
        score = max(
            0.0,
            min(
                100.0,
                0.45 * float(row.get("liquidity_score") or 0)
                + 35.0 * min(evaluation.annualized_return_on_collateral, 1.0)
                + 20.0 * (1.0 - min(tail_ratio, 1.0)),
            ),
        )
        details = {
            **evaluation.as_dict(),
            "contract_version": 2,
            "feature_version": FEATURE_VERSION,
            "probability_semantics": "provisional_uncalibrated",
            "provider_local_quote": True,
            "max_contracts": max_contracts,
            "available_cash": available_cash,
            "net_liquidation": net_liquidation,
            "management_plan": {
                "profit_review_pct": 0.50,
                "mandatory_review_dte": 21,
                "assignment_requires_quality_pass": True,
                "automatic_roll": False,
            },
            "quality_basis": {
                "fundamentals_present": bool(row.get("has_fundamentals")),
                "history_observations": int(row.get("history_observations") or 0),
                "earnings_before_expiry": False,
            },
        }
        repository.store_option_decision(
            run_id,
            decision_key=f"cash-secured-put:{row['contract_id']}",
            instrument_id=int(row["instrument_id"]),
            contract_id=int(row["contract_id"]),
            snapshot_id=int(row["snapshot_id"]),
            quote_observed_at=row["quote_observed_at"],
            state="READY" if "cash_secured_put" in calibrated_ready else "SETUP",
            score=round(score, 2),
            rank=None,
            inputs={"structure": "cash_secured_put", "row": row, "evaluation": details},
            reasons=("acceptable_assignment_entry", "cash_secured_income", "liquidity_supported"),
            blockers=sizing_blockers,
            details={
                "quality_status": "complete" if not sizing_blockers else "sizing_blocked",
                "premium_mid": row.get("bid"),
                "fill_assumption": row.get("bid"),
                "structure": "cash_secured_put",
                "entry_price": row.get("bid"),
                "exit_cost_estimate": max(0.0, (_float(row.get("ask")) or 0) - (_float(row.get("bid")) or 0)),
                "secured_cash": evaluation.secured_cash,
                "max_profit": evaluation.max_profit,
                "max_loss": evaluation.max_loss,
                "break_even": evaluation.break_even,
                "effective_assignment_price": evaluation.effective_assignment_price,
                "probability_profit": evaluation.probability_profit,
                "probability_assignment": evaluation.probability_assignment,
                "probability_touch": evaluation.probability_touch,
                "expected_value": expected_value,
                "risk_adjusted_expectancy": risk_adjusted,
                "tail_cvar": evaluation.tail_cvar,
                "data_confidence": 0.65,
                "execution_confidence": max(0.0, 1.0 - (spread_pct or 1.0)),
                "details": details,
            },
            strategy_revision_id=strategy_id,
        )
        created += 1
    return created


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
              AND COALESCE(option_decision.entry_price, option_decision.fill_assumption,
                           option_decision.premium_mid) > 0
            ON CONFLICT (decision_id) DO NOTHING
            """,
            [run_id],
        )
    return int(result.rowcount)

def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
