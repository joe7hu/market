"""Bounded operational evidence for the trader's first screen.

Reads the existing publication, job, order and research authorities. This is not
an admission policy, a second ledger, or a claim of profitability. A failed read
never becomes an empty/healthy population.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from investment_panel.core.job_policy import scheduler_intervals, scheduler_enabled
from investment_panel.domain.decision import is_market_open, is_us_market_day, market_session_bounds, MARKET_TZ
from investment_panel.infrastructure.postgres.runtime import API_PROFILE, DatabaseRuntime
from investment_panel.settings import AppConfig
from investment_panel.infrastructure.postgres.strategy_parameters import PARAMETER_FAILURE_VERDICTS

logger = logging.getLogger(__name__)

WORKFLOW_JOBS = (
    "update_market_data", "update_market_valuations", "refresh_market_publication",
    "refresh_decision_models", "process_options_paper_orders", "refresh_paper_quotes",
    "run_option_paper_experiments", "run_stock_alpha_walk_forward",
    "refresh_symbol_decision_outcomes", "run_continuous_advisor_replay",
)
BASELINE_MODELS = (
    "market_environment_assets", "market_environment_model",
    "market_valuation_reference_charts", "market_state_snapshot", "coverage_matrix",
)


def next_session_open(now: datetime) -> datetime:
    local = now.astimezone(MARKET_TZ)
    for offset in range(10):
        day = local.date() + timedelta(days=offset)
        if is_us_market_day(day):
            opened, _ = market_session_bounds(day)
            if opened > now:
                return opened.astimezone(UTC)
    raise ValueError("No market session within calendar horizon")


def worker_projection(row: dict[str, Any] | None, *, job: str, interval: int | None,
                      now: datetime, enabled: bool) -> dict[str, Any]:
    base = {"job": job, "interval_seconds": interval, "last_attempt_at": None,
            "last_success_at": None, "heartbeat_at": None, "next_expected_at": None,
            "status": "not_started", "reason": "No recorded worker run."}
    if not row:
        if not enabled or not interval:
            base.update(status="disabled", reason="Not scheduled by the current configuration.")
        return base
    base.update(source_status=row.get("source_status"), downstream_status=row.get("downstream_status"),
                last_attempt_at=row.get("started_at"), heartbeat_at=row.get("heartbeat_at"),
                last_success_at=row.get("last_success_at"), run_id=str(row.get("id") or ""))
    finished = row.get("finished_at")
    if interval and finished:
        base["next_expected_at"] = finished + timedelta(seconds=interval)
    status = str(row.get("status") or "unknown")
    last_activity = row.get("heartbeat_at") if status == "running" else finished or row.get("started_at")
    tolerance = max(90, 3 * (interval or 0))
    overdue = bool(enabled and interval and last_activity and (now - last_activity).total_seconds() > tolerance)
    base.update(status="overdue" if overdue else status,
                reason="Worker heartbeat or next run is overdue." if overdue else
                       "Last run failed; inspect its error in System health." if status == "failed" else
                       "Last run was partial; inspect its unresolved stages." if status == "partial" else
                       "Recorded worker state; this does not establish strategy quality.")
    if not enabled or not interval:
        base.update(status="disabled", reason="Not scheduled; last recorded run is shown for context.")
    return base


class WorkstationRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def status(self, config: AppConfig) -> dict[str, Any]:
        now = datetime.now(UTC)
        failures: list[str] = []
        intervals = scheduler_intervals(config)
        settings = config.analysis.options_decision_system
        with self.runtime.snapshot(API_PROFILE) as connection:
            def read(name: str, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
                try:
                    with connection.transaction():
                        return [dict(row) for row in connection.execute(sql, params or []).fetchall()]
                except Exception:
                    logger.exception("Workstation status read failed: %s", name)
                    failures.append(name)
                    return []

            jobs = read("jobs", """
                SELECT latest.*, success.last_success_at FROM unnest(%s::text[]) name(job)
                JOIN LATERAL (SELECT id, job_name, status, started_at, heartbeat_at, finished_at,
                    source_status, downstream_status FROM ops.job_run WHERE job_name = name.job
                    AND started_at <= %s ORDER BY started_at DESC, id DESC LIMIT 1) latest ON true
                LEFT JOIN LATERAL (SELECT max(finished_at) AS last_success_at FROM ops.job_run
                    WHERE job_name = name.job AND status = 'succeeded' AND finished_at <= %s) success ON true
            """, [list(WORKFLOW_JOBS), now, now])
            market = read("market_publication", """
                SELECT publication.id::text AS publication_id, publication.published_at,
                       run.input_cutoff, run.status AS run_status, run.inputs->'optional_errors' AS optional_errors
                FROM app.publication publication JOIN analysis.run run ON run.id = publication.analysis_run_id
                WHERE publication.scope = 'market' AND publication.status = 'published' AND run.status = 'succeeded'
                  AND publication.published_at <= %s ORDER BY publication.published_at DESC, publication.id DESC LIMIT 1
            """, [now])
            market_counts = read("market_models", """
                SELECT model_name, count(*)::int AS count FROM app.publication_content_item
                WHERE publication_id = %s::uuid AND model_name = ANY(%s) GROUP BY model_name
            """, [market[0]["publication_id"], list(BASELINE_MODELS)]) if market else []
            orders = read("paper_orders", """
                SELECT lane, status, count(*)::int AS count, min(created_at) AS oldest_at,
                       max(updated_at) AS last_transition_at,
                       max(execution_quote #>> '{management,last_checked_at}') AS last_checked_at
                FROM app.paper_order WHERE paper_only AND created_at <= %s GROUP BY lane, status
            """, [now])
            observations = read("observations", """
                SELECT status, pending_entry_reason AS reason, count(*)::int AS count,
                       max(created_at) AS latest_at, max(coalesce(exit_at, entry_at)) AS last_transition_at
                FROM analysis.shadow_trade WHERE source_kind = 'options_paper_experiment' AND created_at <= %s
                GROUP BY status, pending_entry_reason ORDER BY count(*) DESC, status, pending_entry_reason
            """, [now])
            waiting = read("waiting_orders", """
                SELECT paper.id::text AS paper_order_id, instrument.symbol, paper.status,
                       paper.updated_at AS last_transition_at,
                       paper.execution_quote->'management' AS management,
                       paper.unfilled_reason AS reason, paper.expires_at,
                       paper.ticket_snapshot #>> '{entry,valid_until}' AS entry_deadline
                FROM app.paper_order paper JOIN catalog.instrument instrument ON instrument.id = paper.instrument_id
                WHERE paper.paper_only AND paper.created_at <= %s
                  AND paper.status NOT IN ('exited', 'invalidated', 'unfilled', 'rejected', 'unmeasurable')
                ORDER BY paper.execution_quote #>> '{management,last_checked_at}' NULLS FIRST, paper.created_at
                LIMIT 20
            """, [now])
            preflight_rejections = read("parameter_preflight", """
                SELECT id::text AS proposal_id, result->>'strategy_version' AS strategy,
                       result->>'status' AS status, result->'preflight' AS preflight, created_at
                FROM analysis.agent_task WHERE task_kind = 'strategy_mutation_proposal'
                  AND status = 'completed' AND result->>'candidate_revision_id' IS NULL
                  AND result->>'status' = ANY(%s) AND created_at <= %s
                ORDER BY created_at DESC, id DESC LIMIT 10
            """, [sorted(PARAMETER_FAILURE_VERDICTS), now])
            evaluations = read("evaluations", """
                SELECT revision.id AS strategy_revision_id, revision.name, revision.status AS revision_status,
                       evaluation.evaluation_type, evaluation.verdict, evaluation.metrics, evaluation.evidence,
                       evaluation.evaluated_at, evaluation.available_at
                FROM analysis.strategy_revision revision
                JOIN LATERAL (
                    SELECT DISTINCT ON (evaluation_type) evaluation_type, verdict, metrics, evidence,
                           evaluated_at, available_at FROM analysis.strategy_evaluation
                    WHERE strategy_revision_id = revision.id AND evaluated_at <= %s AND available_at <= %s
                    ORDER BY evaluation_type, evaluated_at DESC, id DESC
                ) evaluation ON true
                WHERE revision.status IN ('active', 'candidate', 'testing', 'approved') AND revision.created_at <= %s
                ORDER BY (revision.status = 'active') DESC, evaluation.evaluated_at DESC LIMIT 40
            """, [now, now, now])
        by_job = {row["job_name"]: row for row in jobs}
        workers = [worker_projection(by_job.get(job), job=job, interval=intervals.get(job), now=now,
                                     enabled=scheduler_enabled()) for job in WORKFLOW_JOBS]
        if "jobs" in failures:
            for worker in workers:
                worker.update(status="unavailable", reason="Worker state could not be read.")
        counts = {row["model_name"]: row["count"] for row in market_counts}
        market_projection = {
            **(market[0] if market else {}), "models": counts,
            "status": "unavailable" if any(key in failures for key in ("market_publication", "market_models")) else
                      "not_published" if not market else "partial" if any(not counts.get(key) for key in BASELINE_MODELS) else "available",
            "repair_job": "refresh_market_publication",
        }
        if market_projection["status"] == "available" and market:
            age = now - market[0]["published_at"]
            if age.total_seconds() > config.analysis.market_publication_max_age_minutes * 60:
                market_projection["status"] = "stale"
        order_counts: dict[str, int] = {}
        for row in orders:
            order_counts[row["status"]] = order_counts.get(row["status"], 0) + row["count"]
        observation_counts: dict[str, int] = {}
        for row in observations:
            observation_counts[row["status"]] = observation_counts.get(row["status"], 0) + row["count"]
        blockers = []
        if market_projection["status"] != "available":
            blockers.append({"capability": "Market", "reason": "Market publication is incomplete or unavailable.",
                             "action": "Rebuild from stored facts; inspect ingestion only if source facts are missing.",
                             "href": "/market", "job": "refresh_market_publication"})
        manager = next(row for row in workers if row["job"] == "process_options_paper_orders")
        if manager["status"] in {"failed", "partial", "overdue", "not_started", "disabled", "unavailable"}:
            blockers.append({"capability": "Paper execution", "reason": manager["reason"],
                             "action": "Inspect the paper manager and scheduler configuration.",
                             "href": "/health", "job": "process_options_paper_orders"})
        for evaluation in evaluations:
            if evaluation["verdict"] in PARAMETER_FAILURE_VERDICTS:
                blockers.append({"capability": "Strategy learning", "reason": f"{evaluation['name']}: {evaluation['verdict']}",
                                 "action": "Fix the named parameter/implementation mismatch; retrying unchanged inputs is not learning.",
                                 "href": f"/research/strategies/{evaluation['strategy_revision_id']}", "job": None})
        for proposal in preflight_rejections:
            details = proposal.get("preflight") or {}
            detail = "; ".join(details.get("errors") or details.get("blocked_parameters") or [])
            blockers.append({"capability": "Strategy learning", "reason": f"{proposal['strategy']}: {proposal['status']} — {detail}",
                             "action": "Correct the recorded proposal parameters; no candidate or outcome samples were fabricated.",
                             "href": "/research?section=strategies", "job": None})
        return {
            "as_of": now, "paper_only": True, "status": "partial" if failures else "available",
            "failed_reads": failures, "market_session": "regular" if is_market_open(now) else "closed",
            "next_session_at": None if is_market_open(now) else next_session_open(now),
            "workers": workers, "market": market_projection, "blockers": blockers,
            "paper": {"status": "unavailable" if "paper_orders" in failures else "available",
                      "waiting_status": "unavailable" if "waiting_orders" in failures else "available",
                      "counts": order_counts, "lanes": orders, "waiting": waiting,
                      "entries_enabled": settings.options_paper_actions_enabled,
                      "collection_enabled": settings.strategy_experiment_collection_enabled,
                      "promotion_enabled": settings.strategy_auto_promotion_enabled},
            "observations": {"status": "unavailable" if "observations" in failures else "available",
                             "counts": observation_counts, "reason_counts": observations,
                             "accounting_basis": "Prospective observations; not funded-account P&L."},
            "evaluations": evaluations, "preflight_rejections": preflight_rejections,
        }
