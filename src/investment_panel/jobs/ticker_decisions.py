"""Publish immutable ticker-first decisions and refresh their outcomes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import hashlib
import json
from math import isfinite
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.analysis.stock_alpha import FEATURE_VERSION, research_score
from investment_panel.core.config import AppConfig, load_config
from investment_panel.core.decision import (
    AlphaSignal,
    EligibleUniverseSnapshot,
    InputLineage,
    InstrumentStateSnapshot,
    OpportunityRank,
    ExpressionKind,
    MarketStateSnapshot,
    PortfolioImpact,
    TradeUtility,
    TICKER_OPPORTUNITY_RANKING_VERSION,
    TradePlan,
    availability_status_for_blockers,
    apply_opportunity_rank_safety,
    bind_trade_plan,
    build_alpha_signal,
    build_instrument_state_snapshot,
    build_trade_plan,
    build_ticker_decision,
    rank_opportunities,
    trade_expression_identity,
)
from investment_panel.core.panel import TICKER_INITIAL_TABLES
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.panel_models import load_postgres_tables
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.portfolio_ledger import replay_portfolio_at
from investment_panel.database.runtime import JOB_PROFILE
from investment_panel.database.ticker_decisions import TickerDecisionRepository
from investment_panel.database.ticker_execution import TickerPaperExecutionRepository


PUBLISH_INPUT_TABLES = tuple(
    name for name in TICKER_INITIAL_TABLES
    if name not in {
        "ticker_decisions", "ticker_outcomes",
        "instrument_state_snapshot", "alpha_signal", "opportunity_rank", "trade_plan",
    }
)
RANKING_SCOPE = "ticker-opportunity-ranking"
_MARKET_PUBLICATION_ID_UNSET = object()


def publish(
    config_path: str | None = None,
    *,
    symbols: Iterable[str] | None = None,
    as_of: datetime | None = None,
    limit: int = 2_000,
    market_state_publication_id: str | None | object = _MARKET_PUBLICATION_ID_UNSET,
) -> dict[str, Any]:
    """Build and persist one point-in-time decision per equity or ETF ticker.

    The publisher deliberately excludes the persisted decision table from its
    input tables. A publication job must create a new revision when any
    dependency changes; the API read path may use the latest immutable row.
    """

    config = load_config(config_path)
    runtime = runtime_for_config(config)
    reference = _utc(as_of or datetime.now(UTC))
    benchmark_symbols = _catalog_symbols(runtime, limit=10_000)
    selected = _normalise_symbols(benchmark_symbols, symbols, limit=limit)
    benchmark = _freeze_benchmark(runtime, benchmark_symbols, reference)
    repository = TickerDecisionRepository(runtime)
    analysis_repository = AnalysisRepository(runtime)
    alpha_artifacts = {
        horizon: analysis_repository.qualified_stock_alpha_artifact(
            cutoff=reference, horizon=horizon,
        )
        for horizon in ("TACTICAL", "FUNDAMENTAL")
    }
    published: list[dict[str, Any]] = []
    decisions_for_paper: list[Any] = []
    records: list[dict[str, Any]] = []
    skipped = 0
    failures: list[dict[str, str]] = []
    replay = replay_portfolio_at(config, reference)
    for symbol in selected:
        try:
            tables, metadata = load_postgres_tables(
                config,
                PUBLISH_INPUT_TABLES,
                query_row_limits={name: 64 for name in PUBLISH_INPUT_TABLES},
                query_symbol_filter={symbol},
                runtime_profile=JOB_PROFILE,
            )
            if not metadata.get("available_model_count"):
                raise RuntimeError("ticker input read models are unavailable")
            alpha_feature = analysis_repository.stock_alpha_feature(
                symbol, cutoff=reference, feature_version=FEATURE_VERSION,
            )
            tables["stock_alpha_features"] = [alpha_feature] if alpha_feature is not None else []
            prior_decision = repository.latest(symbol)
            prior_episode = prior_decision.opportunity_episode if prior_decision is not None else None
            # The benchmark is written before the read so its membership is
            # part of the same point-in-time input manifest as the decision.
            seed = build_ticker_decision(
                symbol,
                tables,
                as_of=reference,
                portfolio_replay=replay,
                prior_opportunity_episode=prior_episode,
            )
            replay_for_decision = replay_with_seed_stock_evidence(seed, replay)
            if market_state_publication_id is _MARKET_PUBLICATION_ID_UNSET:
                market_publication = analysis_repository.publication_at_or_before(
                    "market", cutoff=reference
                )
            elif market_state_publication_id is None:
                market_publication = None
            else:
                market_publication = analysis_repository.publication_by_id(
                    "market", str(market_state_publication_id)
                )
            snapshot = _market_snapshot_for_decision(
                market_publication,
                reference,
                max_age=timedelta(minutes=config.analysis.market_publication_max_age_minutes),
            )
            impacts = (
                portfolio_impacts(
                    seed, snapshot, market_publication["publication_id"], replay_for_decision
                )
                if snapshot is not None and market_publication is not None
                else {}
            )
            decision = build_ticker_decision(
                symbol,
                tables,
                as_of=reference,
                market_state_snapshot=snapshot,
                portfolio_impacts=impacts,
                risk_policy_snapshot=seed.risk_policy_snapshot,
                portfolio_replay=replay_for_decision,
                prior_opportunity_episode=prior_episode,
            )
            records.append({"decision": decision, "tables": tables})
        except Exception as exc:  # one ticker cannot block the universe
            failures.append({"ticker": symbol, "error": f"{type(exc).__name__}: {exc}"})
    ranking_publication_id = None
    if records:
        rank_rows, models, ranking_inputs = _rank_records(
            records,
            failures,
            reference,
            alpha_artifacts=alpha_artifacts,
            coverage_threshold=config.analysis.ticker_universe_coverage_threshold,
        )
        ranking_run_id = analysis_repository.start_run(
            RANKING_SCOPE,
            input_cutoff=reference,
            code_version=TICKER_OPPORTUNITY_RANKING_VERSION,
            inputs=ranking_inputs,
            feature_versions={"ranking": TICKER_OPPORTUNITY_RANKING_VERSION},
            strategy_revision_id=next(
                (
                    int(artifact["strategy_revision_id"])
                    for artifact in alpha_artifacts.values()
                    if artifact.get("availability_status") == "available"
                ),
                None,
            ),
        )
        ranking_publication_id = analysis_repository.publish(
            ranking_run_id,
            RANKING_SCOPE,
            models,
            validation={
                "scope": RANKING_SCOPE,
                "evaluated_universe_complete": bool(
                    rank_rows
                    and rank_rows[0].eligible_universe is not None
                    and rank_rows[0].eligible_universe.coverage_ratio
                    >= rank_rows[0].eligible_universe.threshold
                    and not rank_rows[0].eligible_universe.systemic_failure
                ),
                "paper_only": True,
                "live_order_submission": False,
            },
            complete_run_summary={
                "universe_count": len(selected),
                "evaluated_count": len(records),
                "failure_count": len(failures),
                "ranking_version": TICKER_OPPORTUNITY_RANKING_VERSION,
            },
        )
        rank_by_key = {
            (rank.ticker, rank.decision_revision, rank.opportunity_episode_id): rank
            for rank in rank_rows
        }
        for record in records:
            current = record["decision"]
            key = (current.ticker, current.decision_revision, current.opportunity_episode_id)
            rank = rank_by_key[key]
            rank_payload = rank.model_dump(mode="json")
            rank_payload["ranking_publication_id"] = str(ranking_publication_id)
            plan = record["plan"].model_copy(update={"publication_id": str(ranking_publication_id)})
            decision = bind_trade_plan(record["decision"], plan).model_copy(update={
                "instrument_state_snapshot": record["snapshot"].model_dump(mode="json"),
                "alpha_signals": [signal.model_dump(mode="json") for signal in record["signals"]],
                "opportunity_rank": rank_payload,
            })
            prior = repository.latest(decision.ticker)
            if prior is not None and _same_published_decision(prior, decision):
                skipped += 1
            else:
                published.append(repository.publish(decision))
            decisions_for_paper.append(decision)
    # Keep publication bounded to selected history. The scheduled outcome-
    # refresh job owns all-ticker historical maturity.
    outcome_result = repository.refresh_outcomes(now=reference, symbols=selected)
    paper_staging = _stage_eligible(runtime, config, decisions_for_paper)
    paper_execution = TickerPaperExecutionRepository(runtime, config).process(now=reference)
    status = "ok" if not failures else "partial" if published or skipped else "failed"
    return {
        "status": status,
        "database": "postgresql",
        "as_of": reference,
        "universe_count": len(selected),
        "benchmark": benchmark,
        "published_count": len(published),
        "skipped_count": skipped,
        "failed_count": len(failures),
        "failures": failures[:50],
        "outcomes": outcome_result,
        "paper_staging": paper_staging,
        "paper_execution": paper_execution,
        "ranking_publication_id": str(ranking_publication_id) if ranking_publication_id else None,
        "ranking_scope": RANKING_SCOPE,
    }


def _market_snapshot_for_decision(
    publication: dict[str, Any] | None,
    cutoff: datetime,
    *,
    max_age: timedelta = timedelta(days=1),
) -> Any:
    if publication is None or not isinstance(publication, Mapping):
        return None
    reference = _utc(cutoff)
    publication_id = str(publication.get("publication_id") or "").strip()
    if not publication_id:
        return None
    if str(publication.get("publication_scope") or "") != "market":
        return None
    if str(publication.get("publication_status") or "") not in {"published", "superseded"}:
        return None
    publication_cutoff = _timestamp(publication.get("input_cutoff"))
    publication_published_at = _timestamp(publication.get("published_at"))
    if (
        publication_cutoff is None
        or publication_cutoff > reference
        or reference - publication_cutoff > max_age
        or publication_published_at is None
        or publication_published_at <= publication_cutoff
        or publication_published_at > reference
    ):
        return None
    models = publication.get("models")
    if not isinstance(models, Mapping):
        return None
    rows = models.get("market_state_snapshot")
    if not isinstance(rows, (list, tuple)) or len(rows) != 1:
        return None
    try:
        snapshot = MarketStateSnapshot.model_validate(rows[0])
    except (TypeError, ValueError):
        return None
    if snapshot.contract_version not in {"market-state-snapshot.v1", "market-state-snapshot.v2"}:
        return None
    if snapshot.publication_id not in {None, "", publication_id}:
        return None
    if _utc(snapshot.input_cutoff) != publication_cutoff or _utc(snapshot.as_of) != publication_cutoff:
        return None
    try:
        source_lineage = tuple(
            InputLineage.model_validate(item)
            for item in publication.get("source_lineage") or ()
        )
    except (TypeError, ValueError):
        return None
    lineages = list(snapshot.input_lineage)
    for dimensions in snapshot.horizons.values():
        for dimension in dimensions:
            lineages.extend(dimension.lineage)
    matrix = snapshot.coverage_matrix
    if matrix is not None:
        if _utc(matrix.as_of) != publication_cutoff or _utc(matrix.input_cutoff) != publication_cutoff:
            return None
        if any(row.input_cutoff is not None and _utc(row.input_cutoff) != publication_cutoff for row in matrix.rows):
            return None
        for row in matrix.rows:
            lineages.extend(row.input_lineage)
    if not all(_lineage_matches_cutoff(item, publication_cutoff) for item in (*source_lineage, *lineages)):
        return None
    return snapshot.model_copy(update={
        "publication_id": publication_id,
        "coverage_matrix": matrix,
    })


def _rank_records(
    records: list[dict[str, Any]],
    failures: list[dict[str, str]],
    reference: datetime,
    *,
    alpha_artifacts: Mapping[str, Mapping[str, Any]],
    coverage_threshold: float,
) -> tuple[list[OpportunityRank], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        decision = record["decision"]
        snapshot, signals = _alpha_models(decision, record["tables"], alpha_artifacts)
        record["snapshot"] = snapshot
        record["signals"] = signals
        selected = decision.selected_expression
        kind = selected.kind if selected is not None else ExpressionKind.CASH
        impact = decision.portfolio_impacts.get(kind)
        signal = next(
            (item for item in signals if item.horizon == (selected.horizon.value if selected else "fundamental")),
            signals[-1] if signals else None,
        )
        impact_payload = impact.model_dump(mode="json") if impact is not None else {}
        policy_payload = decision.risk_policy_snapshot.model_dump(mode="json") if decision.risk_policy_snapshot else {}
        expression_payload = selected.model_dump(mode="json") if selected is not None else {}
        planned_loss = _finite_number(expression_payload.get("planned_loss"))
        lower_expectancy = _finite_number(expression_payload.get("lower_confidence_expectancy"))
        gross = lower_expectancy * planned_loss if lower_expectancy is not None and planned_loss is not None else None
        candidates.append({
            "ticker": decision.ticker,
            "opportunity_episode_id": decision.opportunity_episode_id,
            "decision_revision": decision.decision_revision,
            "policy_version": decision.policy_version,
            "selected_expression_identity": (
                trade_expression_identity(selected) if selected is not None else None
            ),
            "selected_expression_kind": kind.value,
            "portfolio_impact_id": impact.impact_id if impact is not None else None,
            "risk_policy_version": decision.policy_version,
            "alpha_signal_id": signal.signal_id if signal is not None else None,
            "alpha_signal": signal.model_dump(mode="json") if signal is not None else None,
            "instrument_state_snapshot_id": snapshot.snapshot_id,
            "market_snapshot_id": snapshot.market_snapshot_id,
            "market_state_publication_id": snapshot.market_state_publication_id,
            "cutoff": decision.cutoff,
            "input_lineage": tuple(decision.input_lineage),
            "expression": expression_payload,
            "portfolio_impact": impact_payload,
            "risk_policy_snapshot": policy_payload,
            "execution_feasible": bool(
                selected is not None
                and kind is not ExpressionKind.CASH
                and selected.status == "eligible"
                and (selected.quantity or 0) > 0
                and selected.entry_range is not None
            ),
            "lower_confidence_expected_gross_pnl": gross,
            "expected_transaction_costs": impact_payload.get("expected_transaction_costs"),
            "tail_risk_penalty": impact_payload.get("tail_risk_penalty"),
            "portfolio_overlap_penalty": impact_payload.get("portfolio_overlap_penalty"),
            "diversification_benefit": impact_payload.get("diversification_benefit"),
            "capital_at_risk": planned_loss,
        })
    intended = tuple(sorted({str(record["decision"].ticker) for record in records} | {item["ticker"] for item in failures}))
    available = tuple(sorted(str(record["decision"].ticker) for record in records))
    eligible_universe = EligibleUniverseSnapshot(
        intended=intended,
        available=available,
        excluded_reasons={item["ticker"]: item["error"] for item in failures},
        excluded_materiality={item["ticker"]: False for item in failures},
        source_failures={item["ticker"]: (item["error"],) for item in failures},
        coverage_ratio=len(available) / len(intended) if intended else 0.0,
        threshold=coverage_threshold,
        systemic_failure=not available,
        systemic_failure_reasons=("ticker_universe_unavailable",) if not available else (),
    )
    ranks = rank_opportunities(candidates, eligible_universe=eligible_universe)
    ranks_by_key = {
        (rank.ticker, rank.decision_revision, rank.opportunity_episode_id): rank
        for rank in ranks
    }
    for record in records:
        rank = ranks_by_key[(record["decision"].ticker, record["decision"].decision_revision, record["decision"].opportunity_episode_id)]
        if rank.trade_rank is None or rank.trade_rank_unavailable_reason or record["decision"].context_blockers:
            safe = apply_opportunity_rank_safety(record["decision"], rank.model_dump(mode="json"))
            rank = _rank_after_safety(rank, safe)
            record["decision"] = safe
        record["rank"] = rank
        plan = build_trade_plan(
            decision=record["decision"],
            rank=rank,
            alpha_signal=next(
                (signal for signal in record["signals"] if signal.signal_id == rank.alpha_signal_id),
                None,
            ),
        )
        if plan.eligibility != "ACTIONABLE" and (
            record["decision"].selected_expression is not None
            and record["decision"].selected_expression.kind is not ExpressionKind.CASH
        ):
            safe = apply_opportunity_rank_safety(
                record["decision"],
                {"trade_rank_unavailable_reason": plan.primary_blocker or "trade_plan_unavailable"},
            )
            record["decision"] = safe
            rank = _rank_after_safety(rank, safe)
            record["rank"] = rank
            plan = build_trade_plan(
                decision=safe,
                rank=rank,
                alpha_signal=next(
                    (signal for signal in record["signals"] if signal.signal_id == rank.alpha_signal_id),
                    None,
                ),
            )
        record["decision"] = bind_trade_plan(record["decision"], plan)
        record["plan"] = record["decision"].trade_plan
    models = {
        "instrument_state_snapshot": [
            {"stable_key": f"{record['decision'].ticker}:instrument:{record['snapshot'].snapshot_id}", **record["snapshot"].model_dump(mode="json")}
            for record in records
        ],
        "alpha_signal": [
            {"stable_key": f"{record['decision'].ticker}:signal:{signal.signal_id}", **signal.model_dump(mode="json")}
            for record in records for signal in record["signals"]
        ],
        "opportunity_rank": [
            {"stable_key": f"{record['decision'].ticker}:rank:{record['rank'].rank_id}", **record["rank"].model_dump(mode="json")}
            for record in records
        ],
        "trade_plan": [
            {"stable_key": f"{record['decision'].ticker}:plan:{record['plan'].trade_plan_id}", **record["plan"].model_dump(mode="json")}
            for record in records
        ],
    }
    ranking_inputs = {
        "universe": sorted(record["decision"].ticker for record in records) + sorted(item["ticker"] for item in failures),
        "successes": [
            {
                "ticker": record["decision"].ticker,
                "decision_revision": record["decision"].decision_revision,
                "opportunity_episode_id": record["decision"].opportunity_episode_id,
                "input_hash": record["decision"].input_manifest.input_hash,
            }
            for record in records
        ],
        "failures": failures,
        "market_state_publication_ids": sorted({
            str(record["decision"].market_state_publication_id)
            for record in records if record["decision"].market_state_publication_id
        }),
        "market_snapshot_ids": sorted({
            record["snapshot"].snapshot_id for record in records
        }),
        "alpha_signal_ids": sorted(
            signal.signal_id for record in records for signal in record["signals"]
        ),
        "selected_expression_identities": sorted(
            str(record["rank"].selected_expression_identity or "") for record in records
        ),
        "portfolio_impact_ids": sorted(
            str(record["rank"].portfolio_impact_id or "") for record in records
        ),
        "policy_versions": sorted({record["decision"].policy_version for record in records}),
        "ranking_version": TICKER_OPPORTUNITY_RANKING_VERSION,
        "reference": reference,
    }
    return [record["rank"] for record in records], models, ranking_inputs


def _rank_after_safety(rank: OpportunityRank, decision: Any) -> OpportunityRank:
    cash = decision.selected_expression
    impact = decision.portfolio_impacts.get(ExpressionKind.CASH)
    reason = (
        decision.resolution.primary_blocker
        if decision.resolution is not None and decision.resolution.primary_blocker
        else rank.trade_rank_unavailable_reason or "opportunity_rank_unavailable"
    )
    blockers = tuple(dict.fromkeys((
        *rank.blockers,
        *(decision.resolution.blockers if decision.resolution is not None else ()),
        reason,
    )))
    return rank.model_copy(update={
        "selected_expression_identity": trade_expression_identity(cash) if cash else None,
        "selected_expression_kind": ExpressionKind.CASH.value,
        "portfolio_impact_id": impact.impact_id if impact is not None else None,
        "trade_rank": None,
        "trade_rank_unavailable_reason": reason,
        "availability_status": availability_status_for_blockers((reason,)),
        "primary_blocker": reason,
        "blockers": blockers,
        "trade_utility": None,
        "lower_confidence_expected_net_pnl": None,
        "utility": TradeUtility(),
    })


def _alpha_models(
    decision: Any,
    tables: dict[str, list[dict[str, Any]]],
    artifacts: Mapping[str, Mapping[str, Any]],
) -> tuple[InstrumentStateSnapshot, list[AlphaSignal]]:
    snapshot = build_instrument_state_snapshot(
        decision.ticker,
        tables,
        as_of=decision.cutoff,
        market_snapshot=decision.market_state_snapshot,
        market_state_publication_id=decision.market_state_publication_id,
        input_lineage=decision.input_lineage,
    )
    signals: list[AlphaSignal] = []
    for view in (decision.tactical, decision.fundamental):
        artifact = dict(artifacts.get(view.horizon.value) or {})
        expected = view.expected_return_range
        probabilities = {
            scenario.name: scenario.probability
            for scenario in view.scenarios
        }
        distribution = probabilities if all(value is not None for value in probabilities.values()) else None
        availability_status = str(artifact.get("availability_status") or "missing")
        blockers = list(artifact.get("blockers") or ())
        feature_rows = tables.get("stock_alpha_features") or []
        feature = dict(feature_rows[0]) if feature_rows else {}
        score = research_score(feature)
        if expected is None:
            availability_status = "missing"
            blockers.append("forecast_missing")
        if score is None:
            availability_status = "missing"
            blockers.append("alpha_research_features_missing_or_mismatched")
        signals.append(build_alpha_signal(
            ticker=decision.ticker,
            opportunity_episode_id=decision.opportunity_episode_id,
            decision_revision=decision.decision_revision,
            instrument_state_snapshot_id=snapshot.snapshot_id,
            as_of=decision.cutoff,
            input_lineage=decision.input_lineage,
            target=str(artifact.get("target") or "") or None,
            horizon=view.horizon.value,
            direction=view.stance.value,
            forecast_value=((expected.low + expected.high) / 2) if expected is not None else None,
            forecast_range=expected,
            forecast_distribution=distribution,
            probability_semantics="scenario_probability" if distribution is not None else None,
            cohort_id=artifact.get("cohort_id"),
            calibration_state=artifact.get("calibration_state"),
            model_version=artifact.get("model_version"),
            feature_version=artifact.get("feature_version"),
            evaluation_stage=artifact.get("evaluation_stage"),
            availability_status=availability_status,
            strategy_key=artifact.get("strategy_key"),
            strategy_revision_id=artifact.get("strategy_revision_id"),
            model_artifact_id=artifact.get("model_artifact_id"),
            strategy_evaluation_id=artifact.get("strategy_evaluation_id"),
            artifact_published_at=artifact.get("artifact_published_at"),
            evaluation_evaluated_at=artifact.get("evaluation_evaluated_at"),
            evaluation_available_at=artifact.get("evaluation_available_at"),
            oos_period_start=artifact.get("oos_period_start"),
            oos_period_end=artifact.get("oos_period_end"),
            cohort_path=artifact.get("cohort_path") or (),
            fallback_parent=artifact.get("fallback_parent"),
            effective_sample_size=artifact.get("effective_sample_size"),
            calibration_metrics=artifact.get("calibration_metrics"),
            research_score=score,
            cost_model_version=artifact.get("cost_model_version"),
            promotion_stage=artifact.get("promotion_stage"),
            lower_confidence_net_utility_after_costs=artifact.get(
                "lower_confidence_net_utility_after_costs"
            ),
            blockers=tuple(dict.fromkeys(blockers)),
        ))
    return snapshot, signals


def _same_published_decision(left: Any, right: Any) -> bool:
    try:
        same_rank = (
            OpportunityRank.model_validate(left.opportunity_rank)
            == OpportunityRank.model_validate(right.opportunity_rank)
        ) if left.opportunity_rank is not None and right.opportunity_rank is not None else (
            left.opportunity_rank is None and right.opportunity_rank is None
        )
    except (TypeError, ValueError):
        same_rank = False
    return (
        left.input_manifest.input_hash == right.input_manifest.input_hash
        and left.market_state_publication_id == right.market_state_publication_id
        and same_rank
        and (left.trade_plan.trade_plan_id if left.trade_plan else None)
        == (right.trade_plan.trade_plan_id if right.trade_plan else None)
        and left.selected_expression.kind == right.selected_expression.kind
    )


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def replay_with_seed_stock_evidence(seed: Any, replay: dict[str, Any]) -> dict[str, Any]:
    """Reuse the cutoff-bounded evidence copy created by the seed decision."""

    impact = seed.portfolio_impacts.get(ExpressionKind.STOCK)
    enriched = impact.portfolio_before if impact is not None else None
    if isinstance(enriched, Mapping) and "stock_evidence" in enriched:
        replay = dict(enriched)
        replay.pop("stock_impact", None)
        return replay
    return replay


def portfolio_impacts(
    decision: Any,
    snapshot: Any,
    publication_id: str,
    replay: dict[str, Any],
) -> dict[ExpressionKind, PortfolioImpact]:
    canonical_snapshot = snapshot.model_copy(update={"publication_id": publication_id})
    return {
        kind: PortfolioImpact.compose(
            episode=decision.opportunity_episode,
            expression=expression,
            snapshot=canonical_snapshot,
            policy_version=decision.policy_version,
            portfolio_replay=replay,
        )
        for kind, expression in decision.expressions.items()
    }


def publish_benchmark(
    config_path: str | None = None,
    *,
    as_of: datetime | None = None,
    limit: int = 10_000,
) -> dict[str, Any]:
    """Freeze the equity denominator without publishing decisions or orders."""

    config = load_config(config_path)
    runtime = runtime_for_config(config)
    reference = _utc(as_of or datetime.now(UTC))
    symbols = _catalog_symbols(runtime, limit=limit)
    benchmark = _freeze_benchmark(runtime, symbols, reference)
    return {
        "status": "ok",
        "database": "postgresql",
        "as_of": reference,
        "universe_count": len(symbols),
        "benchmark": benchmark,
        "published_count": 0,
        "paper_orders": 0,
        "side_effects": ["analysis.ticker_benchmark_snapshot"],
    }


def _catalog_symbols(runtime: Any, *, limit: int) -> list[str]:
    with runtime.read() as connection:
        rows = connection.execute(
            """
            SELECT symbol
            FROM catalog.instrument
            WHERE asset_class IN ('equity', 'etf')
            ORDER BY symbol
            LIMIT %s
            """,
            [max(1, min(int(limit), 10_000))],
        ).fetchall()
    return sorted({str(row["symbol"]).strip().upper() for row in rows if row["symbol"]})


def _normalise_symbols(
    available: Iterable[str],
    symbols: Iterable[str] | None,
    *,
    limit: int,
) -> list[str]:
    requested = {
        str(symbol).strip().upper()
        for symbol in symbols or ()
        if str(symbol).strip()
    }
    available_set = {str(symbol).strip().upper() for symbol in available if str(symbol).strip()}
    if requested:
        return sorted(requested & available_set)
    return sorted(available_set)[:max(1, min(int(limit), 10_000))]


def _freeze_benchmark(runtime: Any, symbols: Iterable[str], as_of: datetime) -> dict[str, Any]:
    """Freeze the equity denominator independently from option availability.

    The catalog is an explicit current membership source, not a claim that the
    catalog is a complete official index. The coverage field makes that limit
    visible until an official point-in-time constituent feed is available.
    """

    members = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    encoded_members = json.dumps(members, separators=(",", ":"), ensure_ascii=True)
    membership_hash = hashlib.sha256(encoded_members.encode("utf-8")).hexdigest()
    row = {
        "benchmark_key": "market-equity-etf",
        "as_of": as_of,
        "available_at": as_of,
        "membership_hash": membership_hash,
        "member_count": len(members),
        "source_id": "catalog.instrument",
        "source_version": "20260823_0050",
        "exact_membership": members,
        "coverage": {
            "catalog_membership_complete": True,
            "official_index_membership": False,
            "catalog_limit": 10_000,
            "price_coverage": "measured_by_confirmed_price_rows",
            "options_availability_affects_breadth": False,
        },
    }
    with runtime.transaction(JOB_PROFILE) as connection:
        connection.execute(
            """
            INSERT INTO analysis.ticker_benchmark_snapshot (
                benchmark_key, as_of, available_at, membership_hash, member_count,
                source_id, source_version, exact_membership, coverage
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (benchmark_key, as_of) DO UPDATE SET
                available_at = EXCLUDED.available_at,
                membership_hash = EXCLUDED.membership_hash,
                member_count = EXCLUDED.member_count,
                source_id = EXCLUDED.source_id,
                source_version = EXCLUDED.source_version,
                exact_membership = EXCLUDED.exact_membership,
                coverage = EXCLUDED.coverage
            """,
            [
                row["benchmark_key"], row["as_of"], row["available_at"],
                row["membership_hash"], row["member_count"], row["source_id"],
                row["source_version"], Jsonb(row["exact_membership"]),
                Jsonb(row["coverage"]),
            ],
        )
    return row


def _stage_eligible(runtime: Any, config: AppConfig, decisions: Iterable[Any]) -> dict[str, Any]:
    settings = config.analysis.options_decision_system
    if settings.mode != "paper" or not settings.ticker_paper_actions_enabled:
        return {"status": "disabled", "staged": [], "skipped": []}
    repository = TickerPaperExecutionRepository(runtime, config)
    staged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    rank_rows = AnalysisRepository(runtime).publication_rows(
        RANKING_SCOPE, "opportunity_rank", include_lineage=True,
    )
    plan_rows = AnalysisRepository(runtime).publication_rows(
        RANKING_SCOPE, "trade_plan", include_lineage=True,
    )
    ranked: list[tuple[int, Any]] = []
    for decision in decisions:
        rank, rank_reason = _current_rank_for_decision(decision, rank_rows)
        if rank is None:
            skipped.append({"ticker": decision.ticker, "reason": rank_reason})
            continue
        plan, plan_reason = _current_trade_plan_for_decision(decision, rank, plan_rows)
        if plan is None:
            skipped.append({"ticker": decision.ticker, "reason": plan_reason})
            continue
        if plan.eligibility != "ACTIONABLE":
            skipped.append({"ticker": decision.ticker, "reason": plan.primary_blocker or "trade_plan_blocked"})
            continue
        ranked.append((int(rank["trade_rank"]), decision))
    for _, decision in sorted(ranked, key=lambda item: (item[0], item[1].ticker)):
        current_rank, _ = _current_rank_for_decision(decision, rank_rows)
        plan, plan_reason = _current_trade_plan_for_decision(decision, current_rank, plan_rows)
        if plan is None:
            skipped.append({"ticker": decision.ticker, "reason": plan_reason})
            continue
        action = plan.action
        if action not in {
            "BUY", "ADD", "HEDGE", "WAIT_FOR_PRICE", "TRIM", "EXIT",
        }:
            skipped.append({"ticker": decision.ticker, "reason": "capital_action_not_orderable"})
            continue
        if plan.selected_expression_kind is ExpressionKind.CASH:
            skipped.append({"ticker": decision.ticker, "reason": "no_eligible_non_cash_expression"})
            continue
        if plan.quantity is None or plan.quantity <= 0:
            skipped.append({"ticker": decision.ticker, "reason": "quantity_unavailable"})
            continue
        if plan.entry_limit is None or plan.entry_limit <= 0:
            skipped.append({"ticker": decision.ticker, "reason": "entry_limit_unavailable"})
            continue
        try:
            staged.append(repository.stage(
                ticker=decision.ticker,
                decision=decision,
                expression_kind=plan.selected_expression_kind.value,
                idempotency_key=f"ticker:{decision.ticker}:{plan.trade_plan_id}",
                trade_plan_id=plan.trade_plan_id,
            ))
        except ValueError as exc:
            skipped.append({"ticker": decision.ticker, "reason": str(exc)})
    return {"status": "ok", "staged": staged, "skipped": skipped}


def _current_rank_for_decision(
    decision: Any,
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    matches = [
        row for row in rows
        if str(row.get("ticker") or row.get("symbol") or "").upper() == decision.ticker
        and str(row.get("decision_revision") or "") == decision.decision_revision
        and str(row.get("opportunity_episode_id") or "") == decision.opportunity_episode_id
    ]
    if len(matches) != 1:
        return None, "opportunity_rank_missing"
    rank = matches[0]
    selected = decision.selected_expression
    try:
        if selected is None or str(rank.get("selected_expression_kind") or "") != selected.kind.value:
            return None, "opportunity_rank_identity_mismatch"
        if str(rank.get("selected_expression_identity") or "") != trade_expression_identity(selected):
            return None, "opportunity_rank_identity_mismatch"
        if rank.get("trade_rank_unavailable_reason"):
            return None, str(rank["trade_rank_unavailable_reason"])
        if not bool(rank.get("evaluated_universe_complete")):
            return None, "ranking_universe_incomplete"
        rank_utility = float(rank.get("trade_utility"))
        if int(rank.get("trade_rank")) <= 0 or not isfinite(rank_utility) or rank_utility <= 0:
            return None, "opportunity_rank_unavailable"
    except (TypeError, ValueError, OverflowError):
        return None, "opportunity_rank_unavailable"
    return rank, ""


def _current_trade_plan_for_decision(
    decision: Any,
    rank: dict[str, Any] | None,
    rows: list[dict[str, Any]],
) -> tuple[TradePlan | None, str]:
    if rank is None:
        return None, "opportunity_rank_missing"
    matches = [
        row for row in rows
        if str(row.get("ticker") or row.get("symbol") or "").upper() == decision.ticker
        and str(row.get("decision_revision") or "") == decision.decision_revision
        and str(row.get("opportunity_episode_id") or "") == decision.opportunity_episode_id
        and str(row.get("publication_id") or "") == str(rank.get("publication_id") or "")
    ]
    if len(matches) != 1:
        return None, "trade_plan_missing"
    try:
        plan = TradePlan.model_validate(matches[0])
    except (TypeError, ValueError, KeyError):
        return None, "trade_plan_invalid"
    expected = decision.trade_plan
    if (
        expected is None
        or plan.trade_plan_id != expected.trade_plan_id
        or plan.publication_id != expected.publication_id
    ):
        return None, "trade_plan_identity_mismatch"
    if plan.rank_id != str(rank.get("rank_id") or ""):
        return None, "trade_plan_identity_mismatch"
    if plan.selected_expression_identity != str(rank.get("selected_expression_identity") or ""):
        return None, "trade_plan_identity_mismatch"
    if plan.portfolio_impact_id != str(rank.get("portfolio_impact_id") or ""):
        return None, "trade_plan_identity_mismatch"
    return plan, ""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str) and value:
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _lineage_matches_cutoff(lineage: InputLineage, cutoff: datetime) -> bool:
    available_at = _timestamp(lineage.available_at)
    lineage_cutoff = _timestamp(lineage.cutoff)
    return (
        available_at is not None
        and available_at <= cutoff
        and lineage_cutoff == cutoff
    )


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--ticker", "--tickers", action="append", dest="tickers")
    parser.add_argument("--limit", type=int, default=2_000)
    args = parser.parse_args(argv)
    print(json.dumps(publish(args.config, symbols=args.tickers, limit=args.limit), indent=2, default=str))


__all__ = ["PUBLISH_INPUT_TABLES", "publish"]
