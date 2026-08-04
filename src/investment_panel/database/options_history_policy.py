"""Policy and provider-capacity authority for historical option collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
import socket
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.decision import MARKET_TZ, is_us_market_day, market_session_bounds
from investment_panel.database.instruments import canonical_symbol, reconcile_instrument
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


POLICY_REVISION = "options-chain-reliability-20260722"
MAX_PROVIDER_LEASES = 2
LEASE_SECONDS = 14 * 60
HISTORY_PROFILE = "history_full"
EVENT_PROFILE = "event_strip"
CORE_HISTORY_SYMBOLS = frozenset({"QQQ"})
RADAR_WORKLOADS = frozenset({"options_radar"})
HISTORY_WORKLOADS = frozenset({"option_history", "option_event", "option_event_detector"})


class PolicyConflict(ValueError):
    """Raised when a watchlist toggle races with a newer policy revision."""


@dataclass(frozen=True)
class ProviderLease:
    id: int
    provider: str
    workload: str
    symbol: str
    owner: str
    expires_at: datetime


class OptionHistoryPolicyRepository:
    """Own option-history enrollment, publication caps, and provider leases."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def ensure_seeded(self) -> None:
        with self.runtime.transaction() as connection:
            _seed_policy(connection, "QQQ", effective_state="active", collection_tier="core", cadence_minutes=15,
                         publication_cap="PAPER_READY", reason="core 15-minute QQQ history")
            _seed_policy(connection, "NVDA", effective_state="shadow", collection_tier="standard", cadence_minutes=60,
                         publication_cap="WATCH", reason="early NVDA hourly shadow exception")

    def symbols(self) -> dict[str, Any]:
        self.ensure_seeded()
        with self.runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT instrument.symbol, policy.requested_state, policy.effective_state,
                       policy.collection_tier, policy.cadence_minutes, policy.publication_cap,
                       policy.provider, policy.normalized_retention_days, policy.derived_retention_days,
                       policy.provider_payload_retention_days, policy.policy_revision,
                       policy.lock_version, policy.reason, policy.activated_at, policy.paused_at,
                       policy.requested_at, policy.updated_at,
                       latest.snapshot_id, latest.slot_at AS latest_complete_capture,
                       health.complete_captures
                FROM app.option_history_policy policy
                JOIN catalog.instrument instrument ON instrument.id = policy.instrument_id
                LEFT JOIN LATERAL (
                    SELECT snapshot.id AS snapshot_id, snapshot.slot_at
                    FROM raw.option_snapshot snapshot
                    WHERE snapshot.history_symbol = instrument.symbol
                      AND snapshot.collection_profile = %s
                      AND snapshot.latest_complete_generation_id IS NOT NULL
                    ORDER BY snapshot.slot_at DESC NULLS LAST, snapshot.id DESC LIMIT 1
                ) latest ON true
                LEFT JOIN LATERAL (
                    SELECT count(*) AS complete_captures
                    FROM raw.option_snapshot snapshot
                    WHERE snapshot.history_symbol = instrument.symbol
                      AND snapshot.collection_profile = %s
                      AND snapshot.latest_complete_generation_id IS NOT NULL
                ) health ON true
                WHERE policy.profile = %s
                ORDER BY CASE policy.collection_tier WHEN 'core' THEN 0 ELSE 1 END, instrument.symbol
                """
                , [HISTORY_PROFILE, HISTORY_PROFILE, HISTORY_PROFILE]
            ).fetchall()
        return {"rows": [_policy_payload(dict(row)) for row in rows], "count": len(rows), "policy_revision": POLICY_REVISION}

    def policy_for_symbol(self, symbol: str, *, profile: str = HISTORY_PROFILE) -> dict[str, Any] | None:
        self.ensure_seeded()
        normalized = canonical_symbol(symbol)
        with self.runtime.read() as connection:
            row = connection.execute(
                """
                SELECT instrument.id AS instrument_id, instrument.symbol, policy.*
                FROM app.option_history_policy policy
                JOIN catalog.instrument instrument ON instrument.id = policy.instrument_id
                WHERE instrument.symbol = %s AND policy.profile = %s
                """,
                [normalized, profile],
            ).fetchone()
        return _policy_payload(dict(row)) if row else None

    def set_requested_state(self, symbol: str, *, requested_state: str, lock_version: int) -> dict[str, Any]:
        normalized = canonical_symbol(symbol)
        if requested_state not in {"on", "off"}:
            raise ValueError("requested_state must be 'on' or 'off'")
        self.ensure_seeded()
        with self.runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, normalized, asset_class="equity", category="option-history")
            row = connection.execute(
                """
                INSERT INTO app.option_history_policy
                    (instrument_id, profile, requested_state, effective_state, collection_tier, cadence_minutes,
                     publication_cap, provider, normalized_retention_days, derived_retention_days,
                     provider_payload_retention_days, policy_revision, reason)
                VALUES (%s, %s, 'off', 'pending_gate', 'standard', 60, 'WATCH', 'robinhood',
                        730, 730, 90, %s, 'watchlist enrollment pending admission')
                ON CONFLICT (instrument_id, profile) DO NOTHING
                RETURNING lock_version
                """,
                [instrument_id, HISTORY_PROFILE, POLICY_REVISION],
            ).fetchone()
            current = connection.execute(
                """
                SELECT instrument.symbol, policy.*
                FROM app.option_history_policy policy
                JOIN catalog.instrument instrument ON instrument.id = policy.instrument_id
                WHERE policy.instrument_id = %s AND policy.profile = %s
                FOR UPDATE
                """,
                [instrument_id, HISTORY_PROFILE],
            ).fetchone()
            if current is None:
                raise ValueError("option-history policy could not be created")
            if row is None and int(current["lock_version"]) != int(lock_version):
                raise PolicyConflict("option-history policy was modified; reload before retrying")
            effective_state = "disabled" if requested_state == "off" else _on_state(dict(current))
            updated = connection.execute(
                """
                UPDATE app.option_history_policy
                SET requested_state = %s,
                    effective_state = %s,
                    requested_at = now(),
                    updated_at = now(),
                    paused_at = CASE WHEN %s = 'off' THEN now() ELSE paused_at END,
                    activated_at = CASE WHEN %s IN ('active', 'shadow') THEN COALESCE(activated_at, now()) ELSE activated_at END,
                    reason = CASE WHEN %s = 'off' THEN 'watchlist options-history toggle off'
                                  WHEN effective_state = 'pending_gate' THEN 'watchlist enrollment pending admission'
                                  ELSE reason END,
                    lock_version = lock_version + 1
                WHERE instrument_id = %s AND profile = %s
                RETURNING *
                """,
                [requested_state, effective_state, requested_state, effective_state, requested_state, instrument_id, HISTORY_PROFILE],
            ).fetchone()
        return self.policy_for_symbol(normalized, profile=HISTORY_PROFILE) or dict(updated)

    def due_symbols(self, now: datetime | None = None) -> list[dict[str, Any]]:
        self.ensure_seeded()
        reference = now or datetime.now(UTC)
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT instrument.symbol, policy.*
                FROM app.option_history_policy policy
                JOIN catalog.instrument instrument ON instrument.id = policy.instrument_id
                WHERE policy.requested_state = 'on'
                  AND policy.effective_state IN ('active', 'shadow')
                  AND policy.provider = 'robinhood'
                  AND (policy.profile = %s OR (policy.profile = %s AND (policy.expires_at IS NULL OR policy.expires_at > %s)))
                ORDER BY CASE instrument.symbol WHEN 'QQQ' THEN 0 ELSE 1 END,
                         CASE policy.collection_tier WHEN 'core' THEN 0 ELSE 1 END,
                         instrument.symbol
                """
                , [HISTORY_PROFILE, EVENT_PROFILE, reference]
            ).fetchall()
        due: list[dict[str, Any]] = []
        for row in rows:
            slot = eligible_policy_slot(reference, cadence_minutes=int(row["cadence_minutes"]))
            if slot is not None:
                due.append({**_policy_payload(dict(row)), "slot_at": slot})
        return due

    def enroll_event_profile(
        self,
        *,
        event_id: Any,
        symbol: str,
        expires_at: datetime,
        reason: str,
    ) -> dict[str, Any]:
        """Add a 15-minute event strip without mutating full-history policy."""

        normalized = canonical_symbol(symbol)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            instrument_id = reconcile_instrument(
                connection, normalized, asset_class="equity", category="option-history",
            )
            row = connection.execute(
                """
                INSERT INTO app.option_history_policy
                    (instrument_id, profile, requested_state, effective_state, collection_tier,
                     cadence_minutes, publication_cap, provider, normalized_retention_days,
                     derived_retention_days, provider_payload_retention_days, policy_revision,
                     activation_reason, event_id, expires_at, reason, activated_at)
                VALUES (%s, %s, 'on', 'active', 'event', 15, 'WATCH', 'robinhood',
                        365, 730, 30, %s, %s, %s, %s, %s, now())
                ON CONFLICT (instrument_id, profile) DO UPDATE
                SET requested_state = 'on', effective_state = 'active', cadence_minutes = 15,
                    publication_cap = 'WATCH', normalized_retention_days = 365,
                    derived_retention_days = 730, provider_payload_retention_days = 30,
                    policy_revision = EXCLUDED.policy_revision,
                    activation_reason = EXCLUDED.activation_reason, event_id = EXCLUDED.event_id,
                    expires_at = EXCLUDED.expires_at, reason = EXCLUDED.reason,
                    activated_at = COALESCE(app.option_history_policy.activated_at, now()),
                    updated_at = now(), lock_version = app.option_history_policy.lock_version + 1
                RETURNING *
                """,
                [
                    instrument_id, EVENT_PROFILE, POLICY_REVISION, "selloff_event_admitted", event_id,
                    expires_at, reason,
                ],
            ).fetchone()
        return _policy_payload({"symbol": normalized, **dict(row)})

    def acquire_provider_lease(
        self,
        *,
        provider: str,
        workload: str,
        symbol: str,
        owner: str | None = None,
        now: datetime | None = None,
        ttl_seconds: int = LEASE_SECONDS,
    ) -> ProviderLease | None:
        acquired_at = now or datetime.now(UTC)
        lease_owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        normalized = canonical_symbol(symbol)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"provider-lease:{provider}"])
            connection.execute("DELETE FROM ops.provider_lease WHERE provider = %s AND expires_at <= %s", [provider, acquired_at])
            active = int(connection.execute(
                "SELECT count(*) AS count FROM ops.provider_lease WHERE provider = %s AND expires_at > %s",
                [provider, acquired_at],
            ).fetchone()["count"])
            if active >= MAX_PROVIDER_LEASES:
                return None
            if _uses_shared_history_slot(workload, normalized) and _active_radar_lease(connection, provider, acquired_at):
                return None
            row = connection.execute(
                """
                INSERT INTO ops.provider_lease (provider, workload, symbol, owner, heartbeat_at, expires_at, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, provider, workload, symbol, owner, expires_at
                """,
                [
                    provider,
                    workload,
                    normalized,
                    lease_owner,
                    acquired_at,
                    acquired_at + timedelta(seconds=ttl_seconds),
                    Jsonb({"policy_revision": POLICY_REVISION}),
                ],
            ).fetchone()
        return ProviderLease(**dict(row))

    def release_provider_lease(self, lease_id: int) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("DELETE FROM ops.provider_lease WHERE id = %s", [lease_id])

    def heartbeat_provider_lease(self, lease_id: int, *, ttl_seconds: int = LEASE_SECONDS) -> bool:
        now = datetime.now(UTC)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                UPDATE ops.provider_lease
                SET heartbeat_at = %s, expires_at = %s
                WHERE id = %s AND expires_at > %s
                RETURNING id
                """,
                [now, now + timedelta(seconds=ttl_seconds), lease_id, now],
            ).fetchone()
        return row is not None


def eligible_policy_slot(now: datetime, *, cadence_minutes: int) -> datetime | None:
    reference = now.astimezone(MARKET_TZ)
    if not is_us_market_day(reference.date()):
        return None
    open_at, close_at = market_session_bounds(reference.date())
    if reference < open_at or reference >= close_at + timedelta(minutes=15):
        return None
    if cadence_minutes == 15:
        minute = (reference.minute // 15) * 15
        local_slot = reference.replace(minute=minute, second=0, microsecond=0)
        if local_slot > close_at:
            local_slot = close_at
        return local_slot.astimezone(UTC)
    if cadence_minutes != 60:
        raise ValueError("unsupported option-history cadence")
    if reference >= close_at:
        return close_at.astimezone(UTC)
    if reference.minute < 30 or reference.minute >= 45:
        return None
    local_slot = reference.replace(minute=30, second=0, microsecond=0)
    if local_slot < open_at:
        return None
    return local_slot.astimezone(UTC)


def _uses_shared_history_slot(workload: str, symbol: str) -> bool:
    return workload in HISTORY_WORKLOADS and symbol not in CORE_HISTORY_SYMBOLS


def _active_radar_lease(connection: Any, provider: str, now: datetime) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM ops.provider_lease
        WHERE provider = %s AND expires_at > %s AND workload = ANY(%s)
        LIMIT 1
        """,
        [provider, now, sorted(RADAR_WORKLOADS)],
    ).fetchone()
    return row is not None


