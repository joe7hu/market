"""Fast PostgreSQL-native option feature, decision, and publication pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from psycopg.types.json import Jsonb

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.database.strategy_parameters import normalize_gates


FEATURE_VERSION = "option-core-v1"
STRATEGY_KEY = "options-radar-core"
STRATEGY_REVISION = 1
DEFAULT_PARAMETERS = {
    "feature_version": FEATURE_VERSION,
    "score_weights": {"liquidity": 0.65, "convexity": 0.35},
    "gates": {"max_spread_pct": 0.25, "min_open_interest": 50, "min_dte": 14, "max_dte": 900},
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
        return {"status": "skipped", "reason": "no_option_snapshot", "option_features": 0, "decisions": 0}
    run_id = repository.start_run(
        "options-radar",
        input_cutoff=cutoff,
        code_version=code_version,
        inputs={"source_id": source_id, "symbols": list(symbols or []), "cutoff": cutoff.isoformat()},
        feature_versions={"option": FEATURE_VERSION},
        strategy_revision_id=strategy_id,
    )
    try:
        feature_count = _insert_features(
            runtime,
            run_id,
            cutoff,
            source_id=source_id,
            symbols=symbols,
        )
        decision_count = _insert_decisions(runtime, run_id, strategy_id, strategy_parameters)
        models = _publication_models(runtime, run_id)
        publication_id = repository.publish(
            run_id,
            "options-radar",
            models,
            validation={
                "feature_count": feature_count,
                "decision_count": decision_count,
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
            SELECT max(snapshot.observed_at) AS observed_at
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
                     ELSE contract.strike - 2 * quote.mid END,
                CASE WHEN contract.option_type = 'call' THEN contract.strike + 5 * quote.mid
                     ELSE contract.strike - 5 * quote.mid END,
                CASE WHEN contract.option_type = 'call' THEN contract.strike + 10 * quote.mid
                     ELSE contract.strike - 10 * quote.mid END,
                ABS(
                    (CASE WHEN contract.option_type = 'call' THEN contract.strike + 10 * quote.mid
                          ELSE contract.strike - 10 * quote.mid END) - quote.underlying_price
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
            INSERT INTO analysis.strategy_revision
                (strategy_key, revision, name, status, parameters, authority_group, promoted_at)
            VALUES (%s, %s, 'Storage-efficient options radar', 'active', %s, %s, now())
            ON CONFLICT (strategy_key, revision) DO NOTHING
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
                           CASE WHEN quote.mid IS NULL OR quote.mid <= 0 THEN 'missing_premium' END,
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
                   CASE WHEN cardinality(blockers) > 0 THEN 'REJECT'
                        WHEN score >= 85 THEN 'FIRE'
                        WHEN score >= 70 THEN 'SETUP'
                        WHEN score >= 55 THEN 'WATCH'
                        ELSE 'REJECT' END,
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
                premium_mid, fill_assumption, required_move_pct, buy_under, tier
            )
            SELECT decision.id, feature.contract_id, feature.snapshot_id, feature.quote_observed_at,
                   quote.mid, quote.ask, feature.required_move_pct,
                   CASE WHEN quote.bid IS NOT NULL AND quote.ask IS NOT NULL
                        THEN quote.bid + 0.35 * (quote.ask - quote.bid) ELSE quote.mid END,
                   CASE WHEN decision.state = 'FIRE' THEN 'Exceptional'
                        WHEN decision.state = 'SETUP' THEN 'Strong'
                        WHEN decision.state = 'WATCH' THEN 'Watch' ELSE 'Reject' END
            FROM analysis.decision decision
            JOIN analysis.option_feature feature
              ON feature.run_id = decision.run_id AND feature.contract_id::text = decision.decision_key
            JOIN raw.option_quote quote
              ON quote.snapshot_id = feature.snapshot_id AND quote.contract_id = feature.contract_id
             AND quote.observed_at = feature.quote_observed_at
            WHERE decision.run_id = %s
            """,
            [run_id],
        )
        connection.execute(
            """
            INSERT INTO analysis.reject_summary (run_id, strategy_revision_id, instrument_id, gate_code, reject_count)
            SELECT decision.run_id, decision.strategy_revision_id, decision.instrument_id, blocker, count(*)
            FROM analysis.decision decision CROSS JOIN unnest(decision.blockers) blocker
            WHERE decision.run_id = %s AND decision.state = 'REJECT'
            GROUP BY decision.run_id, decision.strategy_revision_id, decision.instrument_id, blocker
            """,
            [run_id],
        )
        connection.execute(
            """
            DELETE FROM analysis.option_decision option_decision
            USING analysis.decision decision
            WHERE option_decision.decision_id = decision.id
              AND decision.run_id = %s AND decision.state = 'REJECT'
            """,
            [run_id],
        )
        connection.execute(
            "DELETE FROM analysis.decision WHERE run_id = %s AND state = 'REJECT'",
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
            "SELECT count(*) AS count FROM analysis.decision WHERE run_id = %s", [run_id]
        ).fetchone()["count"]
    return int(actionable)


