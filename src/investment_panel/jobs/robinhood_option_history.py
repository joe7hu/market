"""Capture one complete Robinhood option chain per configured symbol/15-minute slot."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, time, timedelta
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.decision import MARKET_CLOSE, MARKET_OPEN, MARKET_TZ, is_us_market_day
from investment_panel.core.robinhood_options import RobinhoodClient, collect_robinhood_full_option_chain
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.options_history import OptionHistoryRepository
from investment_panel.database.options_history_policy import EVENT_PROFILE, HISTORY_PROFILE, OptionHistoryPolicyRepository
from investment_panel.database.option_events import OptionEventRepository
from investment_panel.database.options_recovery_execution import RecoveryExecutionRepository


def history_slot(now: datetime | None = None) -> datetime | None:
    """Return the active 15-minute ET slot, including the 16:00 closing slot."""

    reference = (now or datetime.now(UTC)).astimezone(MARKET_TZ)
    if not is_us_market_day(reference.date()):
        return None
    # Allow the 16:00 slot to begin up to the option-session close grace window.
    if reference.time() < MARKET_OPEN or reference.time() >= time(16, 15):
        return None
    minute = (reference.minute // 15) * 15
    local_slot = reference.replace(minute=minute, second=0, microsecond=0)
    if local_slot.time() > MARKET_CLOSE:
        local_slot = local_slot.replace(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute)
    return local_slot.astimezone(UTC)


def run(
    config_path: str | None = "config.yaml",
    *,
    now: datetime | None = None,
    client: RobinhoodClient | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    provider = config.data_sources.brokers.robinhood
    slot_at = history_slot(now)
    if not provider.enabled:
        return {"status": "skipped", "reason": "robinhood_disabled", "symbols": []}
    if not provider.history_enabled:
        return {"status": "skipped", "reason": "history_disabled", "symbols": []}
    if slot_at is None:
        return {"status": "skipped", "reason": "outside_regular_session", "symbols": []}
    runtime = runtime_for_config(config)
    ingestion = IngestionRepository(runtime)
    history = OptionHistoryRepository(
        runtime,
        options_risk_sleeve_capital=config.analysis.options_decision_system.options_risk_sleeve_capital,
    )
    policy = OptionHistoryPolicyRepository(runtime)
    events = OptionEventRepository(runtime)
    history.defer_stale_running_captures(
        source_id="robinhood",
        stale_after=timedelta(seconds=int(provider.max_collection_seconds) + 120),
    )
    history.defer_stale_running_captures(
        source_id="robinhood",
        collection_profile=EVENT_PROFILE,
        workload="option_event",
        stale_after=timedelta(seconds=int(provider.max_collection_seconds) + 120),
    )
    use_policy = True
    try:
        scheduled = policy.due_symbols(now)
    except Exception:
        use_policy = False
        scheduled = [
            {"symbol": symbol, "slot_at": slot_at, "publication_cap": "PAPER_READY"}
            for symbol in dict.fromkeys(symbol for symbol in provider.history_symbols if symbol)
        ]
    symbols = [str(item["symbol"]).upper() for item in scheduled]
    ingestion.register_source(
        "robinhood", name="Robinhood", family="broker", kind="option_chain",
        capabilities={"option_quotes": True, "option_history_full": True},
    )
    captures: list[dict[str, Any]] = []
    for schedule in scheduled:
        symbol = str(schedule["symbol"]).upper()
        capture_slot_at = schedule.get("slot_at") or slot_at
        profile = str(schedule.get("profile") or HISTORY_PROFILE)
        event_id = str(schedule.get("event_id") or "") if profile == EVENT_PROFILE else None
        universe = f"event-strip:{event_id}" if event_id else None
        capability = "option_event_strip" if event_id else "option_history_full"
        workload = "option_event" if event_id else "option_history"
        with ingestion.run("robinhood", capability) as run:
            if history.claim_slot(
                source_id="robinhood",
                symbol=symbol,
                slot_at=capture_slot_at,
                run_id=run.id,
                collection_profile=profile,
                universe=universe,
            ) is None:
                run.finish("skipped", summary={"symbol": symbol, "slot_at": capture_slot_at.isoformat(), "reason": "slot_already_claimed"})
                captures.append({"symbol": symbol, "profile": profile, "status": "skipped", "reason": "slot_already_claimed"})
                continue
            lease_id = None
            if use_policy:
                lease = policy.acquire_provider_lease(
                    provider="robinhood",
                    workload=workload,
                    symbol=symbol,
                    ttl_seconds=int(provider.max_collection_seconds) + 120,
                )
                if lease is None:
                    history.defer_capture(
                        source_id="robinhood", symbol=symbol, slot_at=capture_slot_at, run_id=run.id,
                        reason="provider_capacity_deferred", collection_profile=profile, universe=universe,
                    )
                    if event_id:
                        events.record_terminal_capture(
                            event_id,
                            scheduled_at=capture_slot_at,
                            status="deferred",
                            reason="provider_capacity_deferred",
                        )
                    run.finish("skipped", summary={"symbol": symbol, "slot_at": capture_slot_at.isoformat(), "reason": "provider_capacity_deferred"})
                    captures.append({"symbol": symbol, "profile": profile, "status": "skipped", "reason": "provider_capacity_deferred"})
                    continue
                lease_id = lease.id
            try:
                captured = collect_robinhood_full_option_chain(provider, symbol, client=client)
                selection = None
                if event_id:
                    captured, selection = events.filter_event_strip(event_id, captured, as_of=capture_slot_at)
                stored = history.store_capture(
                    run_id=run.id,
                    source_id="robinhood",
                    symbol=symbol,
                    slot_at=capture_slot_at,
                    captured=captured,
                    minimum_completeness=provider.history_min_completeness,
                    collection_profile=profile,
                    universe=universe,
                    materialize=not event_id,
                )
                event_capture = (
                    events.record_capture(event_id, stored=stored, selection=selection)
                    if event_id and selection is not None
                    else None
                )
                recovery = None
                if event_id and event_capture is not None:
                    # Selection is deterministic and isolated from provider capture:
                    # a temporary scoring failure never discards the event strip.
                    try:
                        from investment_panel.core.options_recovery_paper import recovery_risk_policy

                        execution = RecoveryExecutionRepository(
                            runtime,
                            risk_policy=recovery_risk_policy(config.analysis.options_decision_system),
                        )
                        recovery = execution.evaluate_capture(
                            event_id,
                            capture_id=event_capture["event_capture_id"],
                        )
                        recovery["paper_staging"] = execution.stage_qualified_orders(
                            event_id,
                            enabled=(
                                config.analysis.options_decision_system.options_paper_actions_enabled
                                and config.analysis.options_decision_system.recovery_paper_actions_enabled
                            ),
                        )
                        recovery["paper_management"] = execution.manage_event_orders(event_id)
                        if config.analysis.options_decision_system.decision_inbox_enabled:
                            from investment_panel.database.decision_inbox import DecisionInboxRepository

                            inbox = DecisionInboxRepository(runtime)
                            for managed in recovery["paper_management"].get("orders") or []:
                                status = str(managed.get("status") or "").lower()
                                if status in {"entered", "exited", "invalidated"} and managed.get("paper_order_id"):
                                    inbox.record_paper_lifecycle(
                                        str(managed["paper_order_id"]), status=status,
                                    )
                        from investment_panel.database.options_recovery_learning import RecoveryLearningRepository

                        learning = RecoveryLearningRepository(runtime)
                        # Persist paper lifecycle first so the outcome owner
                        # sees a real filled/exited ticket, never a stale
                        # shadow status from the preceding capture.
                        recovery["paper_lifecycle"] = learning.sync_paper_lifecycle(event_id)
                        recovery["learning"] = learning.refresh_outcomes()
                        recovery["promotion"] = learning.auto_promote_eligible(
                            enabled=config.analysis.options_decision_system.strategy_auto_promotion_enabled,
                            recovery_paper_actions_enabled=(
                                config.analysis.options_decision_system.recovery_paper_actions_enabled
                            ),
                        )
                        # Advisory work is only queued here.  Its own worker
                        # records failure telemetry; it can never unwind a
                        # successful provider capture or deterministic ticket.
                        try:
                            from investment_panel.database.options_recovery_agents import RecoveryEventAgentRepository

                            settings = config.analysis.options_decision_system
                            recovery["agent_queue"] = RecoveryEventAgentRepository(runtime).queue_if_material(
                                event_id,
                                capture_id=event_capture["event_capture_id"],
                                model=config.agents.option_agent.model,
                                reasoning_effort=config.agents.option_agent.reasoning_effort,
                                debounce_minutes=settings.event_agent_debounce_minutes,
                                max_batches_per_symbol_per_day=settings.event_agent_max_batches_per_symbol_per_day,
                                max_tasks=settings.event_agent_max_tasks_per_batch,
                            )
                        except Exception as agent_exc:  # no agent path can block the tape
                            recovery["agent_queue"] = {
                                "status": "failed",
                                "error": f"{type(agent_exc).__name__}: {agent_exc}",
                            }
                    except Exception as exc:  # pragma: no cover - provider path safety net
                        recovery = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                status = "succeeded" if stored["capture_state"] == "complete" else "partial"
                run.finish(
                    status, item_count=stored["received_contract_count"], instrument_count=1,
                    failure_detail="; ".join(stored["errors"][:10]) or None, summary=stored,
                )
                captures.append({
                    "symbol": symbol,
                    "profile": profile,
                    "event_capture": event_capture,
                    "recovery": recovery,
                    "status": status,
                    **stored,
                })
            except Exception as exc:
                history.fail_capture(
                    source_id="robinhood",
                    symbol=symbol,
                    slot_at=capture_slot_at,
                    run_id=run.id,
                    error=exc,
                    collection_profile=profile,
                    universe=universe,
                )
                if event_id:
                    events.record_terminal_capture(
                        event_id,
                        scheduled_at=capture_slot_at,
                        status="failed",
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                run.finish("failed", failure_detail=f"{type(exc).__name__}: {exc}", summary={"symbol": symbol, "slot_at": capture_slot_at.isoformat()})
                captures.append({"symbol": symbol, "profile": profile, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                if lease_id is not None:
                    policy.release_provider_lease(lease_id)
    complete = [capture for capture in captures if capture.get("status") == "succeeded"]
    failed = [capture for capture in captures if capture.get("status") == "failed"]
    return {
        "status": "failed" if failed and not complete else ("partial" if failed or len(complete) != len(captures) else "ok"),
        "slot_at": slot_at.isoformat(), "symbols": symbols, "captures": captures,
        "complete_symbols": len(complete), "failed_symbols": len(failed),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.config), default=str))


if __name__ == "__main__":
    main()
