"""PostgreSQL owner for the Phase 4 closed-loop artifacts."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.decision import StrategyForecast, TradePlan
from investment_panel.core.portfolio import (
    BookAttribution,
    ExecutionModelSnapshot,
    PaperExecutionObservation,
    PortfolioDriftDecision,
    PortfolioAllocationSnapshot,
    PortfolioScenarioArtifact,
    allocation_id_for_snapshot,
    attribution_id_for_record,
    execution_model_id_for_snapshot,
)
from investment_panel.database.runtime import DatabaseRuntime


PHASE4_PANEL_TABLES = (
    "portfolio_allocation",
    "portfolio_allocation_items",
    "portfolio_scenario_artifact",
    "execution_model_snapshot",
    "paper_execution_observations",
    "book_attribution",
)


class PortfolioLoopRepository:
    """Persist immutable allocation and paper telemetry records."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def read_shared_panel_models(self) -> dict[str, list[dict[str, Any]]]:
        """Read one immutable allocation bundle from one PostgreSQL snapshot."""

        empty = {name: [] for name in PHASE4_PANEL_TABLES}
        with self.runtime.snapshot() as connection:
            allocation = connection.execute(
                """SELECT allocation_id, as_of, input_cutoff, status, cash_hurdle,
                          forecast_ids, action_ids, strategy_registry_ids, input_hash,
                          available_at, metadata
                   FROM analysis.portfolio_allocation_snapshot
                   ORDER BY as_of DESC, available_at DESC, allocation_id DESC
                   LIMIT 1"""
            ).fetchone()
            if allocation is None:
                return empty
            allocation_id = allocation["allocation_id"]
            result = {name: [] for name in PHASE4_PANEL_TABLES}
            result["portfolio_allocation"] = [dict(allocation)]
            items = [dict(row) for row in connection.execute(
                """SELECT allocation_item_id, allocation_id, ticker,
                          strategy_forecast_id, action_id, hypothesis_id::text,
                          disposition, target_weight, current_weight,
                          marginal_book_utility, trace, blockers, funding_source, created_at
                   FROM analysis.portfolio_allocation_item
                   WHERE allocation_id = %s
                   ORDER BY disposition, target_weight DESC, ticker""", [allocation_id]
            ).fetchall()]
            drift_rows = [dict(row) for row in connection.execute(
                """SELECT decision_id, allocation_id, allocation_item_id, drift_score,
                          rollback_threshold, proposed_weight, action, input_cutoff,
                          input_hash, metadata
                   FROM analysis.portfolio_drift_evidence
                   WHERE allocation_id = %s
                   ORDER BY decision_id""", [allocation_id]
            ).fetchall()]
            drift_by_item: dict[str, list[dict[str, Any]]] = {}
            for row in drift_rows:
                drift_by_item.setdefault(str(row["allocation_item_id"]), []).append(row)
            for item in items:
                item["drift_evidence"] = drift_by_item.get(str(item["allocation_item_id"]), [])
            result["portfolio_allocation_items"] = items
            result["portfolio_scenario_artifact"] = [dict(row) for row in connection.execute(
                """SELECT scenario_artifact_id, allocation_id, model_version,
                          probability_semantics, scenarios, tail_dependence,
                          simultaneous_unwind, input_cutoff, input_hash, available_at
                   FROM analysis.probabilistic_portfolio_scenario_artifact
                   WHERE allocation_id = %s
                   ORDER BY available_at DESC, scenario_artifact_id DESC LIMIT 1""", [allocation_id]
            ).fetchall()]
            result["execution_model_snapshot"] = [dict(row) for row in connection.execute(
                """SELECT execution_model_snapshot_id, allocation_id, model_version,
                          calibration_status, sample_count, fill_probability, spread_bps,
                          latency_ms, impact_bps, input_cutoff, input_hash, available_at, metadata
                   FROM analysis.execution_model_snapshot
                   WHERE allocation_id = %s
                   ORDER BY available_at DESC, execution_model_snapshot_id DESC LIMIT 1""", [allocation_id]
            ).fetchall()]
            result["paper_execution_observations"] = [dict(row) for row in connection.execute(
                """SELECT paper_execution_observation_id, allocation_item_id, paper_order_id::text,
                          execution_mode, paper_only, status, requested_quantity, filled_quantity,
                          requested_price, fill_price, spread_bps, latency_ms, impact_bps,
                          side, exit_price, observed_at, available_at, metadata
                   FROM app.paper_execution_observation
                   WHERE paper_only AND execution_mode = 'paper'
                     AND (allocation_item_id IS NULL OR allocation_item_id IN (
                         SELECT allocation_item_id FROM analysis.portfolio_allocation_item
                         WHERE allocation_id = %s))
                   ORDER BY observed_at DESC, paper_execution_observation_id DESC LIMIT 500""", [allocation_id]
            ).fetchall()]
            result["book_attribution"] = [dict(row) for row in connection.execute(
                """SELECT book_attribution_id, allocation_id, allocation_item_id,
                          strategy_forecast_id, hypothesis_id::text, paper_execution_observation_id,
                          pnl_status, realized_pnl, attribution, input_cutoff, available_at
                   FROM analysis.book_attribution
                   WHERE allocation_id = %s
                   ORDER BY available_at DESC, book_attribution_id DESC LIMIT 500""", [allocation_id]
            ).fetchall()]
            return result

    def store_allocation(
        self,
        allocation: PortfolioAllocationSnapshot,
        *,
        scenario: PortfolioScenarioArtifact | None = None,
        execution_model: ExecutionModelSnapshot | None = None,
    ) -> str:
        if allocation_id_for_snapshot(allocation) != allocation.allocation_id:
            raise ValueError("allocation identity does not match PostgreSQL payload")
        if scenario is not None and scenario.allocation_id != allocation.allocation_id:
            raise ValueError("scenario allocation lineage does not match snapshot")
        if execution_model is not None and execution_model.allocation_id != allocation.allocation_id:
            raise ValueError("execution model allocation lineage does not match snapshot")
        with self.runtime.transaction() as connection:
            connection.execute(
                """INSERT INTO analysis.portfolio_allocation_snapshot
                   (allocation_id, as_of, input_cutoff, status, cash_hurdle,
                    forecast_ids, action_ids, strategy_registry_ids, input_hash)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (allocation_id) DO NOTHING""",
                [
                    allocation.allocation_id, allocation.as_of, allocation.input_cutoff,
                    allocation.status, allocation.cash_hurdle, Jsonb(list(allocation.forecast_ids)),
                    Jsonb(list(allocation.action_ids)), Jsonb(list(allocation.strategy_registry_ids)),
                    allocation.allocation_id.split(":", 1)[1],
                ],
            )
            for item in allocation.items:
                connection.execute(
                    """INSERT INTO analysis.portfolio_allocation_item
                       (allocation_item_id, allocation_id, ticker, strategy_forecast_id,
                       action_id, hypothesis_id, disposition, target_weight,
                        current_weight, marginal_book_utility, trace, blockers, funding_source)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (allocation_item_id) DO NOTHING""",
                    [
                        item.allocation_item_id, allocation.allocation_id, item.ticker,
                        item.strategy_forecast_id, item.action_id, item.hypothesis_id,
                        item.disposition, item.target_weight, item.current_weight, item.marginal_book_utility,
                        Jsonb(item.trace), Jsonb(list(item.blockers)), item.funding_source,
                    ],
                )
            if scenario is not None:
                self.store_scenario(connection, scenario)
            if execution_model is not None:
                self.store_execution_model(connection, execution_model)
        return allocation.allocation_id

    def refresh_authoritative_allocation(self, *, as_of: Any) -> PortfolioAllocationSnapshot:
        """Build and persist one allocation from PostgreSQL-owned forecast/action rows."""

        from investment_panel.core.portfolio import PortfolioCandidate, allocate_portfolio, build_execution_model_snapshot

        candidates: list[PortfolioCandidate] = []
        with self.runtime.snapshot() as connection:
            rows = connection.execute(
                """SELECT forecast.id AS strategy_forecast_id, forecast.strategy_revision_id,
                          forecast.strategy_evaluation_id::text, instrument.symbol AS ticker,
                          forecast.opportunity_episode_id, forecast.target, forecast.horizon,
                          forecast.forecast_value, forecast.forecast_range, forecast.forecast_distribution,
                          forecast.probability_semantics, forecast.model_artifact_id,
                          forecast.artifact_hash, forecast.input_hash, forecast.as_of,
                          forecast.input_cutoff, forecast.generated_at, forecast.available_at,
                          forecast.status, revision.strategy_key, revision.revision,
                          revision.hypothesis_id::text AS hypothesis_id,
                          decision.input_manifest->'trade_plan' AS trade_plan,
                          decision.portfolio_impacts, decision.data_requests
                   FROM analysis.strategy_forecast forecast
                   JOIN catalog.instrument instrument ON instrument.id = forecast.instrument_id
                   JOIN analysis.strategy_revision revision ON revision.id = forecast.strategy_revision_id
                   LEFT JOIN LATERAL (
                       SELECT decision.input_manifest, decision.portfolio_impacts,
                              decision.data_requests, decision.as_of
                       FROM analysis.ticker_decision decision
                       WHERE decision.instrument_id = forecast.instrument_id
                         AND decision.status = 'published'
                         AND decision.as_of <= %s
                         AND decision.published_at IS NOT NULL
                         AND decision.published_at <= %s
                         AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = forecast.id
                       ORDER BY decision.as_of DESC, decision.published_at DESC, decision.id DESC
                       LIMIT 1
                   ) decision ON true
                   WHERE forecast.status = 'available'
                     AND forecast.available_at <= forecast.input_cutoff
                     AND forecast.input_cutoff <= %s
                     AND forecast.available_at <= %s
                   ORDER BY forecast.input_cutoff DESC, forecast.id DESC
                   LIMIT 500""",
                [as_of, as_of, as_of, as_of],
            ).fetchall()
            for row in rows:
                raw = dict(row)
                try:
                    forecast = StrategyForecast.model_validate({key: raw[key] for key in (
                        "strategy_forecast_id", "strategy_revision_id", "strategy_evaluation_id", "ticker",
                        "opportunity_episode_id", "target", "horizon", "forecast_value", "forecast_range",
                        "forecast_distribution", "probability_semantics", "model_artifact_id", "artifact_hash",
                        "input_hash", "as_of", "input_cutoff", "generated_at", "available_at",
                    )})
                except Exception:
                    continue
                plan_value = raw.get("trade_plan")
                if not isinstance(plan_value, dict):
                    continue
                try:
                    plan = TradePlan.model_validate(plan_value)
                except Exception:
                    continue
                if plan.ticker != forecast.ticker or plan.strategy_forecast_id != forecast.strategy_forecast_id:
                    continue
                impact_values = raw.get("portfolio_impacts") or {}
                impact = impact_values.get(plan.selected_expression_kind.value) if isinstance(impact_values, dict) else None
                impact = impact if isinstance(impact, dict) else {}
                range_value = forecast.forecast_range
                uncertainty = None
                if range_value is not None:
                    uncertainty = abs(float(range_value.high) - float(range_value.low)) / 2
                candidates.append(PortfolioCandidate(
                    candidate_id=forecast.strategy_forecast_id,
                    ticker=forecast.ticker,
                    strategy_forecast_id=forecast.strategy_forecast_id,
                    action_id=plan.trade_plan_id,
                    hypothesis_id=raw.get("hypothesis_id"),
                    strategy_registry_id=f"{raw.get('strategy_key')}:{raw.get('revision')}",
                    expected_return=forecast.forecast_value,
                    uncertainty=uncertainty,
                    volatility=impact.get("volatility"),
                    risk_budget=impact.get("risk_budget"),
                    kelly_cap=impact.get("kelly_cap"),
                    drawdown_cap=impact.get("drawdown_cap"),
                    capacity=impact.get("capacity"),
                    overlap_penalty=impact.get("portfolio_overlap_penalty"),
                    execution_penalty=impact.get("expected_transaction_costs"),
                    covariance=impact.get("covariance"),
                    expression=plan.selected_expression.model_dump(mode="json"),
                    missing_data=tuple(str(value) for value in (raw.get("data_requests") or []) if str(value).strip()),
                    input_cutoff=forecast.input_cutoff,
                    available_at=forecast.available_at,
                    evidence_status="available" if plan.eligibility == "ACTIONABLE" else "blocked",
                    blockers=tuple(plan.blockers),
                ))
        allocation = allocate_portfolio(candidates, as_of=as_of, cash_hurdle=0)
        with self.runtime.snapshot() as connection:
            observations = [PaperExecutionObservation.model_validate(dict(row)) for row in connection.execute(
                """SELECT paper_execution_observation_id, allocation_item_id, paper_order_id::text,
                          execution_mode, paper_only, status, requested_quantity, filled_quantity,
                          requested_price, fill_price, spread_bps, latency_ms, impact_bps,
                          side, exit_price, observed_at, available_at
                   FROM app.paper_execution_observation
                   WHERE paper_only AND execution_mode = 'paper'
                   ORDER BY observed_at DESC LIMIT 500"""
            ).fetchall()]
        execution = build_execution_model_snapshot(allocation.allocation_id, allocation.input_cutoff, observations)
        self.store_allocation(allocation, execution_model=execution)
        return allocation

    @staticmethod
    def store_scenario(connection: Any, scenario: PortfolioScenarioArtifact) -> None:
        connection.execute(
            """INSERT INTO analysis.probabilistic_portfolio_scenario_artifact
               (scenario_artifact_id, allocation_id, model_version, probability_semantics,
                scenarios, tail_dependence, simultaneous_unwind, input_cutoff, input_hash)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (scenario_artifact_id) DO NOTHING""",
            [
                scenario.scenario_artifact_id, scenario.allocation_id, scenario.model_version,
                scenario.probability_semantics, Jsonb(list(scenario.scenarios)),
                Jsonb(scenario.tail_dependence), Jsonb(scenario.simultaneous_unwind),
                scenario.input_cutoff, scenario.scenario_artifact_id.split(":", 1)[1],
            ],
        )

    @staticmethod
    def store_execution_model(connection: Any, model: ExecutionModelSnapshot) -> None:
        if execution_model_id_for_snapshot(model) != model.execution_model_snapshot_id:
            raise ValueError("execution model identity does not match PostgreSQL payload")
        connection.execute(
            """INSERT INTO analysis.execution_model_snapshot
               (execution_model_snapshot_id, allocation_id, model_version, calibration_status,
                sample_count, fill_probability, spread_bps, latency_ms, impact_bps,
                input_cutoff, input_hash)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (execution_model_snapshot_id) DO NOTHING""",
            [
                model.execution_model_snapshot_id, model.allocation_id, model.model_version,
                model.calibration_status, model.sample_count, model.fill_probability,
                model.spread_bps, model.latency_ms, model.impact_bps,
                model.input_cutoff, model.execution_model_snapshot_id.split(":", 1)[1],
            ],
        )

    def record_paper_execution(self, observation: PaperExecutionObservation) -> str:
        with self.runtime.transaction() as connection:
            connection.execute(
                """INSERT INTO app.paper_execution_observation
                   (paper_execution_observation_id, allocation_item_id, paper_order_id,
                    execution_mode, paper_only, status, requested_quantity, filled_quantity,
                          requested_price, fill_price, spread_bps, latency_ms, impact_bps,
                          side, exit_price,
                          observed_at, available_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (paper_execution_observation_id) DO NOTHING""",
                [
                    observation.paper_execution_observation_id, observation.allocation_item_id,
                    observation.paper_order_id, observation.execution_mode, observation.paper_only,
                    observation.status, observation.requested_quantity, observation.filled_quantity,
                    observation.requested_price, observation.fill_price, observation.spread_bps,
                    observation.latency_ms, observation.impact_bps, observation.side, observation.exit_price,
                    observation.observed_at,
                    observation.available_at,
                ],
            )
        return observation.paper_execution_observation_id

    def record_attribution(self, attribution: BookAttribution) -> str:
        with self.runtime.transaction() as connection:
            if attribution_id_for_record(attribution) != attribution.book_attribution_id:
                raise ValueError("attribution identity does not match PostgreSQL payload")
            if attribution.pnl_status == "realized":
                observation = connection.execute(
                    """SELECT observation.filled_quantity, observation.fill_price,
                              observation.exit_price, observation.side,
                              observation.execution_mode, observation.paper_only,
                              observation.status, observation.paper_order_id
                       FROM app.paper_execution_observation observation
                       WHERE observation.paper_execution_observation_id = %s""",
                    [attribution.paper_execution_observation_id],
                ).fetchone()
                if observation is None or not observation["paper_only"] or observation["execution_mode"] != "paper":
                    raise ValueError("realized attribution requires a genuine paper observation")
                if observation["filled_quantity"] <= 0 or observation["fill_price"] is None or observation["exit_price"] is None:
                    raise ValueError("realized attribution requires entry and exit fills")
                direction = 1 if observation["side"] == "buy" else -1
                derived = direction * (float(observation["exit_price"]) - float(observation["fill_price"])) * float(observation["filled_quantity"])
                if abs(float(attribution.realized_pnl or 0) - derived) > 1e-9:
                    raise ValueError("realized attribution P&L does not match the paper fill lineage")
            connection.execute(
                """INSERT INTO analysis.book_attribution
                   (book_attribution_id, allocation_id, allocation_item_id,
                    strategy_forecast_id, hypothesis_id, paper_execution_observation_id,
                    pnl_status, realized_pnl, attribution, input_cutoff)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (book_attribution_id) DO NOTHING""",
                [
                    attribution.book_attribution_id, attribution.allocation_id,
                    attribution.allocation_item_id, attribution.strategy_forecast_id,
                    attribution.hypothesis_id, attribution.paper_execution_observation_id,
                    attribution.pnl_status, attribution.realized_pnl,
                    Jsonb(attribution.attribution), attribution.input_cutoff,
                ],
            )
        return attribution.book_attribution_id

    def store_drift_decisions(self, decisions: tuple[PortfolioDriftDecision, ...]) -> int:
        with self.runtime.transaction() as connection:
            for decision in decisions:
                connection.execute(
                    """INSERT INTO analysis.portfolio_drift_evidence
                       (decision_id, allocation_id, allocation_item_id, drift_score,
                        rollback_threshold, proposed_weight, action, input_cutoff, input_hash)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,
                               (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = %s),%s)
                       ON CONFLICT (decision_id) DO NOTHING""",
                    [decision.decision_id, decision.allocation_id, decision.allocation_item_id,
                     decision.drift_score, decision.rollback_threshold, decision.proposed_weight,
                     decision.action, decision.allocation_id, decision.decision_id.split(":", 1)[1]],
                )
        return len(decisions)
