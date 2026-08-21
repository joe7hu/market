"""Application actions for Event Scout transport endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from investment_panel.core.config import AppConfig
from investment_panel.core.event_scout import EventScout, replay_mrna
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.event_scout import persist_event_packet


MAX_EVENT_SCOUT_CLOCK_SKEW = timedelta(minutes=5)


def _reference_time(value: datetime | None) -> datetime:
    reference = value or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return reference.astimezone(UTC)


def _prepare_signal(signal: dict[str, Any], reference: datetime) -> dict[str, Any]:
    prepared = dict(signal)
    raw_observed_at = prepared.get("observed_at") or prepared.get("as_of")
    if raw_observed_at is None:
        prepared["observed_at"] = reference.isoformat()
        return prepared
    if isinstance(raw_observed_at, datetime):
        observed_at = raw_observed_at
    else:
        try:
            observed_at = datetime.fromisoformat(str(raw_observed_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    observed_at = observed_at.astimezone(UTC)
    if observed_at > reference + MAX_EVENT_SCOUT_CLOCK_SKEW:
        raise ValueError("observed_at is too far in the future")
    # Allow a small provider clock skew, but never publish a packet whose
    # point-in-time cutoff is ahead of the server clock.
    prepared["observed_at"] = min(observed_at, reference).isoformat()
    return prepared


def persist_mrna_replay(config: AppConfig, *, symbol: str) -> dict[str, Any]:
    if symbol.strip().upper() != "MRNA":
        raise ValueError("Only the MRNA acceptance replay is available")
    packet = replay_mrna()
    persist_event_packet(runtime_for_config(config), packet, {
        "symbol": packet["symbol"],
        "trigger_type": packet["trigger_type"],
        "observed_at": packet["as_of"],
        "source_url": packet.get("source_url"),
        "source_kind": packet.get("source_kind") or "replay_fixture",
        "status": "replay",
        "cooldown_until": None,
        "collection_status": {"replay": "fixture_only"},
    })
    return packet


def process_signal(config: AppConfig, signal: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    reference = _reference_time(now)
    prepared_signal = _prepare_signal(signal, reference)
    runtime = runtime_for_config(config)
    result = EventScout().process_signal(prepared_signal, now=reference)
    if result.get("accepted"):
        persisted = persist_event_packet(
            runtime,
            result["packet"],
            result["scout_event"],
            enforce_cooldown=True,
            reference_at=reference,
        )
        if persisted is None:
            return {
                "status": "cooldown_or_invalid",
                "accepted": False,
                "symbol": str(signal.get("symbol") or "").upper(),
            }
    return result


__all__ = ["persist_mrna_replay", "process_signal"]
