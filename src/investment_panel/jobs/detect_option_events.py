"""Refresh confirmed Robinhood quotes, then detect recovery events.

No detector path is allowed to reuse stale quote rows.  A failed lease,
provider request, or ingestion run records an explainable failed detector run
and exits before it can update or close a recovery event.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import time
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.decision import MARKET_TZ, is_us_market_day, market_session_bounds
from investment_panel.core.job_policy import job_timeout_seconds
from investment_panel.core.robinhood_options import RobinhoodClient, collect_robinhood_equity_quotes
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.option_events import OptionEventRepository
from investment_panel.database.options_history_policy import OptionHistoryPolicyRepository
from investment_panel.database.options_recovery_cohorts import RecoveryCohortRepository


def run(
    config_path: str | None = "config.yaml",
    *,
    now: datetime | None = None,
    client: RobinhoodClient | None = None,
) -> dict[str, Any]:
    """Run the regular-hours refresh-then-detect transaction boundary."""

    config = load_config(config_path)
    reference = now or datetime.now(UTC)
    slot = detector_slot(reference)
    runtime = runtime_for_config(config)
    repository = OptionEventRepository(runtime)
    cohorts = RecoveryCohortRepository(runtime)
    provider = config.data_sources.brokers.robinhood
    if slot is None:
        return {
            "status": "skipped", "reason": "outside_regular_session",
            "checked_at": reference.isoformat(), "capture_health": repository.capture_health(now=reference),
        }
    started = datetime.now(UTC)
    cohort = cohorts.current()
    if cohort is None:
        return {
            "status": "failed", "reason": "current_recovery_cohort_missing",
            "checked_at": reference.isoformat(), "capture_health": repository.capture_health(now=reference),
        }
    if not provider.enabled:
        cohorts.record_detector_run(
            scheduled_at=slot, started_at=started, finished_at=datetime.now(UTC), expected_symbols=0,
            received_symbols=0, fresh_symbols=0, quote_age_p95_minutes=None, provider_run_id=None,
            status="failed", failure_reasons=["robinhood_disabled"],
        )
        cohorts.refresh_program_session(trading_date=slot.astimezone(MARKET_TZ).date(), now=reference)
        return _failed(repository, reference, "robinhood_disabled")

    ingestion = IngestionRepository(runtime)
    policy = OptionHistoryPolicyRepository(runtime)
    symbols = ingestion.option_universe()
    if not symbols:
        cohorts.record_detector_run(
            scheduled_at=slot, started_at=started, finished_at=datetime.now(UTC), expected_symbols=0,
            received_symbols=0, fresh_symbols=0, quote_age_p95_minutes=None, provider_run_id=None,
            status="succeeded", details={"triggering_quote_count": 0, "fresh_triggering_quote_count": 0},
        )
        cohorts.refresh_program_session(trading_date=slot.astimezone(MARKET_TZ).date(), now=reference)
        return {
            "status": "ok", "reason": "empty_effective_universe", "checked_at": reference.isoformat(),
            "capture_health": repository.capture_health(now=reference),
        }
    lease = policy.acquire_provider_lease(
        provider="robinhood", workload="option_event_detector", symbol="RECOVERY",
        now=reference, ttl_seconds=max(90, int(provider.timeout_seconds) + 60),
    )
    if lease is None:
        cohorts.record_detector_run(
            scheduled_at=slot, started_at=started, finished_at=datetime.now(UTC), expected_symbols=len(symbols),
            received_symbols=0, fresh_symbols=0, quote_age_p95_minutes=None, provider_run_id=None,
            status="failed", failure_reasons=["provider_capacity_deferred"],
        )
        cohorts.refresh_program_session(trading_date=slot.astimezone(MARKET_TZ).date(), now=reference)
        return _failed(repository, reference, "provider_capacity_deferred")
    provider_run_id: str | None = None
    try:
        ingestion.register_source(
            "robinhood", name="Robinhood", family="broker", kind="market_data",
            capabilities={"quotes": True, "option_quotes": True, "recovery_detector": True},
        )
        with ingestion.run("robinhood", "equity_quotes") as provider_run:
            provider_run_id = str(provider_run.id)
            # Leave time for ingestion, durable failure accounting, and lease
            # release.  The concrete MCP client observes this same monotonic
            # deadline per request, so a slow multi-batch universe cannot be
            # killed by the outer 90-second job wrapper before it records a
            # failed detector run.
            collection_deadline = _detector_collection_deadline()
            payload = collect_robinhood_equity_quotes(
                provider, symbols, client=client, deadline=collection_deadline, regular_session_only=True,
            )
            provider_errors = list(payload.get("errors") or [])
            stored = ingestion.store_quotes(provider_run.id, "robinhood", list(payload.get("rows") or []))
            failed_reason = (
                "provider_quote_batch_deadline_exceeded"
                if "collector_deadline_exceeded" in provider_errors
                else "provider_quote_batch_failed"
                if provider_errors and not payload.get("rows")
                else "ingestion_quote_store_failed" if not stored else None
            )
            if failed_reason is not None:
                provider_run.finish(
                    "failed", item_count=0, instrument_count=0,
                    failure_detail="; ".join(str(item) for item in (provider_errors or [failed_reason])[:10]),
                    summary={"requested_symbols": len(symbols), "stored_quotes": stored, "errors": provider_errors[:20]},
                )
                cohorts.record_detector_run(
                    scheduled_at=slot, started_at=started, finished_at=datetime.now(UTC),
                    expected_symbols=len(symbols), received_symbols=0, fresh_symbols=0,
                    quote_age_p95_minutes=None, provider_run_id=str(provider_run.id), status="failed",
                    failure_reasons=[failed_reason, *provider_errors[:10]],
                )
                cohorts.refresh_program_session(trading_date=slot.astimezone(MARKET_TZ).date(), now=reference)
                return _failed(repository, reference, failed_reason)
            provider_run.finish(
                "succeeded", item_count=stored, instrument_count=len(set(payload.get("received_symbols") or [])),
                summary={"requested_symbols": len(symbols), "errors": provider_errors[:20]},
            )
            # Changed facts receive their availability timestamp during the
            # completed ingestion transaction.  The detector must use a
            # post-ingestion cutoff or it will exclude the quotes it just
            # confirmed.
            detection_reference = _post_ingestion_reference(reference, datetime.now(UTC))
            # Use the exact batch universe for both the provider accounting and
            # detection.  A symbol cannot disappear from the canary denominator
            # merely because it was sourced from a catalyst or research route
            # instead of a watchlist row.
            observations, report = repository.detector_observations(
                detection_reference,
                provider_run_id=str(provider_run.id),
                symbols=symbols,
            )
            fresh_ages = [float(value) for value in (report.get("fresh_quote_ages") or [])]
            detected = repository.detect_events(
                observations, now=detection_reference, require_valid_reference=True,
            )
            trigger_count = int(report.get("triggering_quote_count") or 0)
            fresh_trigger_count = int(report.get("fresh_triggering_quote_count") or 0)
            quality_defects: list[str] = []
            if report.get("stale_triggering_symbols"):
                quality_defects.append("stale_event_trigger_quote")
            if report.get("critical_reference_symbols"):
                quality_defects.append("invalid_reference_bar")
            if any("provider_timestamp_missing" in str(error) for error in provider_errors):
                quality_defects.append("provider_unconfirmed_quote")
            detector = cohorts.record_detector_run(
                scheduled_at=slot, started_at=started, finished_at=datetime.now(UTC),
                expected_symbols=len(symbols),
                received_symbols=int(report.get("received_symbols") or 0),
                fresh_symbols=int(report.get("fresh_symbols") or 0), quote_age_p95_minutes=_p95(fresh_ages),
                provider_run_id=str(provider_run.id), status="succeeded",
                # These are quality defects rather than transport failures.
                # Keeping them on the durable run lets the program projection
                # fail closed while preserving the successful provider run.
                failure_reasons=quality_defects, details={
                    "stored_quotes": stored, "provider_errors": provider_errors[:20],
                    "detector_exclusions": list(report.get("exclusions") or [])[:100],
                    "triggering_quote_count": trigger_count,
                    "fresh_triggering_quote_count": fresh_trigger_count,
                    "stale_triggering_symbols": list(report.get("stale_triggering_symbols") or [])[:100],
                    "critical_reference_symbols": list(report.get("critical_reference_symbols") or [])[:100],
                },
            )
            program = cohorts.refresh_program_session(
                trading_date=slot.astimezone(MARKET_TZ).date(), now=detection_reference,
            )
            return {
                **detected, "status": detected.get("status") or "ok", "checked_at": detection_reference.isoformat(),
                "provider_run_id": str(provider_run.id), "detector_run_id": str(detector["id"]) if detector else None,
                "detector_report": report, "program_session": program,
                "capture_health": repository.capture_health(now=detection_reference),
            }
    except Exception as exc:
        cohorts.record_detector_run(
            scheduled_at=slot, started_at=started, finished_at=datetime.now(UTC), expected_symbols=len(symbols),
            received_symbols=0, fresh_symbols=0, quote_age_p95_minutes=None, provider_run_id=provider_run_id,
            status="failed", failure_reasons=[f"{type(exc).__name__}:{exc}"],
        )
        cohorts.refresh_program_session(trading_date=slot.astimezone(MARKET_TZ).date(), now=reference)
        return _failed(repository, reference, f"detector_ingestion_failed:{type(exc).__name__}")
    finally:
        policy.release_provider_lease(lease.id)


def detector_slot(now: datetime) -> datetime | None:
    """Five-minute detector slot during the actual RTH session, inclusive."""

    local = now.astimezone(MARKET_TZ)
    if not is_us_market_day(local.date()):
        return None
    open_at, close_at = market_session_bounds(local.date())
    if local < open_at or local > close_at:
        return None
    minute = (local.minute // 5) * 5
    return local.replace(minute=minute, second=0, microsecond=0).astimezone(UTC)


def _failed(repository: OptionEventRepository, reference: datetime, reason: str) -> dict[str, Any]:
    return {
        "status": "failed", "reason": reason, "checked_at": reference.isoformat(),
        "capture_health": repository.capture_health(now=reference),
    }


def _post_ingestion_reference(scheduled_reference: datetime, completed_at: datetime) -> datetime:
    """Use the completed ingest boundary without moving a supplied clock backward."""

    return max(scheduled_reference, completed_at)


def _detector_collection_deadline() -> float | None:
    """Reserve job-tail time for PostgreSQL accounting and cleanup."""

    timeout_seconds = job_timeout_seconds("detect_option_events")
    if timeout_seconds is None:
        return None
    return time.monotonic() + max(1, int(timeout_seconds) - 15)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(float(value) for value in values)
    if len(values) == 1:
        return values[0]
    index = int(round(0.95 * (len(values) - 1)))
    return values[index]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.config), default=str))


if __name__ == "__main__":
    main()
