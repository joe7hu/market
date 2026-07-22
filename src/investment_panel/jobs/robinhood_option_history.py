"""Capture one complete Robinhood option chain per configured symbol/15-minute slot."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, time
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.decision import MARKET_CLOSE, MARKET_OPEN, MARKET_TZ, is_us_market_day
from investment_panel.core.robinhood_options import RobinhoodClient, collect_robinhood_full_option_chain
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.options_history import OptionHistoryRepository
from investment_panel.database.options_history_policy import OptionHistoryPolicyRepository


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
    history = OptionHistoryRepository(runtime)
    policy = OptionHistoryPolicyRepository(runtime)
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
        with ingestion.run("robinhood", "option_history_full") as run:
            if history.claim_slot(source_id="robinhood", symbol=symbol, slot_at=capture_slot_at, run_id=run.id) is None:
                run.finish("skipped", summary={"symbol": symbol, "slot_at": capture_slot_at.isoformat(), "reason": "slot_already_claimed"})
                captures.append({"symbol": symbol, "status": "skipped", "reason": "slot_already_claimed"})
                continue
            lease_id = None
            if use_policy:
                lease = policy.acquire_provider_lease(provider="robinhood", workload="option_history", symbol=symbol)
                if lease is None:
                    history.defer_capture(
                        source_id="robinhood", symbol=symbol, slot_at=capture_slot_at, run_id=run.id,
                        reason="provider_capacity_deferred",
                    )
                    run.finish("skipped", summary={"symbol": symbol, "slot_at": capture_slot_at.isoformat(), "reason": "provider_capacity_deferred"})
                    captures.append({"symbol": symbol, "status": "skipped", "reason": "provider_capacity_deferred"})
                    continue
                lease_id = lease.id
            try:
                captured = collect_robinhood_full_option_chain(provider, symbol, client=client)
                stored = history.store_capture(run_id=run.id, source_id="robinhood", symbol=symbol, slot_at=capture_slot_at, captured=captured, minimum_completeness=provider.history_min_completeness)
                status = "succeeded" if stored["capture_state"] == "complete" else "partial"
                run.finish(
                    status, item_count=stored["received_contract_count"], instrument_count=1,
                    failure_detail="; ".join(stored["errors"][:10]) or None, summary=stored,
                )
                captures.append({"symbol": symbol, "status": status, **stored})
            except Exception as exc:
                history.fail_capture(source_id="robinhood", symbol=symbol, slot_at=capture_slot_at, run_id=run.id, error=exc)
                run.finish("failed", failure_detail=f"{type(exc).__name__}: {exc}", summary={"symbol": symbol, "slot_at": capture_slot_at.isoformat()})
                captures.append({"symbol": symbol, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
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
