"""Persisted local refresh-job launcher for the API."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import timedelta
import json
import subprocess
import sys
from threading import Event, Thread
import time
import traceback
from typing import Any, Callable, Iterator
from investment_panel.core.config import load_config
from investment_panel.core.job_execution import (
    PROJECT_ROOT,
    RefreshProcessSpec,
    SOURCE_ROOT,
    execute_sync,
)
from investment_panel.core.job_policy import default_job_timeouts, job_timeout_seconds
from investment_panel.database.authority import database_url, runtime_for_url
from investment_panel.database.jobs import JobRepository
from investment_panel.database.options_history_policy import OptionHistoryPolicyRepository
from investment_panel.jobs import (
    postgres_refresh,
    refresh_options_radar,
    run_option_agents,
    run_agent_experiment,
    run_option_recovery_agents,
    run_thesis_monitor,
    snapshot_database,
    update_ibkr_options,
    update_broker_sources,
    update_arco_sources,
    update_content_sources,
    update_disclosure_sources,
    update_market_data,
    update_market_valuations,
    update_market_events,
    update_phase2_sources,
    update_robinhood_options,
    robinhood_option_history,
    detect_option_events,
    options_paper_execution,
    decision_inbox,
    refresh_symbol_decision_outcomes,
    ticker_decisions,
    ticker_data_requests,
    update_company_financials,
    stock_alpha_walk_forward,
)
from investment_panel.database.retention import RetentionRepository


JobRunner = Callable[[str | None], dict[str, Any]]

JOB_TIMEOUT_SECONDS: dict[str, int] = default_job_timeouts()
JOB_HEARTBEAT_SECONDS = 30.0
RADAR_PROVIDER_LEASE_RETRY_DELAYS_SECONDS = (15.0, 30.0, 60.0)

__all__ = [
    "PROJECT_ROOT",
    "SOURCE_ROOT",
    "finish_refresh_job_failed",
    "mark_stale_running_jobs",
    "run_refresh_job",
    "start_refresh_job",
]


def _job_timeout_seconds(job_name: str) -> int | None:
    return job_timeout_seconds(job_name, JOB_TIMEOUT_SECONDS)


def _acquire_radar_provider_lease(policy: OptionHistoryPolicyRepository) -> tuple[Any | None, int]:
    """Acquire capacity for the daily source-plus-publication refresh.

    Robinhood capacity can be occupied briefly by an RTH recovery detector or a
    just-finished history capture.  A hard refresh is only useful if it reaches
    the publication step, so wait through bounded, observable contention rather
    than recording a terminal successful skip with yesterday's radar still live.
    """

    for attempt in range(len(RADAR_PROVIDER_LEASE_RETRY_DELAYS_SECONDS) + 1):
        lease = policy.acquire_provider_lease(
            provider="robinhood",
            workload="options_radar",
            symbol="RADAR",
            ttl_seconds=(
                _job_timeout_seconds("options_radar_hard_refresh")
                or JOB_TIMEOUT_SECONDS["options_radar_hard_refresh"]
            ),
        )
        if lease is not None:
            return lease, attempt + 1
        if attempt < len(RADAR_PROVIDER_LEASE_RETRY_DELAYS_SECONDS):
            time.sleep(RADAR_PROVIDER_LEASE_RETRY_DELAYS_SECONDS[attempt])
    return None, len(RADAR_PROVIDER_LEASE_RETRY_DELAYS_SECONDS) + 1


def run_options_radar_hard_refresh(config_path: str | None = "config.yaml") -> dict[str, Any]:
    """Pull fresh option chains, then rematerialize the visible radar snapshot."""

    config = load_config(config_path)
    policy = OptionHistoryPolicyRepository(runtime_for_url(database_url(config)))
    lease, lease_attempts = _acquire_radar_provider_lease(policy)
    if lease is None:
        return {
            "ok": False,
            "status": "failed",
            "failedStep": "acquire_provider_lease",
            "reason": "provider_capacity_deferred",
            "provider_lease_attempts": lease_attempts,
            "error": (
                "Robinhood option radar refresh deferred after "
                f"{lease_attempts} provider-capacity attempts"
            ),
        }
    try:
        source = update_robinhood_options.run(config_path)
    finally:
        policy.release_provider_lease(lease.id)
    source_status = str(source.get("status") or "").strip().lower()
    if source_status not in {"ok", "partial"}:
        return {
            "ok": False,
            "status": "failed",
            "failedStep": "update_robinhood_options",
            "error": f"Robinhood option refresh returned {source_status or 'unknown'}",
            "source": source,
            "source_result": source,
            "source_status": source_status or "failed",
            "downstream_status": "not_run",
        }
    source_symbols = source.get("symbols")
    radar_symbols = [str(symbol).upper() for symbol in source_symbols if symbol] if isinstance(source_symbols, list) else None
    if radar_symbols == []:
        radar = {"status": "skipped", "reason": "no_incremental_symbols", "source": "robinhood"}
    else:
        radar = refresh_options_radar.run_signal_only(config_path, symbols=radar_symbols, source="robinhood")
    radar_status = str(radar.get("status") or "succeeded").strip().lower()
    composite_status = "partial" if source_status == "partial" or radar_status in {"partial", "failed"} else "succeeded"
    return {
        "ok": True,
        "status": composite_status,
        "provider_lease_attempts": lease_attempts,
        "source": source,
        "source_result": source,
        "source_status": source_status,
        "downstream_status": radar_status,
        "options_radar": radar,
    }


def run_source_with_material_thesis(
    config_path: str | None,
    source_runner: JobRunner,
) -> dict[str, Any]:
    """Refresh a source, then evaluate only symbols whose evidence changed."""
    source = source_runner(config_path)
    source_status = str(source.get("status") or "ok").lower()
    if source_status not in {"ok", "partial"}:
        return {
            **source,
            "source_result": source,
            "source_status": source_status,
            "downstream_status": "not_run",
        }
    affected_symbols = sorted({str(symbol).upper() for symbol in source.get("affected_symbols") or [] if symbol})
    if "affected_symbols" in source and not affected_symbols:
        monitor = {
            "status": "ok", "completed": 0, "failed": 0, "skipped": 0,
            "results": [], "errors": [], "reason": "no_changed_symbols",
        }
    else:
        monitor = run_thesis_monitor.run(
            config_path,
            symbols=affected_symbols or None,
            trigger="material_event",
        )
    monitor_status = str(monitor.get("status") or "failed").lower()
    composite_status = "partial" if monitor_status in {"partial", "failed"} else source_status
    return {
        **source, "status": composite_status, "ok": composite_status == "ok",
        "source_result": source,
        "source_status": source_status,
        "downstream_status": monitor_status,
        "material_thesis_monitor": monitor,
        "downstream_thesis_monitor": monitor,
    }


ALLOWLIST: dict[str, JobRunner] = {
    "full_market_refresh": lambda config_path: postgres_refresh.full(config_path, continue_on_error=True),
    "daily_screen": lambda config_path: postgres_refresh.full(config_path, continue_on_error=True),
    "refresh_decision_models": lambda config_path: postgres_refresh.publish_decisions(config_path),
    "update_preopen_daily_brief_scheduled": lambda config_path: postgres_refresh.scheduled_preopen(config_path),
    "hourly_options_radar": lambda config_path: refresh_options_radar.run_signal_only(config_path),
    "premarket_options_intelligence": lambda config_path: postgres_refresh.premarket(config_path),
    # IBKR option chains (price/greeks/OI/volume) persisted as source='ibkr' — the
    # reliable option source replacing the rate-limited TradingView+yfinance combo.
    "update_ibkr_options": lambda config_path: update_ibkr_options.run(config_path),
    # Robinhood option chains (price/greeks/OI/volume) persisted as
    # source='robinhood'. This is market-data only; no account or order tools.
    "update_robinhood_options": lambda config_path: update_robinhood_options.run(config_path),
    "robinhood_option_history": lambda config_path: robinhood_option_history.run(config_path),
    "detect_option_events": lambda config_path: detect_option_events.run(config_path),
    "refresh_options_radar": lambda config_path: refresh_options_radar.run(config_path),
    # Agent-free rematerialization for the in-process continuous scheduler. Codex
    # thesis/postmortem workers stay on the daily premarket cadence; this path
    # only recomputes deterministic option math, gates, and ranking.
    "refresh_options_radar_deterministic": lambda config_path: refresh_options_radar.run_deterministic_only(config_path),
    # Fast fresh-signal rematerialization (no agents, no heavy learning pass) for
    # the continuous 15-min loop; the full deterministic refresh (with learning)
    # runs on a slower cadence.
    "refresh_options_radar_signal": lambda config_path: refresh_options_radar.run_signal_only(config_path),
    # IBKR-scoped fast signal refresh for the cutover: rematerializes from the
    # reliable source='ibkr' chains only (clean OI/volume/greeks, no peer conflict).
    "refresh_options_radar_signal_ibkr": lambda config_path: refresh_options_radar.run_signal_only(config_path, source="ibkr"),
    "refresh_options_radar_signal_robinhood": lambda config_path: refresh_options_radar.run_signal_only(config_path, source="robinhood"),
    "options_radar_hard_refresh": run_options_radar_hard_refresh,
    "refresh_options_radar_learning_marks": lambda config_path: refresh_options_radar.run_learning_marks(config_path),
    "run_option_agents": lambda config_path: run_option_agents.run(config_path),
    "run_agent_experiment": lambda config_path: run_agent_experiment.run(config_path),
    "run_option_recovery_agents": lambda config_path: run_option_recovery_agents.run(config_path),
    "process_options_paper_orders": lambda config_path: options_paper_execution.run(config_path),
    "sync_decision_inbox": lambda config_path: decision_inbox.run(config_path),
    "refresh_symbol_decision_outcomes": lambda config_path: refresh_symbol_decision_outcomes.run(config_path),
    "run_stock_alpha_walk_forward": lambda config_path: stock_alpha_walk_forward.scheduled(config_path),
    # Manual run: forces the consolidated agent over the full open queue whenever a
    # command is configured, independent of the auto-run (enabled) toggle.
    "run_option_agents_force": lambda config_path: run_option_agents.run(config_path, force=True),
    # On-demand run: processes only user-requested (ondemand:) thesis requests.
    "run_option_agents_ondemand": lambda config_path: run_option_agents.run(config_path, ondemand=True),
    "run_thesis_monitor": lambda config_path: run_thesis_monitor.run(config_path, trigger="preopen"),
    "run_thesis_monitor_force": lambda config_path: run_thesis_monitor.run(config_path, trigger="manual", force=True),
    "run_thesis_monitor_preflight": lambda config_path: run_thesis_monitor.run(config_path, trigger="manual", force=True, dry_run=True),
    "update_broker_sources": lambda config_path: update_broker_sources.run(config_path),
    # Ticker data requests use stable, user-visible operation names. Each
    # request points to a source-specific collector or to an explicit
    # missing-source report; no unrelated collector is presented as proof that
    # the requested field was collected.
    "update_broker_account": lambda config_path: update_broker_sources.run(config_path),
    "update_market_data": lambda config_path: update_market_data.run(config_path),
    "update_market_valuations": lambda config_path: update_market_valuations.run(config_path),
    "update_company_financials": lambda config_path: update_company_financials.run(config_path),
    "update_earnings_and_estimates": lambda config_path: ticker_data_requests.update_earnings_and_estimates(config_path),
    "update_macro_series": lambda config_path: ticker_data_requests.update_macro_series(config_path),
    "update_phase2_sources": lambda config_path: update_phase2_sources.run(config_path),
    "update_short_interest_and_borrow": lambda config_path: ticker_data_requests.update_short_interest_and_borrow(config_path),
    "publish_ticker_benchmark": lambda config_path: ticker_decisions.publish_benchmark(config_path),
    "update_theses": lambda config_path: run_thesis_monitor.run(config_path, trigger="manual"),
    "update_decision_models": lambda config_path: postgres_refresh.publish_decisions(config_path),
    "market-refresh-decision-models": lambda config_path: postgres_refresh.publish_decisions(config_path),
    "market-update-event-calendar": lambda config_path: update_market_events.run(config_path),
    "market-publish-ticker-decisions": lambda config_path: ticker_decisions.publish(config_path),
    "market-update-disclosures": lambda config_path: run_source_with_material_thesis(config_path, update_disclosure_sources.run),
    # Preserve the established UI/automation job names while routing them to
    # PostgreSQL-native implementations.
    "update_free_sources": lambda config_path: update_market_data.run(config_path),
    "update_free_sources_radar": lambda config_path: update_market_data.run(config_path),
    "update_research_sources": lambda config_path: run_source_with_material_thesis(config_path, update_content_sources.run_research),
    "update_social_sources": lambda config_path: run_source_with_material_thesis(config_path, update_content_sources.run_social),
    "update_event_calendar": lambda config_path: run_source_with_material_thesis(config_path, update_market_events.run),
    "update_disclosures": lambda config_path: run_source_with_material_thesis(config_path, update_disclosure_sources.run),
    "update_arco_data": lambda config_path: run_source_with_material_thesis(config_path, update_arco_sources.run),
    "postgres_retention": lambda config_path: RetentionRepository(
        runtime_for_url(database_url(load_config(config_path)))
    ).prune(),
    "snapshot_database": lambda config_path: snapshot_database.run(config_path),
}


def refresh_job_rows(db_path: Any) -> list[dict[str, Any]]:
    repository = _job_repository(db_path)
    repository.mark_stale()
    return repository.rows()


def fail_running_jobs(db_path: Any, reason: str) -> int:
    return _job_repository(db_path).fail_all_running(reason)


def mark_stale_running_jobs(
    db_path: Any,
    *,
    stale_after: timedelta = timedelta(hours=3),
    retries: int = 30,
) -> int:
    return _job_repository(db_path).mark_stale(stale_after=stale_after)


def start_refresh_job(
    job_name: str,
    db_path: Any,
    *,
    scheduled_due_at: Any | None = None,
    dispatched_at: Any | None = None,
) -> dict[str, Any]:
    if job_name not in ALLOWLIST:
        allowed = ", ".join(sorted(ALLOWLIST))
        raise ValueError(f"refresh job is not allowlisted: {job_name}. Allowed jobs: {allowed}")

    return _job_repository(db_path).start(
        job_name,
        scheduled_due_at=scheduled_due_at,
        dispatched_at=dispatched_at,
    )


def execute_refresh_job(
    job_id: str,
    job_name: str,
    db_path: Any,
    config_path: str | None = "config.yaml",
    *,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    if job_name not in ALLOWLIST:
        allowed = ", ".join(sorted(ALLOWLIST))
        raise ValueError(f"refresh job is not allowlisted: {job_name}. Allowed jobs: {allowed}")
    repository = _job_repository(db_path, config_path)
    try:
        with _heartbeat_while_running(repository, job_id):
            summary = ALLOWLIST[job_name](config_path)
    except Exception as exc:
        error = f"{exc}\n{traceback.format_exc()}"
        repository.finish(job_id, "failed", error=error, summary={"error": str(exc)})
        if raise_on_error:
            raise
        return {"id": job_id, "job_name": job_name, "status": "failed", "error": str(exc)}

    failure = summary_failure_message(summary)
    if failure:
        source_status = summary.get("source_status") if isinstance(summary, dict) else None
        downstream = summary.get("downstream_status") if isinstance(summary, dict) else None
        repository.finish(
            job_id,
            "failed",
            error=failure,
            summary=summary,
            source_status=str(source_status) if source_status else None,
            downstream_status=str(downstream) if downstream else None,
        )
        return {"id": job_id, "job_name": job_name, "status": "failed", "error": failure, "summary": summary}

    status = summary_terminal_status(summary)
    source_status = summary.get("source_status") if isinstance(summary, dict) else None
    downstream = summary.get("downstream_status") if isinstance(summary, dict) else None
    repository.finish(
        job_id,
        status,
        summary=summary,
        source_status=str(source_status) if source_status else None,
        downstream_status=str(downstream) if downstream else None,
    )
    return {"id": job_id, "job_name": job_name, "status": status, "summary": summary}


def finish_refresh_job_failed(job_id: str, job_name: str, db_path: Any, error: str) -> dict[str, Any]:
    _job_repository(db_path).finish(job_id, "failed", error=error)
    return {"id": job_id, "job_name": job_name, "status": "failed", "error": error}


@contextmanager
def _heartbeat_while_running(
    repository: JobRepository,
    job_id: str,
    *,
    interval_seconds: float = JOB_HEARTBEAT_SECONDS,
) -> Iterator[None]:
    stop = Event()

    def pulse() -> None:
        while not stop.wait(interval_seconds):
            if not repository.heartbeat(job_id):
                return

    repository.heartbeat(job_id)
    worker = Thread(target=pulse, name=f"market-job-heartbeat-{job_id}", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=max(1.0, interval_seconds))


def execute_refresh_job_subprocess(
    job_id: str,
    job_name: str,
    db_path: Any,
    config_path: str | None = "config.yaml",
) -> dict[str, Any]:
    repository = _job_repository(db_path, config_path)
    spec = RefreshProcessSpec(
        job_id=job_id,
        job_name=job_name,
        database_url=repository.runtime.dsn,
        config_path=config_path or "config.yaml",
    )
    return execute_sync(
        spec,
        lambda error: finish_refresh_job_failed(job_id, job_name, db_path, error),
        timeout_overrides=JOB_TIMEOUT_SECONDS,
        run_process=subprocess.run,
    )


def run_refresh_job(job_name: str, db_path: Any, config_path: str | None = "config.yaml") -> dict[str, Any]:
    job = start_refresh_job(job_name, db_path)
    if not job.get("created"):
        return job
    return execute_refresh_job(job["id"], job_name, db_path, config_path)


def _job_repository(database: Any, config_path: str | None = "config.yaml") -> JobRepository:
    if isinstance(database, str) and database.startswith(("postgresql://", "postgresql+psycopg://")):
        dsn = database
    elif isinstance(database, dict) or getattr(database, "database", None) is not None:
        dsn = database_url(database)
    else:
        dsn = load_config(config_path).database.url
    return JobRepository(runtime_for_url(dsn))


def summary_failure_message(summary: Any) -> str | None:
    if not isinstance(summary, dict):
        return None
    if summary.get("status") in {"partial", "skipped"}:
        return None
    if summary.get("ok") is not False and summary.get("status") != "failed":
        return None
    error = summary.get("error")
    if isinstance(error, str) and error:
        return error
    source_errors = summary.get("source_errors")
    if isinstance(source_errors, list):
        failed_sources = [
            str(item.get("name") or "").strip()
            for item in source_errors
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        if failed_sources:
            return f"Refresh failed for sources: {', '.join(failed_sources[:3])}"
    failed_step = summary.get("failedStep")
    if isinstance(failed_step, str) and failed_step:
        return f"Refresh failed at {failed_step}"
    return "Refresh failed"


def summary_terminal_status(summary: Any) -> str:
    if isinstance(summary, dict) and summary.get("status") in {"partial", "skipped"}:
        return str(summary["status"])
    return "succeeded"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_name")
    parser.add_argument("--db-path", help="Deprecated non-secret database reference")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--job-id")
    args = parser.parse_args(argv)

    if args.job_id:
        result = execute_refresh_job(args.job_id, args.job_name, args.db_path, args.config, raise_on_error=False)
    else:
        result = run_refresh_job(args.job_name, args.db_path, args.config)
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