def apply_publication_cap(state: dict[str, Any], policy: dict[str, Any] | None) -> dict[str, Any]:
    computed = str(state.get("paper_state") or "COLLECTING")
    cap = str((policy or {}).get("publication_cap") or "PAPER_READY")
    effective = computed
    blockers = list(state.get("blockers") or [])
    reasons = list(state.get("reasons") or [])
    if cap == "WATCH" and computed == "PAPER_READY":
        effective = "WATCH"
        blockers = sorted(set([*blockers, "symbol_shadow_only"]))
        reasons = sorted(set([*reasons, "publication_cap_watch"]))
    return {**state, "computed_paper_state": computed, "paper_state": effective, "blockers": blockers, "reasons": reasons}


def _seed_policy(
    connection: Any,
    symbol: str,
    *,
    effective_state: str,
    collection_tier: str,
    cadence_minutes: int,
    publication_cap: str,
    reason: str,
) -> None:
    instrument_id = reconcile_instrument(connection, symbol, asset_class="equity", category="option-history")
    connection.execute(
        """
        INSERT INTO app.option_history_policy
            (instrument_id, profile, requested_state, effective_state, collection_tier, cadence_minutes,
             publication_cap, provider, normalized_retention_days, derived_retention_days,
             provider_payload_retention_days, policy_revision, reason, activated_at)
        VALUES (%s, %s, 'on', %s, %s, %s, %s, 'robinhood', 730, 730, 90, %s, %s,
                CASE WHEN %s IN ('active', 'shadow') THEN now() ELSE NULL END)
        ON CONFLICT (instrument_id, profile) DO NOTHING
        """,
        [instrument_id, HISTORY_PROFILE, effective_state, collection_tier, cadence_minutes, publication_cap, POLICY_REVISION, reason, effective_state],
    )


