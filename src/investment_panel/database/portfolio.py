"""PostgreSQL owner for the Phase 4 closed-loop artifacts."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.portfolio import (
    BookAttribution,
    ExecutionModelSnapshot,
    PaperExecutionObservation,
    PortfolioAllocationSnapshot,
    PortfolioScenarioArtifact,
)
from investment_panel.database.runtime import DatabaseRuntime


class PortfolioLoopRepository:
    """Persist immutable allocation and paper telemetry records."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def store_allocation(
        self,
        allocation: PortfolioAllocationSnapshot,
        *,
        scenario: PortfolioScenarioArtifact | None = None,
        execution_model: ExecutionModelSnapshot | None = None,
    ) -> str:
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
                        marginal_book_utility, trace, blockers, funding_source)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (allocation_item_id) DO NOTHING""",
                    [
                        item.allocation_item_id, allocation.allocation_id, item.ticker,
                        item.strategy_forecast_id, item.action_id, item.hypothesis_id,
                        item.disposition, item.target_weight, item.marginal_book_utility,
                        Jsonb(item.trace), Jsonb(list(item.blockers)), item.funding_source,
                    ],
                )
            if scenario is not None:
                self.store_scenario(connection, scenario)
            if execution_model is not None:
                self.store_execution_model(connection, execution_model)
        return allocation.allocation_id

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
                    observed_at, available_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (paper_execution_observation_id) DO NOTHING""",
                [
                    observation.paper_execution_observation_id, observation.allocation_item_id,
                    observation.paper_order_id, observation.execution_mode, observation.paper_only,
                    observation.status, observation.requested_quantity, observation.filled_quantity,
                    observation.requested_price, observation.fill_price, observation.spread_bps,
                    observation.latency_ms, observation.impact_bps, observation.observed_at,
                    observation.available_at,
                ],
            )
        return observation.paper_execution_observation_id

    def record_attribution(self, attribution: BookAttribution) -> str:
        with self.runtime.transaction() as connection:
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
