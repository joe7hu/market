"""Lossless, content-addressed context shared by immutable ticker decisions.

The SQL read view expands both legacy inline and new referenced rows. It does
not depend on the NAS. Canonical input manifests, trade plans, outcome lineage,
and portfolio impacts stay inline and unchanged.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping

from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime, JOB_PROFILE

CONTEXT_COLUMNS = ("market_state_snapshot", "risk_policy_snapshot")


def require_maintenance_headroom(*, minimum_bytes: int) -> None:
    """Operator identifies the actual PGDATA/tablespace backing filesystem."""
    configured = os.environ.get("MARKET_STORAGE_DATABASE_PATH", "").strip()
    if not configured or not Path(configured).is_dir():
        raise ValueError("set MARKET_STORAGE_DATABASE_PATH to the mounted PostgreSQL data-volume path before maintenance")
    free = shutil.disk_usage(configured).free
    if free < minimum_bytes:
        raise RuntimeError(f"PostgreSQL maintenance requires {minimum_bytes} free bytes; measured {free}")


def context_digest(payload: Mapping[str, Any]) -> str:
    """Hash canonical JSON without dropping timestamps, nulls, or evidence."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return sha256(raw.encode("utf-8")).hexdigest()


def store_context(connection: Any, payload: Mapping[str, Any] | None) -> str | None:
    """Intern immutable JSON, never rewrite an existing TOAST value on retry."""
    if not payload:
        return None
    value = dict(payload)
    digest = context_digest(value)
    connection.execute(
        """INSERT INTO analysis.decision_context (content_hash, payload)
           VALUES (%s, %s) ON CONFLICT (content_hash) DO NOTHING""",
        [digest, Jsonb(value)],
    )
    # Check equality on conflicts too: corruption or a digest collision must
    # fail closed rather than silently substitute evidence. Return only bool.
    valid = connection.execute(
        "SELECT payload = %s::jsonb AS valid FROM analysis.decision_context WHERE content_hash = %s",
        [Jsonb(value), digest],
    ).fetchone()
    if not valid or not valid["valid"]:
        raise ValueError("decision context hash does not identify the original evidence")
    return digest


def compact_context_batch(runtime: DatabaseRuntime, *, batch_size: int = 25, execute: bool = False) -> dict[str, Any]:
    """Intern one bounded batch of old contexts in the same transaction.

    This frees reusable PostgreSQL pages after ordinary VACUUM, not necessarily
    filesystem bytes. The runbook requires headroom before any UPDATE batch.
    """
    if not 1 <= batch_size <= 100:
        raise ValueError("decision-context batch_size must be between 1 and 100")
    if execute:
        require_maintenance_headroom(minimum_bytes=2 * 1024**3)
    predicate = """(market_state_context_hash IS NULL AND market_state_snapshot <> '{}'::jsonb)
                    OR (risk_policy_context_hash IS NULL AND risk_policy_snapshot <> '{}'::jsonb)"""
    with runtime.transaction(JOB_PROFILE) if execute else runtime.read(JOB_PROFILE) as connection:
        if not execute:
            row = connection.execute(
                f"SELECT count(*) AS remaining FROM analysis.ticker_decision WHERE {predicate}"
            ).fetchone()
            return {"phase": "decision-context", "dry_run": True, "remaining": int(row["remaining"]),
                    "filesystem_reclaim": "not_until_separate_compaction"}
        rows = connection.execute(
            f"""SELECT id, market_state_snapshot, risk_policy_snapshot,
                       market_state_context_hash, risk_policy_context_hash
                FROM analysis.ticker_decision WHERE {predicate}
                ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""", [batch_size],
        ).fetchall()
        for row in rows:
            market_hash = row["market_state_context_hash"] or store_context(connection, row["market_state_snapshot"])
            policy_hash = row["risk_policy_context_hash"] or store_context(connection, row["risk_policy_snapshot"])
            connection.execute(
                """UPDATE analysis.ticker_decision
                   SET market_state_context_hash = %s, risk_policy_context_hash = %s,
                       market_state_snapshot = CASE WHEN %s::text IS NULL THEN market_state_snapshot ELSE '{}'::jsonb END,
                       risk_policy_snapshot = CASE WHEN %s::text IS NULL THEN risk_policy_snapshot ELSE '{}'::jsonb END
                   WHERE id = %s""",
                [market_hash, policy_hash, market_hash, policy_hash, row["id"]],
            )
        return {"phase": "decision-context", "dry_run": False, "compacted": len(rows),
                "status": "batch_complete" if rows else "nothing_due",
                "filesystem_reclaim": "not_until_separate_compaction"}
