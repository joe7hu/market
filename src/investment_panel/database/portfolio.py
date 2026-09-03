"""PostgreSQL owner for the Phase 4 closed-loop artifacts."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.decision import PortfolioImpact, StrategyForecast, TradePlan
from investment_panel.core.portfolio import (
    BookAttribution,
    AuthoritativePortfolioBundle,
    PortfolioBookEvidence,
    PortfolioConstraintEvidence,
    PortfolioImpactRiskEvidence,
    PortfolioExecutionEvidence,
    PortfolioScenarioEvidence,
    ExecutionModelSnapshot,
    PaperExecutionObservation,
    PortfolioDriftDecision,
    PortfolioAllocationSnapshot,
    PortfolioScenarioArtifact,
    allocation_id_for_snapshot,
    attribution_id_for_record,
    canonical_content_hash,
    execution_model_id_for_snapshot,
    integrated_portfolio_dto,
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
                          content_hash,
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
                """SELECT allocation_item_id, allocation_id, candidate_id, ticker,
                          strategy_forecast_id, action_id, hypothesis_id::text,
                          rank_id,
                          disposition, target_weight, current_weight,
                          marginal_book_utility, trace, blockers, funding_source, funding_amount,
                          content_hash, created_at
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
                          simultaneous_unwind, input_cutoff, input_hash, content_hash, available_at
                   FROM analysis.probabilistic_portfolio_scenario_artifact
                   WHERE allocation_id = %s
                   ORDER BY available_at DESC, scenario_artifact_id DESC LIMIT 1""", [allocation_id]
            ).fetchall()]
            result["execution_model_snapshot"] = [dict(row) for row in connection.execute(
                """SELECT execution_model_snapshot_id, allocation_id, model_version,
                          calibration_status, sample_count, fill_probability, spread_bps,
                          latency_ms, impact_bps, input_cutoff, input_hash, content_hash, available_at, metadata
                   FROM analysis.execution_model_snapshot
                   WHERE allocation_id = %s
                   ORDER BY available_at DESC, execution_model_snapshot_id DESC LIMIT 1""", [allocation_id]
            ).fetchall()]
            result["paper_execution_observations"] = [dict(row) for row in connection.execute(
                """SELECT paper_execution_observation_id, allocation_item_id, action_id, paper_order_id::text,
                          execution_mode, paper_only, status, requested_quantity, filled_quantity,
                          requested_price, fill_price, spread_bps, latency_ms, impact_bps,
                          side, exit_price, observed_at, available_at, metadata
                   FROM app.paper_execution_observation
                   WHERE paper_only AND execution_mode = 'paper'
                     AND allocation_item_id IN (
                         SELECT allocation_item_id FROM analysis.portfolio_allocation_item
                         WHERE allocation_id = %s)
                   ORDER BY observed_at DESC, paper_execution_observation_id DESC LIMIT 500""", [allocation_id]
            ).fetchall()]
            result["book_attribution"] = [dict(row) for row in connection.execute(
                """SELECT book_attribution_id, allocation_id, allocation_item_id,
                          strategy_forecast_id, hypothesis_id::text, action_id, rank_id, expression,
                          experiment_id, trial_id::text, result_id::text, paper_execution_observation_id,
                          pnl_status, realized_pnl, attribution, input_cutoff, content_hash, available_at
                   FROM analysis.book_attribution
                   WHERE allocation_id = %s
                   ORDER BY available_at DESC, book_attribution_id DESC LIMIT 500""", [allocation_id]
            ).fetchall()]
            # Re-hydrate the complete immutable model before publishing the
            # read model. This catches tampered or incomplete rows at the
            # PostgreSQL boundary instead of allowing a UI to invent values.
            allocation_model = PortfolioAllocationSnapshot.model_validate({
                "allocation_id": allocation_id,
                "as_of": allocation["as_of"],
                "input_cutoff": allocation["input_cutoff"],
                "status": allocation["status"],
                "cash_hurdle": allocation["cash_hurdle"],
                "items": tuple({key: value for key, value in item.items() if key not in {"allocation_id", "created_at", "drift_evidence", "content_hash"}}
                                | {"candidate_id": item.get("candidate_id") or item["ticker"]}
                                for item in items),
                "forecast_ids": tuple(allocation.get("forecast_ids") or ()),
                "action_ids": tuple(allocation.get("action_ids") or ()),
                "strategy_registry_ids": tuple(allocation.get("strategy_registry_ids") or ()),
                "metadata": allocation.get("metadata") or {},
            })
            if str(allocation.get("content_hash") or "").strip() != canonical_content_hash(allocation_model):
                raise ValueError("stored allocation content digest does not match canonical payload")
            for item, row in zip(allocation_model.items, items):
                expected_item_hash = canonical_content_hash(item.model_dump(mode="json") | {"allocation_id": allocation_id})
                if str(row.get("content_hash") or "").strip() != expected_item_hash:
                    raise ValueError("stored allocation item content digest does not match canonical payload")
            scenario_id = result["portfolio_scenario_artifact"][0]["scenario_artifact_id"] if result["portfolio_scenario_artifact"] else None
            execution_id = result["execution_model_snapshot"][0]["execution_model_snapshot_id"] if result["execution_model_snapshot"] else None
            canonical = integrated_portfolio_dto(
                allocation_model,
                scenario_artifact_id=scenario_id,
                execution_model_snapshot_id=execution_id,
                scenario=(
                    {key: result["portfolio_scenario_artifact"][0][key] for key in (
                        "scenario_artifact_id", "allocation_id", "scenarios", "tail_dependence", "simultaneous_unwind",
                    )}
                    if result["portfolio_scenario_artifact"] else None
                ),
                execution=(
                    {key: result["execution_model_snapshot"][0][key] for key in (
                        "execution_model_snapshot_id", "allocation_id", "calibration_status", "sample_count",
                    )}
                    if result["execution_model_snapshot"] else None
                ),
                attribution_count=len(result["book_attribution"]),
            ).model_dump(mode="json")
            result["portfolio_allocation"][0]["canonical_portfolio"] = canonical
            actions = {item["allocation_item_id"]: action for item, action in zip(items, canonical["actions"])}
            for item in result["portfolio_allocation_items"]:
                item["canonical_action"] = actions.get(item["allocation_item_id"])
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
                    forecast_ids, action_ids, strategy_registry_ids, input_hash, content_hash, metadata)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (allocation_id) DO NOTHING""",
                [
                    allocation.allocation_id, allocation.as_of, allocation.input_cutoff,
                    allocation.status, allocation.cash_hurdle, Jsonb(list(allocation.forecast_ids)),
                    Jsonb(list(allocation.action_ids)), Jsonb(list(allocation.strategy_registry_ids)),
                    allocation.allocation_id.split(":", 1)[1], canonical_content_hash(allocation), Jsonb(allocation.metadata),
                ],
            )
            stored = connection.execute(
                """SELECT input_hash, content_hash, as_of, input_cutoff, status, cash_hurdle,
                          forecast_ids, action_ids, strategy_registry_ids, metadata
                   FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = %s""",
                [allocation.allocation_id],
            ).fetchone()
            if stored is None or str(stored["input_hash"]).strip() != allocation.allocation_id.split(":", 1)[1] or not str(stored["content_hash"]).strip() or str(stored["content_hash"]).strip() == "0" * 64:
                raise ValueError("stored allocation content digest does not match canonical allocation")
            if any(stored[key] != value for key, value in {
                "as_of": allocation.as_of, "input_cutoff": allocation.input_cutoff,
                "status": allocation.status, "cash_hurdle": allocation.cash_hurdle,
            }.items()):
                raise ValueError("immutable allocation replay diverges from PostgreSQL content")
            for item in allocation.items:
                connection.execute(
                    """INSERT INTO analysis.portfolio_allocation_item
                       (allocation_item_id, allocation_id, candidate_id, ticker, strategy_forecast_id,
                       action_id, rank_id, hypothesis_id, disposition, target_weight,
                       current_weight, marginal_book_utility, trace, blockers, funding_source,
                       funding_amount, input_hash, content_hash)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (allocation_item_id) DO NOTHING""",
                    [
                        item.allocation_item_id, allocation.allocation_id, item.candidate_id, item.ticker,
                        item.strategy_forecast_id, item.action_id, item.rank_id, item.hypothesis_id,
                        item.disposition, item.target_weight, item.current_weight, item.marginal_book_utility,
                        Jsonb(item.trace), Jsonb(list(item.blockers)), item.funding_source, item.funding_amount,
                        item.allocation_item_id.split(":", 1)[1],
                        canonical_content_hash(item.model_dump(mode="json") | {"allocation_id": allocation.allocation_id}),
                    ],
                )
                stored_item = connection.execute(
                    """SELECT allocation_id, candidate_id, ticker, strategy_forecast_id,
                              action_id, rank_id, hypothesis_id, disposition, target_weight,
                              current_weight, marginal_book_utility, trace, blockers, funding_source,
                              funding_amount, input_hash, content_hash
                       FROM analysis.portfolio_allocation_item WHERE allocation_item_id = %s""",
                    [item.allocation_item_id],
                ).fetchone()
                expected_item_hash = canonical_content_hash(item.model_dump(mode="json") | {"allocation_id": allocation.allocation_id})
                if stored_item is None or str(stored_item["input_hash"]).strip() != item.allocation_item_id.split(":", 1)[1] or str(stored_item["content_hash"]).strip() != expected_item_hash or stored_item["allocation_id"] != allocation.allocation_id:
                    raise ValueError("immutable allocation item replay diverges from PostgreSQL content")
            if scenario is not None:
                self.store_scenario(connection, scenario)
            if execution_model is not None:
                self.store_execution_model(connection, execution_model)
        return allocation.allocation_id

    def read_authoritative_candidate_bundle(self, *, as_of: Any) -> AuthoritativePortfolioBundle:
        """Read one validated PostgreSQL-owned candidate bundle at one cutoff."""

        from investment_panel.core.portfolio import (
            PortfolioCandidate,
        )

        candidates: list[PortfolioCandidate] = []
        with self.runtime.snapshot() as connection:
            hurdle_row = connection.execute(
                "SELECT value FROM app.setting WHERE key = 'portfolio_cash_hurdle'"
            ).fetchone()
            hurdle_value = hurdle_row["value"] if hurdle_row else None
            if isinstance(hurdle_value, dict):
                hurdle_value = hurdle_value.get("value") or hurdle_value.get("cash_hurdle")
            cash_hurdle = float(hurdle_value) if hurdle_value is not None else None
            account = connection.execute(
                """SELECT id, source_id, account_key, net_liquidation, cash_balance, observed_at
                   FROM raw.broker_account_snapshot
                   WHERE observed_at <= %s
                   ORDER BY observed_at DESC, id DESC LIMIT 1""", [as_of]
            ).fetchone()
            positions = connection.execute(
                """SELECT position.instrument_id, position.id, position.market_value,
                          position.quantity, instrument.symbol
                   FROM raw.broker_position_snapshot position
                   JOIN raw.broker_account_snapshot account
                     ON account.id = position.account_snapshot_id
                   JOIN catalog.instrument instrument ON instrument.id = position.instrument_id
                   WHERE account.observed_at <= %s
                     AND account.id = (
                         SELECT id FROM raw.broker_account_snapshot
                         WHERE observed_at <= %s ORDER BY observed_at DESC, id DESC LIMIT 1
                     )""", [as_of, as_of]
            ).fetchall()
            position_by_ticker = {str(row["symbol"]).upper(): dict(row) for row in positions}
            rows = connection.execute(
                """SELECT forecast.id AS strategy_forecast_id, forecast.strategy_revision_id,
                          forecast.strategy_evaluation_id::text, instrument.symbol AS ticker,
                          forecast.opportunity_episode_id, forecast.target, forecast.horizon,
                          forecast.forecast_value, forecast.forecast_range, forecast.forecast_distribution,
                          forecast.probability_semantics, forecast.model_artifact_id,
                          forecast.artifact_hash, forecast.input_hash, forecast.as_of,
                          forecast.input_cutoff, forecast.generated_at, forecast.available_at,
                          forecast.research_trial_id, forecast.trial_result_id,
                          forecast.status, revision.strategy_key, revision.revision,
                          revision.hypothesis_id::text AS hypothesis_id,
                          decision.input_manifest->'trade_plan' AS trade_plan,
                          decision.id AS ticker_decision_id, decision.input_hash AS decision_input_hash,
                          decision.experiment_id AS decision_experiment_id,
                          decision.portfolio_impacts, decision.data_requests,
                          decision.selected_expression
                   FROM analysis.strategy_forecast forecast
                   JOIN catalog.instrument instrument ON instrument.id = forecast.instrument_id
                   JOIN analysis.strategy_revision revision ON revision.id = forecast.strategy_revision_id
                   LEFT JOIN LATERAL (
                       SELECT decision.input_manifest, decision.portfolio_impacts,
                              decision.id, decision.input_hash, decision.experiment_id,
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
                impact_raw = impact_values.get(plan.selected_expression_kind.value) if isinstance(impact_values, dict) else None
                try:
                    if not isinstance(impact_raw, dict):
                        raise ValueError("portfolio impact risk row is not an object")
                    persisted_impact = PortfolioImpact.model_validate(impact_raw)
                    impact_source_hash = canonical_content_hash(
                        persisted_impact.model_dump(mode="json"),
                    )
                    uncertainty = (
                        abs(float(forecast.forecast_range.high) - float(forecast.forecast_range.low)) / 2
                        if forecast.forecast_range is not None else None
                    )
                    risk_evidence = PortfolioImpactRiskEvidence.model_validate({
                        "impact_id": persisted_impact.impact_id, "ticker": forecast.ticker,
                        "source_decision_id": str(raw.get("ticker_decision_id") or ""),
                        "source_input_hash": impact_source_hash,
                        "input_cutoff": forecast.input_cutoff, "expected_return": forecast.forecast_value,
                        "uncertainty": uncertainty, "volatility": impact_raw.get("volatility"),
                        "risk_budget": impact_raw.get("risk_budget"), "kelly_cap": impact_raw.get("kelly_cap"),
                        "drawdown_cap": impact_raw.get("drawdown_cap"), "capacity": impact_raw.get("capacity"),
                        "overlap_penalty": impact_raw.get("portfolio_overlap_penalty", 0),
                        "execution_penalty": impact_raw.get("expected_transaction_costs", 0),
                        "covariance": impact_raw.get("covariance"),
                    })
                except (TypeError, ValueError):
                    continue
                if risk_evidence.impact_id != plan.portfolio_impact_id:
                    continue
                position = position_by_ticker.get(forecast.ticker.upper())
                nav = float(account["net_liquidation"]) if account and account["net_liquidation"] is not None else None
                market_value = float(position["market_value"]) if position and position.get("market_value") is not None else 0.0
                current_weight = min(max(abs(market_value / nav), 0.0), 1.0) if nav and nav > 0 else 0.0
                candidates.append(PortfolioCandidate(
                    candidate_id=forecast.strategy_forecast_id,
                    ticker=forecast.ticker,
                    strategy_forecast_id=forecast.strategy_forecast_id,
                    action_id=plan.trade_plan_id,
                    rank_id=plan.rank_id,
                    hypothesis_id=raw.get("hypothesis_id"),
                    portfolio_impact_id=risk_evidence.impact_id,
                    source_decision_id=str(raw.get("ticker_decision_id") or "") or None,
                    source_input_hash=str(raw.get("decision_input_hash") or "") or None,
                    experiment_id=str(raw.get("decision_experiment_id") or "") or None,
                    trial_id=str(raw.get("research_trial_id") or "") or None,
                    result_id=str(raw.get("trial_result_id") or "") or None,
                    risk_evidence=risk_evidence,
                    strategy_registry_id=f"strategy_revision:{raw.get('strategy_revision_id')}",
                    expected_return=forecast.forecast_value,
                    uncertainty=uncertainty,
                    volatility=risk_evidence.volatility, risk_budget=risk_evidence.risk_budget,
                    kelly_cap=risk_evidence.kelly_cap, drawdown_cap=risk_evidence.drawdown_cap,
                    capacity=risk_evidence.capacity, overlap_penalty=risk_evidence.overlap_penalty,
                    execution_penalty=risk_evidence.execution_penalty, covariance=risk_evidence.covariance,
                    expression=plan.selected_expression.model_dump(mode="json"),
                    invalidation=plan.invalidation.model_dump(mode="json") if plan.invalidation is not None else None,
                    missing_data=tuple(str(value) for value in (raw.get("data_requests") or []) if str(value).strip()),
                    input_cutoff=forecast.input_cutoff,
                    available_at=forecast.available_at,
                    evidence_status="available" if plan.eligibility == "ACTIONABLE" else "blocked",
                    blockers=tuple(plan.blockers),
                    current_weight=current_weight,
                    cash_available=float(account["cash_balance"]) if account and account["cash_balance"] is not None else None,
                    cash_source_id=(f"broker-account:{account['source_id']}:{account['id']}" if account else None),
                    trim_position_id=(f"broker-position:{position['id']}" if position else None),
                    trim_available=(abs(float(position["market_value"])) if position and position.get("market_value") is not None else None),
                ))
            forecast_keys = [candidate.strategy_forecast_id for candidate in candidates if candidate.strategy_forecast_id]
            tape_rows = [dict(row) for row in connection.execute(
                """SELECT tape.pnl_date, tape.strategy_forecast_id, instrument.symbol AS ticker,
                          tape.net_return, tape.tail_return, tape.input_cutoff, tape.available_at
                   FROM analysis.strategy_pnl_tape tape
                   JOIN catalog.instrument instrument ON instrument.id = tape.instrument_id
                   WHERE tape.strategy_forecast_id = ANY(%s)
                     AND tape.available_at <= %s AND tape.input_cutoff <= %s
                   ORDER BY tape.pnl_date, tape.id LIMIT 64""", [forecast_keys, as_of, as_of]
            ).fetchall()] if forecast_keys else []
            tape_by_forecast = {
                str(row["strategy_forecast_id"]) for row in tape_rows
                if row.get("net_return") is not None and row.get("tail_return") is not None
            }
            if cash_hurdle is None or cash_hurdle <= 0:
                candidates = [candidate.model_copy(update={"blockers": tuple((*candidate.blockers, "cash_hurdle_missing"))}) for candidate in candidates]
            if not tape_rows:
                candidates = [candidate.model_copy(update={"blockers": tuple((*candidate.blockers, "scenario_evidence_missing"))}) for candidate in candidates]
            else:
                candidates = [candidate.model_copy(update={"blockers": tuple((*candidate.blockers, "scenario_evidence_missing"))})
                              if candidate.strategy_forecast_id not in tape_by_forecast else candidate for candidate in candidates]
            drift_rows = [dict(row) for row in connection.execute(
                """SELECT revision.id AS strategy_revision_id,
                          monitoring.metrics->>'decay_score' AS decay_score
                   FROM analysis.strategy_monitoring_evidence monitoring
                   JOIN analysis.strategy_revision revision ON revision.id = monitoring.strategy_revision_id
                   WHERE monitoring.available_at <= %s AND monitoring.input_cutoff <= %s
                     AND monitoring.evidence_kind = 'decay'
                   ORDER BY monitoring.input_cutoff DESC, monitoring.id DESC""", [as_of, as_of]
            ).fetchall()]
            drift_by_revision = {str(row["strategy_revision_id"]): float(row["decay_score"])
                                 for row in drift_rows if row.get("decay_score") is not None}
            execution_row = connection.execute(
                """SELECT execution_model_snapshot_id, calibration_status, sample_count
                   FROM analysis.execution_model_snapshot
                   WHERE input_cutoff = %s
                   ORDER BY available_at DESC, execution_model_snapshot_id DESC LIMIT 1""", [as_of]
            ).fetchone()
            constraint_payload = {
                "cash_hurdle": cash_hurdle,
                "source": "app.setting:portfolio_cash_hurdle",
                "candidate_count": len(candidates),
            }
            authority_snapshot_id = f"broker-account:{account['id']}" if account else "missing"
            authority_payload = {
                "authority_snapshot_id": authority_snapshot_id,
                "input_cutoff": as_of,
                "cash_hurdle": cash_hurdle,
                "constraint": constraint_payload,
                "execution": dict(execution_row) if execution_row else None,
                "scenario_rows": tape_rows,
                "candidate_provenance": [
                    {
                        "impact_id": candidate.portfolio_impact_id,
                        "decision_id": candidate.source_decision_id,
                        "source_input_hash": candidate.source_input_hash,
                        "risk": candidate.risk_evidence.model_dump(mode="json") if candidate.risk_evidence else None,
                    }
                    for candidate in candidates
                ],
            }
            required_candidates = all(
                candidate.evidence_status == "available"
                and not candidate.blockers
                and candidate.strategy_forecast_id in tape_by_forecast
                for candidate in candidates
            )
            complete = bool(
                account is not None and account["cash_balance"] is not None
                and cash_hurdle is not None and cash_hurdle > 0
                and candidates and required_candidates and tape_rows and execution_row is not None
            )
        return AuthoritativePortfolioBundle._from_postgresql(
            cutoff=as_of,
            snapshot_id=authority_snapshot_id,
            source_payload=authority_payload,
            candidates=tuple(candidates),
            book=PortfolioBookEvidence(
                snapshot_id=(f"broker-account:{account['id']}" if account else None),
                cash_available=(float(account["cash_balance"]) if account and account["cash_balance"] is not None else None),
                cash_source_id=(f"broker-account:{account['source_id']}:{account['id']}" if account else None),
                positions={ticker: f"broker-position:{row['id']}" for ticker, row in position_by_ticker.items()},
                input_cutoff=as_of,
            ),
            constraints=PortfolioConstraintEvidence(
                cash_hurdle=cash_hurdle,
                constraint_hash=canonical_content_hash(constraint_payload),
                volatility_source="published portfolio impact",
                capacity_source="published portfolio impact",
                covariance_source="published portfolio impact",
            ),
            execution=PortfolioExecutionEvidence(
                snapshot_id=(str(execution_row["execution_model_snapshot_id"]) if execution_row else None),
                calibration_status=(str(execution_row["calibration_status"]) if execution_row else "calibration_pending"),
                sample_count=(int(execution_row["sample_count"]) if execution_row else 0),
                input_cutoff=as_of,
            ),
            scenario=PortfolioScenarioEvidence(
                artifact_id=None,
                observations=tuple(tape_rows),
                input_cutoff=as_of,
            ),
            drift_scores=drift_by_revision,
            complete=complete,
        )

    def refresh_authoritative_allocation(self, *, as_of: Any) -> PortfolioAllocationSnapshot:
        """Persist one allocation from the validated PostgreSQL candidate bundle."""

        from investment_panel.core.portfolio import (
            allocate_portfolio, apply_decay_to_allocation,
            build_execution_model_snapshot, build_scenario_artifact_from_observations,
        )

        bundle = self.read_authoritative_candidate_bundle(as_of=as_of)
        # A missing persisted hurdle is a hard fail-closed condition. The
        # empty allocator result records CASH without inventing a policy value.
        allocation = allocate_portfolio(bundle, as_of=as_of)
        drift_scores = {
            item.allocation_item_id: bundle.drift_scores.get(str(next(
                (candidate.strategy_registry_id or "").split(":")[-1]
                for candidate in bundle.candidates if candidate.candidate_id == item.candidate_id
            )))
            for item in allocation.items if item.disposition == "selected" and item.ticker != "CASH"
        }
        allocation, drift_decisions = apply_decay_to_allocation(
            allocation, drift_scores, rollback_threshold=1.0,
        )
        scenario = None
        if any(item.disposition == "selected" and item.ticker != "CASH" for item in allocation.items):
            try:
                scenario = build_scenario_artifact_from_observations(allocation, list(bundle.scenario_observations))
            except ValueError:
                # Missing cross-sectional shock coverage is a data failure,
                # not permission to persist a partial or synthetic scenario.
                safe_candidates = [candidate.model_copy(update={"blockers": tuple((*candidate.blockers, "scenario_cross_section_missing"))}) for candidate in bundle.candidates]
                safe_bundle = bundle.model_copy(update={"candidates": tuple(safe_candidates), "complete": False})
                allocation = allocate_portfolio(safe_bundle, as_of=as_of)
                allocation, drift_decisions = apply_decay_to_allocation(
                    allocation, {}, rollback_threshold=1.0,
                )
        with self.runtime.snapshot() as connection:
            item_ids = [item.allocation_item_id for item in allocation.items if item.ticker != "CASH"]
            observations = [PaperExecutionObservation.model_validate(dict(row)) for row in connection.execute(
                """SELECT paper_execution_observation_id, allocation_item_id, action_id, paper_order_id::text,
                          execution_mode, paper_only, status, requested_quantity, filled_quantity,
                          requested_price, fill_price, spread_bps, latency_ms, impact_bps,
                          side, exit_price, observed_at, available_at
                   FROM app.paper_execution_observation
                   WHERE paper_only AND execution_mode = 'paper'
                     AND allocation_item_id = ANY(%s)
                     AND filled_quantity >= 0
                   ORDER BY observed_at DESC LIMIT 500""", [item_ids]
            ).fetchall()] if item_ids else []
        execution = build_execution_model_snapshot(allocation.allocation_id, allocation.input_cutoff, observations)
        self.store_allocation(allocation, scenario=scenario, execution_model=execution)
        self.store_drift_decisions(drift_decisions)
        return allocation

    @staticmethod
    def store_scenario(connection: Any, scenario: PortfolioScenarioArtifact) -> None:
        connection.execute(
            """INSERT INTO analysis.probabilistic_portfolio_scenario_artifact
               (scenario_artifact_id, allocation_id, model_version, probability_semantics,
                scenarios, tail_dependence, simultaneous_unwind, input_cutoff, input_hash, content_hash)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (scenario_artifact_id) DO NOTHING""",
            [
                scenario.scenario_artifact_id, scenario.allocation_id, scenario.model_version,
                scenario.probability_semantics, Jsonb(list(scenario.scenarios)),
                Jsonb(scenario.tail_dependence), Jsonb(scenario.simultaneous_unwind),
                scenario.input_cutoff, scenario.scenario_artifact_id.split(":", 1)[1], canonical_content_hash(scenario),
            ],
        )
        stored = connection.execute(
            "SELECT content_hash FROM analysis.probabilistic_portfolio_scenario_artifact WHERE scenario_artifact_id = %s",
            [scenario.scenario_artifact_id],
        ).fetchone()
        if stored is None or str(stored["content_hash"]).strip() != canonical_content_hash(scenario):
            raise ValueError("stored scenario content digest does not match canonical payload")

    @staticmethod
    def store_execution_model(connection: Any, model: ExecutionModelSnapshot) -> None:
        if execution_model_id_for_snapshot(model) != model.execution_model_snapshot_id:
            raise ValueError("execution model identity does not match PostgreSQL payload")
        connection.execute(
            """INSERT INTO analysis.execution_model_snapshot
               (execution_model_snapshot_id, allocation_id, model_version, calibration_status,
                sample_count, fill_probability, spread_bps, latency_ms, impact_bps,
                input_cutoff, input_hash, content_hash)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (execution_model_snapshot_id) DO NOTHING""",
            [
                model.execution_model_snapshot_id, model.allocation_id, model.model_version,
                model.calibration_status, model.sample_count, model.fill_probability,
                model.spread_bps, model.latency_ms, model.impact_bps,
                model.input_cutoff, model.execution_model_snapshot_id.split(":", 1)[1], canonical_content_hash(model),
            ],
        )
        stored = connection.execute(
            "SELECT content_hash FROM analysis.execution_model_snapshot WHERE execution_model_snapshot_id = %s",
            [model.execution_model_snapshot_id],
        ).fetchone()
        if stored is None or str(stored["content_hash"]).strip() != canonical_content_hash(model):
            raise ValueError("stored execution content digest does not match canonical payload")

    def record_paper_execution(self, observation: PaperExecutionObservation) -> str:
        from investment_panel.core.portfolio import build_execution_model_snapshot

        with self.runtime.transaction() as connection:
            if not observation.allocation_item_id or not observation.action_id:
                raise ValueError("paper execution requires allocation and action lineage")
            order = connection.execute(
                """SELECT id, created_at, status, filled_quantity, actual_fill_price, filled_at, exit_at,
                          policy_result
                   FROM app.paper_order WHERE id = %s""", [observation.paper_order_id]
            ).fetchone()
            if order is None:
                raise ValueError("paper execution requires an existing paper order")
            item = connection.execute(
                    """SELECT allocation_id, action_id FROM analysis.portfolio_allocation_item
                       WHERE allocation_item_id = %s""", [observation.allocation_item_id]
            ).fetchone()
            if item is None or str(item["action_id"] or "") != observation.action_id:
                raise ValueError("paper execution action does not match allocation lineage")
            if str((order["policy_result"] or {}).get("trade_plan_id") or "") != observation.action_id:
                raise ValueError("paper execution order does not match allocation action lineage")
            if observation.filled_quantity > 0 and (
                order["status"] not in {"entered", "partial_exited", "exited", "closed", "invalidated"}
                or order["filled_quantity"] is None
                or float(order["filled_quantity"]) < observation.filled_quantity
                or order["actual_fill_price"] is None
                or order["filled_at"] is None
                or observation.observed_at < order["filled_at"]
            ):
                raise ValueError("paper execution requires a genuine existing fill")
            connection.execute(
                """INSERT INTO app.paper_execution_observation
                   (paper_execution_observation_id, allocation_item_id, action_id, paper_order_id,
                    execution_mode, paper_only, status, requested_quantity, filled_quantity,
                          requested_price, fill_price, spread_bps, latency_ms, impact_bps,
                          side, exit_price,
                          observed_at, available_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (paper_execution_observation_id) DO NOTHING""",
                [
                    observation.paper_execution_observation_id, observation.allocation_item_id,
                    observation.action_id, observation.paper_order_id, observation.execution_mode, observation.paper_only,
                    observation.status, observation.requested_quantity, observation.filled_quantity,
                    observation.requested_price, observation.fill_price, observation.spread_bps,
                    observation.latency_ms, observation.impact_bps, observation.side, observation.exit_price,
                    observation.observed_at,
                    observation.available_at,
                ],
            )
            allocation = connection.execute(
                    """SELECT allocation_id, input_cutoff FROM analysis.portfolio_allocation_item item
                       JOIN analysis.portfolio_allocation_snapshot snapshot USING (allocation_id)
                       WHERE item.allocation_item_id = %s""", [observation.allocation_item_id]
            ).fetchone()
            if allocation is None:
                raise ValueError("paper execution allocation lineage is unavailable")
            rows = connection.execute(
                    """SELECT paper_execution_observation_id, allocation_item_id, action_id, paper_order_id::text,
                              execution_mode, paper_only, status, requested_quantity, filled_quantity,
                              requested_price, fill_price, spread_bps, latency_ms, impact_bps,
                              side, exit_price, observed_at, available_at
                       FROM app.paper_execution_observation
                       WHERE allocation_item_id = %s ORDER BY observed_at, paper_execution_observation_id""",
                    [observation.allocation_item_id],
            ).fetchall()
            model = build_execution_model_snapshot(
                    allocation["allocation_id"], allocation["input_cutoff"],
                    [PaperExecutionObservation.model_validate(dict(row)) for row in rows],
            )
            self.store_execution_model(connection, model)
        return observation.paper_execution_observation_id

    def record_attribution(self, attribution: BookAttribution) -> str:
        with self.runtime.transaction() as connection:
            if attribution.pnl_status != "realized":
                raise ValueError("attribution requires a genuine realized paper fill")
            if attribution_id_for_record(attribution) != attribution.book_attribution_id:
                raise ValueError("attribution identity does not match PostgreSQL payload")
            if attribution.pnl_status == "realized":
                observation = connection.execute(
                    """SELECT observation.allocation_item_id, observation.filled_quantity, observation.fill_price,
                              observation.exit_price, observation.side,
                              observation.execution_mode, observation.paper_only,
                              observation.status, observation.paper_order_id,
                              paper.status AS order_status, paper.exit_at, paper.filled_at,
                              paper.policy_result
                       FROM app.paper_execution_observation observation
                       JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                       WHERE observation.paper_execution_observation_id = %s""",
                    [attribution.paper_execution_observation_id],
                ).fetchone()
                if observation is None or observation["allocation_item_id"] != attribution.allocation_item_id or not observation["paper_only"] or observation["execution_mode"] != "paper":
                    raise ValueError("realized attribution requires a genuine paper observation")
                item = connection.execute(
                    """SELECT item.allocation_id, item.action_id, item.rank_id, item.strategy_forecast_id, item.hypothesis_id,
                              item.trace->'expression' AS expression,
                              forecast.input_cutoff, forecast.research_trial_id, forecast.trial_result_id,
                              decision.id AS published_decision_id, decision.experiment_id
                       FROM analysis.portfolio_allocation_item item
                       JOIN analysis.strategy_forecast forecast ON forecast.id = item.strategy_forecast_id
                       JOIN analysis.ticker_decision decision
                         ON decision.input_manifest->'trade_plan'->>'trade_plan_id' = item.action_id
                        AND decision.input_manifest->'trade_plan'->>'rank_id' = item.rank_id
                        AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = item.strategy_forecast_id
                        AND decision.status = 'published' AND decision.published_at IS NOT NULL
                       WHERE item.allocation_item_id = %s
                       ORDER BY decision.published_at DESC, decision.id DESC LIMIT 1""",
                    [attribution.allocation_item_id],
                ).fetchone()
                if item is None or item["allocation_id"] != attribution.allocation_id or item["strategy_forecast_id"] != attribution.strategy_forecast_id or item["hypothesis_id"] != attribution.hypothesis_id or item["action_id"] != attribution.action_id or item["rank_id"] != attribution.rank_id or item["experiment_id"] != attribution.experiment_id or str(item["research_trial_id"]) != attribution.trial_id or str(item["trial_result_id"]) != attribution.result_id or str((observation["policy_result"] or {}).get("trade_plan_id") or "") != str(item["action_id"] or ""):
                    raise ValueError("realized attribution order does not match action lineage")
                if item["input_cutoff"] != attribution.input_cutoff or attribution.expression != (item["expression"] or attribution.expression):
                    raise ValueError("realized attribution expression or cutoff lineage is invalid")
                required = {"hypothesis_id", "experiment_id", "trial_id", "result_id", "forecast_id", "action_id", "rank_id", "expression", "fill_id", "pnl", "cost_decomposition"}
                if not required.issubset(attribution.attribution):
                    raise ValueError("realized attribution is missing canonical decomposition")
                if observation["filled_quantity"] <= 0 or observation["fill_price"] is None or observation["exit_price"] is None or observation["order_status"] not in {"exited", "closed"} or observation["exit_at"] is None or observation["filled_at"] is None:
                    raise ValueError("realized attribution requires entry and exit fills")
                direction = 1 if observation["side"] == "buy" else -1
                derived = direction * (float(observation["exit_price"]) - float(observation["fill_price"])) * float(observation["filled_quantity"])
                if abs(float(attribution.realized_pnl or 0) - derived) > 1e-9:
                    raise ValueError("realized attribution P&L does not match the paper fill lineage")
            connection.execute(
                """INSERT INTO analysis.book_attribution
                   (book_attribution_id, allocation_id, allocation_item_id,
                   strategy_forecast_id, hypothesis_id, action_id, rank_id, expression,
                   experiment_id, trial_id, result_id, paper_execution_observation_id,
                    pnl_status, realized_pnl, attribution, input_cutoff, input_hash, content_hash)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (book_attribution_id) DO NOTHING""",
                [
                    attribution.book_attribution_id, attribution.allocation_id,
                    attribution.allocation_item_id, attribution.strategy_forecast_id,
                    attribution.hypothesis_id, attribution.action_id, attribution.rank_id,
                    Jsonb(attribution.expression), attribution.experiment_id, attribution.trial_id,
                    attribution.result_id, attribution.paper_execution_observation_id,
                    attribution.pnl_status, attribution.realized_pnl,
                    Jsonb(attribution.attribution), attribution.input_cutoff,
                    attribution.book_attribution_id.split(":", 1)[1], canonical_content_hash(attribution),
                ],
            )
        return attribution.book_attribution_id

    def store_drift_decisions(self, decisions: tuple[PortfolioDriftDecision, ...]) -> int:
        with self.runtime.transaction() as connection:
            for decision in decisions:
                connection.execute(
                    """INSERT INTO analysis.portfolio_drift_evidence
                       (decision_id, allocation_id, allocation_item_id, drift_score,
                        rollback_threshold, proposed_weight, action, input_cutoff, input_hash, content_hash)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,
                               (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = %s),%s,%s)
                       ON CONFLICT (decision_id) DO NOTHING""",
                    [decision.decision_id, decision.allocation_id, decision.allocation_item_id,
                     decision.drift_score, decision.rollback_threshold, decision.proposed_weight,
                     decision.action, decision.allocation_id, decision.decision_id.split(":", 1)[1], canonical_content_hash(decision)],
                )
        return len(decisions)
