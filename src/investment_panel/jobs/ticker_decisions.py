"""Publish immutable ticker-first decisions and refresh their outcomes."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from math import isfinite
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.core.config import AppConfig, load_config
from investment_panel.core.decision import (
    AlphaSignal,
    InstrumentStateSnapshot,
    OpportunityRank,
    ExpressionKind,
    MarketStateSnapshot,
    PortfolioImpact,
    TradeUtility,
    TICKER_OPPORTUNITY_RANKING_VERSION,
    apply_opportunity_rank_safety,
    build_alpha_signal,
    build_instrument_state_snapshot,
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
        "instrument_state_snapshot", "alpha_signal", "opportunity_rank",
    }
)
RANKING_SCOPE = "ticker-opportunity-ranking"


def publish(
    config_path: str | None = None,
    *,
    symbols: Iterable[str] | None = None,
    as_of: datetime | None = None,
    limit: int = 2_000,
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
    published: list[dict[str, Any]] = []
    decisions_for_paper: list[Any] = []
    records: list[dict[str, Any]] = []
    skipped = 0
    failures: list[dict[str, str]] = []
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
            # The benchmark is written before the read so its membership is
            # part of the same point-in-time input manifest as the decision.
            replay = replay_portfolio_at(config, reference)
            seed = build_ticker_decision(symbol, tables, as_of=reference, portfolio_replay=replay)
            market_publication = analysis_repository.publication_at_or_before(
                "market", cutoff=reference
            )
            snapshot = _market_snapshot_for_decision(market_publication, reference)
            impacts = (
                portfolio_impacts(seed, snapshot, market_publication["publication_id"], replay)
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
                portfolio_replay=replay,
            )
            records.append({"decision": decision, "tables": tables})
        except Exception as exc:  # one ticker cannot block the universe
            failures.append({"ticker": symbol, "error": f"{type(exc).__name__}: {exc}"})
    ranking_publication_id = None
    if records:
        rank_rows, models, ranking_inputs = _rank_records(records, failures, reference)
        ranking_run_id = analysis_repository.start_run(
            RANKING_SCOPE,
            input_cutoff=reference,
            code_version=TICKER_OPPORTUNITY_RANKING_VERSION,
            inputs=ranking_inputs,
            feature_versions={"ranking": TICKER_OPPORTUNITY_RANKING_VERSION},
        )
        ranking_publication_id = analysis_repository.publish(
            ranking_run_id,
            RANKING_SCOPE,
            models,
            validation={
                "scope": RANKING_SCOPE,
                "evaluated_universe_complete": not failures,
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
        _publish_at_cutoff(runtime, ranking_publication_id, reference)
        rank_by_key = {
            (rank.ticker, rank.decision_revision, rank.opportunity_episode_id): rank
            for rank in rank_rows
        }
        for record in records:
            original = record["decision"]
            key = (original.ticker, original.decision_revision, original.opportunity_episode_id)
            rank = rank_by_key[key]
            if rank.trade_rank is None or rank.trade_rank_unavailable_reason:
                safe = apply_opportunity_rank_safety(original, rank.model_dump(mode="json"))
                cash = safe.selected_expression
                rank = rank.model_copy(update={
                    "selected_expression_identity": trade_expression_identity(cash) if cash else None,
                    "selected_expression_kind": ExpressionKind.CASH.value,
                    "trade_rank": None,
                    "trade_utility": None,
                    "lower_confidence_expected_net_pnl": None,
                    "utility": TradeUtility(),
                })
                record["decision"] = safe
            rank_payload = rank.model_dump(mode="json")
            rank_payload["ranking_publication_id"] = str(ranking_publication_id)
            decision = record["decision"].model_copy(update={
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
    # Outcome maturity is independent of whether this run created a new
    # revision. A replay must still resolve older recommendations.
    outcome_result = repository.refresh_outcomes(now=reference)
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


def _market_snapshot_for_decision(publication: dict[str, Any] | None, cutoff: datetime) -> Any:
    if publication is None:
        return None
    rows = publication.get("models", {}).get("market_state_snapshot") or []
    if not rows:
        return None
    snapshot = MarketStateSnapshot.model_validate(rows[0])
    matrix = snapshot.coverage_matrix
    if matrix is not None:
        matrix = matrix.model_copy(update={
            "as_of": cutoff,
            "input_cutoff": cutoff,
            "rows": tuple(row.model_copy(update={"input_cutoff": cutoff}) for row in matrix.rows),
        })
    return snapshot.model_copy(update={
        "publication_id": publication["publication_id"],
        "as_of": cutoff,
        "input_cutoff": cutoff,
        "coverage_matrix": matrix,
    })


def _rank_records(
    records: list[dict[str, Any]],
    failures: list[dict[str, str]],
    reference: datetime,
) -> tuple[list[OpportunityRank], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        decision = record["decision"]
        snapshot, signals = _alpha_models(decision, record["tables"])
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
    ranks = rank_opportunities(candidates, evaluated_universe_complete=not failures)
    ranks_by_key = {
        (rank.ticker, rank.decision_revision, rank.opportunity_episode_id): rank
        for rank in ranks
    }
    for record in records:
        rank = ranks_by_key[(record["decision"].ticker, record["decision"].decision_revision, record["decision"].opportunity_episode_id)]
        if rank.trade_rank is None or rank.trade_rank_unavailable_reason:
            safe = apply_opportunity_rank_safety(record["decision"], rank.model_dump(mode="json"))
            cash = safe.selected_expression
            rank = rank.model_copy(update={
                "selected_expression_identity": trade_expression_identity(cash) if cash else None,
                "selected_expression_kind": ExpressionKind.CASH.value,
                "trade_rank": None,
                "trade_utility": None,
                "lower_confidence_expected_net_pnl": None,
                "utility": TradeUtility(),
            })
            record["decision"] = safe
        record["rank"] = rank
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


def _publish_at_cutoff(runtime: Any, publication_id: Any, cutoff: datetime) -> None:
    """Make the newly visible rank generation valid at its input cutoff."""

    with runtime.transaction(JOB_PROFILE) as connection:
        connection.execute(
            "UPDATE app.publication SET published_at = %s WHERE id = %s",
            [cutoff, publication_id],
        )


def _alpha_models(decision: Any, tables: dict[str, list[dict[str, Any]]]) -> tuple[InstrumentStateSnapshot, list[AlphaSignal]]:
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
        expected = view.expected_return_range
        probabilities = {
            scenario.name: scenario.probability
            for scenario in view.scenarios
        }
        distribution = probabilities if all(value is not None for value in probabilities.values()) else None
        calibration = _latest_calibration(tables, decision.cutoff)
        signals.append(build_alpha_signal(
            ticker=decision.ticker,
            opportunity_episode_id=decision.opportunity_episode_id,
            decision_revision=decision.decision_revision,
            instrument_state_snapshot_id=snapshot.snapshot_id,
            as_of=decision.cutoff,
            input_lineage=decision.input_lineage,
            target="expected_return" if expected is not None else None,
            horizon=view.horizon.value,
            direction=view.stance.value,
            forecast_value=((expected.low + expected.high) / 2) if expected is not None else None,
            forecast_range=expected,
            forecast_distribution=distribution,
            probability_semantics="scenario_probability" if distribution is not None else None,
            cohort_id=str(calibration.get("cohort_id") or "ticker-thesis-v1") if expected is not None else None,
            calibration_state=str(calibration.get("calibration_state") or "uncalibrated") if expected is not None else None,
            model_version=decision.input_manifest.experiment_id if expected is not None else None,
            feature_version=decision.input_manifest.code_version if expected is not None else None,
            evaluation_stage="research" if expected is not None else None,
            blockers=(() if expected is not None else ("forecast_missing",)),
        ))
    return snapshot, signals


def _latest_calibration(tables: dict[str, list[dict[str, Any]]], cutoff: datetime) -> dict[str, Any]:
    rows = [
        dict(row)
        for name in ("conviction_calibration", "ticker_calibration", "calibration")
        for row in tables.get(name) or []
    ]
    bounded = []
    for row in rows:
        available_at = row.get("available_at") or row.get("as_of")
        if available_at is None:
            continue
        try:
            parsed = _utc(datetime.fromisoformat(str(available_at).replace("Z", "+00:00")))
        except (TypeError, ValueError):
            continue
        if parsed <= cutoff:
            bounded.append((parsed, row))
    return max(bounded, key=lambda item: item[0], default=(None, {}))[1]


def _same_published_decision(left: Any, right: Any) -> bool:
    return (
        left.input_manifest.input_hash == right.input_manifest.input_hash
        and left.market_state_publication_id == right.market_state_publication_id
        and left.opportunity_rank == right.opportunity_rank
        and left.selected_expression.kind == right.selected_expression.kind
    )


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def portfolio_impacts(
    decision: Any,
    snapshot: Any,
    publication_id: str,
    replay: dict[str, Any],
) -> dict[ExpressionKind, PortfolioImpact]:
    before = {
        "cutoff": replay["cutoff"],
        "positions": replay["positions"],
        "portfolio_value": replay["portfolio_value"],
        "transaction_count": replay["transaction_count"],
    }
    impacts: dict[ExpressionKind, PortfolioImpact] = {}
    for kind, expression in decision.expressions.items():
        planned_loss = float(expression.planned_loss or 0)
        impacts[kind] = PortfolioImpact(
            impact_id=f"portfolio-impact:{decision.opportunity_episode_id}:{kind.value}",
            opportunity_episode_id=decision.opportunity_episode_id,
            expression_kind=kind,
            expression_identity=trade_expression_identity(expression),
            decision_revision=decision.decision_revision,
            risk_policy_version=decision.policy_version,
            market_snapshot_id=snapshot.snapshot_id,
            market_state_publication_id=publication_id,
            cutoff=decision.cutoff,
            input_lineage=tuple(decision.input_lineage),
            portfolio_before=before,
            portfolio_after={**before, "expression_kind": kind.value},
            marginal_risk=0.0 if kind is ExpressionKind.CASH else planned_loss,
            diversification_benefit=None,
            expected_transaction_costs=None,
            tail_risk_penalty=None,
            portfolio_overlap_penalty=None,
            capital_at_risk=planned_loss,
            risk_budget_consumed=0.0 if kind is ExpressionKind.CASH else planned_loss,
            positions_most_correlated=(),
            position_to_trim_or_replace=None,
            scenario_pnl={"status": "zero_impact", "pnl": 0.0} if kind is ExpressionKind.CASH else None,
            factor_exposure=None,
            greeks=None,
            liquidity={"status": "unavailable"},
            availability="available",
        )
    return impacts


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
    ranked: list[tuple[int, Any]] = []
    for decision in decisions:
        rank, rank_reason = _current_rank_for_decision(decision, rank_rows)
        if rank is None:
            skipped.append({"ticker": decision.ticker, "reason": rank_reason})
            continue
        ranked.append((int(rank["trade_rank"]), decision))
    for _, decision in sorted(ranked, key=lambda item: (item[0], item[1].ticker)):
        expression = decision.selected_expression
        action = decision.capital_action.action.value
        if action not in {
            "BUY", "ADD", "HEDGE", "WAIT_FOR_PRICE", "TRIM", "EXIT",
        }:
            skipped.append({"ticker": decision.ticker, "reason": "capital_action_not_orderable"})
            continue
        if expression is None or expression.kind.value == "CASH" or expression.status != "eligible":
            skipped.append({"ticker": decision.ticker, "reason": "no_eligible_non_cash_expression"})
            continue
        if expression.quantity is None or expression.quantity <= 0:
            skipped.append({"ticker": decision.ticker, "reason": "quantity_unavailable"})
            continue
        entry = expression.entry_range
        limit_price = ((entry.low + entry.high) / 2) if entry is not None else None
        if limit_price is None or limit_price <= 0:
            skipped.append({"ticker": decision.ticker, "reason": "entry_limit_unavailable"})
            continue
        key = f"ticker:{decision.ticker}:{decision.decision_revision}:{expression.kind.value}:{decision.capital_action.expires_at}"
        try:
            staged.append(repository.stage(
                ticker=decision.ticker,
                decision=decision,
                expression_kind=expression.kind.value,
                idempotency_key=key,
                quantity=expression.quantity,
                limit_price=limit_price,
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


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--ticker", action="append", dest="tickers")
    parser.add_argument("--limit", type=int, default=2_000)
    args = parser.parse_args()
    print(json.dumps(publish(args.config, symbols=args.tickers, limit=args.limit), indent=2, default=str))


__all__ = ["PUBLISH_INPUT_TABLES", "publish"]