def _on_state(policy: dict[str, Any]) -> str:
    current = str(policy.get("effective_state") or "pending_gate")
    if current in {"active", "shadow", "paused"}:
        return "paused" if current == "paused" else current
    return "pending_gate"


def _policy_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "instrument_id": int(row["instrument_id"]) if row.get("instrument_id") is not None else None,
        "symbol": str(row.get("symbol") or "").upper(),
        "profile": row.get("profile") or HISTORY_PROFILE,
        "requested_state": row.get("requested_state"),
        "effective_state": row.get("effective_state"),
        "collection_tier": row.get("collection_tier"),
        "cadence_minutes": int(row["cadence_minutes"]) if row.get("cadence_minutes") is not None else None,
        "publication_cap": row.get("publication_cap"),
        "provider": row.get("provider"),
        "normalized_retention_days": int(row["normalized_retention_days"]) if row.get("normalized_retention_days") is not None else None,
        "derived_retention_days": int(row["derived_retention_days"]) if row.get("derived_retention_days") is not None else None,
        "provider_payload_retention_days": int(row["provider_payload_retention_days"]) if row.get("provider_payload_retention_days") is not None else None,
        "policy_revision": row.get("policy_revision"),
        "lock_version": int(row["lock_version"]) if row.get("lock_version") is not None else None,
        "reason": row.get("reason"),
        "activation_reason": row.get("activation_reason"),
        "event_id": str(row["event_id"]) if row.get("event_id") is not None else None,
        "expires_at": row.get("expires_at"),
        "activated_at": row.get("activated_at"),
        "paused_at": row.get("paused_at"),
        "requested_at": row.get("requested_at"),
        "updated_at": row.get("updated_at"),
        "latest_complete_capture": row.get("latest_complete_capture"),
        "latest_snapshot_id": int(row["snapshot_id"]) if row.get("snapshot_id") is not None else None,
        "complete_captures": int(row["complete_captures"]) if row.get("complete_captures") is not None else 0,
        "readiness": _readiness_label(row),
    }


def _readiness_label(row: dict[str, Any]) -> str:
    if row.get("requested_state") == "off":
        return "disabled"
    state = str(row.get("effective_state") or "")
    if state == "pending_gate":
        return "pending admission"
    if state == "shadow":
        return "shadow collection"
    if state == "active":
        return "active"
    if state == "paused":
        return "paused"
    return "disabled"