def _publication_models(runtime: DatabaseRuntime, run_id: Any) -> dict[str, list[dict[str, Any]]]:
    with runtime.read(JOB_PROFILE) as connection:
        rows = connection.execute(
            """
            SELECT
                decision.id::text AS opportunity_id, decision.id::text AS candidate_event_id,
                decision.id::text AS event_id, instrument.symbol, instrument.symbol AS ticker,
                decision.state, decision.rank, decision.score, option_decision.tier,
                quote.observed_at AS snapshot_time, snapshot.source_id AS data_source,
                contract.id::text AS contract_id, contract.expiration, contract.strike,
                contract.option_type, quote.underlying_price, quote.bid, quote.ask, quote.mid,
                quote.mid AS premium_mid, quote.volume, quote.open_interest,
                quote.provider_iv AS iv, quote.provider_delta AS delta,
                feature.dte, feature.spread_pct, feature.liquidity_score,
                feature.convexity_score, feature.required_2x_price, feature.required_5x_price,
                feature.required_10x_price, feature.required_move_pct,
                option_decision.buy_under, decision.reasons AS top_reasons,
                decision.blockers, decision.quality_status,
                jsonb_build_object(
                    'expiration', contract.expiration, 'strike', contract.strike,
                    'option_type', contract.option_type, 'feature_version', feature.feature_version
                ) AS raw
            FROM analysis.decision decision
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            JOIN analysis.option_feature feature
              ON feature.run_id = decision.run_id AND feature.contract_id = option_decision.contract_id
            JOIN raw.option_quote quote
              ON quote.snapshot_id = option_decision.snapshot_id
             AND quote.contract_id = option_decision.contract_id
             AND quote.observed_at = option_decision.quote_observed_at
            JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
            JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
            WHERE decision.run_id = %s
            ORDER BY decision.rank
            """,
            [run_id],
        ).fetchall()
        rejected = connection.execute(
            """
            SELECT instrument.symbol, sum(summary.reject_count) AS reject_count
            FROM analysis.reject_summary summary
            JOIN catalog.instrument instrument ON instrument.id = summary.instrument_id
            WHERE summary.run_id = %s GROUP BY instrument.symbol
            """,
            [run_id],
        ).fetchall()
    all_rows = [dict(row) for row in rows]
    actionable = [row for row in all_rows if row["state"] != "REJECT"]
    summaries: dict[str, dict[str, Any]] = {}
    for row in all_rows:
        summary = summaries.setdefault(row["symbol"], {"symbol": row["symbol"], "ticker": row["ticker"], "fire_count": 0, "setup_count": 0, "watch_count": 0, "reject_count": 0})
        summary[f"{str(row['state']).lower()}_count"] += 1
    for row in rejected:
        summary = summaries.setdefault(
            row["symbol"],
            {"symbol": row["symbol"], "ticker": row["symbol"], "fire_count": 0, "setup_count": 0, "watch_count": 0, "reject_count": 0},
        )
        summary["reject_count"] = int(row["reject_count"] or 0)
    snapshots = [
        {
            key: row[key]
            for key in (
                "snapshot_time", "ticker", "underlying_price", "expiration", "strike",
                "option_type", "bid", "ask", "mid", "volume", "open_interest", "iv",
                "delta", "dte", "spread_pct", "data_source", "contract_id", "raw",
            )
        }
        for row in all_rows
    ]
    features = [
        {
            key: row[key]
            for key in (
                "snapshot_time", "contract_id", "ticker", "required_2x_price", "required_5x_price",
                "required_10x_price", "required_move_pct", "liquidity_score", "convexity_score", "raw",
            )
        }
        for row in all_rows
    ]
    return {
        "option_radar_opportunity": actionable,
        "candidate_event": all_rows,
        "option_radar_summary": list(summaries.values()),
        "option_snapshot": snapshots,
        "option_features": features,
    }
