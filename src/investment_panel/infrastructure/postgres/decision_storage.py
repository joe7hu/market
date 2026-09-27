"""Lossless, content-addressed context shared by immutable ticker decisions.

The SQL read view expands both legacy inline and new referenced rows. It does
not depend on the NAS. Expanded reads preserve exact trade terms and outcome
lineage while referenced evidence has one local owner.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime, JOB_PROFILE

CONTEXT_COLUMNS = ("market_state_snapshot", "risk_policy_snapshot")
MAX_CONTEXT_BATCH_BYTES = 64 * 1024**2
MAX_EVIDENCE_BATCH_BYTES = 64 * 1024**2


def lock_decision_evidence_writer(connection: Any) -> None:
    connection.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
                       ["market-decision-evidence-gc"])


def ranking_publication_reference(*values: Any) -> UUID | None:
    """Resolve one exact plan/rank publication identity, including legacy local IDs."""
    found: set[UUID | str] = set()
    for value in values:
        if not value:
            continue
        raw = str(value)
        try:
            found.add(UUID(raw))
        except ValueError:
            found.add(raw)
    if len(found) > 1:
        raise ValueError("plan and rank publication references conflict")
    only = next(iter(found), None)
    return only if isinstance(only, UUID) else None


def backfill_ranking_publication_refs(
    runtime: DatabaseRuntime, *, batch_size: int = 25, execute: bool = False,
) -> dict[str, Any]:
    """Index exact legacy plan references before ranking publication retention."""
    if not 1 <= batch_size <= 100:
        raise ValueError("ranking reference batch_size must be between 1 and 100")
    with runtime.transaction(JOB_PROFILE) if execute else runtime.read(JOB_PROFILE) as connection:
        if not execute:
            remaining = connection.execute(
                "SELECT count(*) AS n FROM analysis.ticker_decision WHERE NOT ranking_ref_checked"
            ).fetchone()["n"]
            return {"phase": "ranking-refs", "dry_run": True, "remaining": int(remaining)}
        candidates = connection.execute("""SELECT id FROM analysis.ticker_decision
            WHERE NOT ranking_ref_checked ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""",
            [batch_size]).fetchall()
        consumed = checked = 0
        for candidate in candidates:
            decision_id = candidate["id"]
            before = connection.execute("""SELECT expanded.input_manifest::text AS manifest,
                expanded.input_manifest->'trade_plan'->>'publication_id' AS plan_publication_id,
                expanded.input_manifest->'opportunity_rank'->>'ranking_publication_id' AS rank_publication_id,
                expanded.input_manifest->'opportunity_rank'->>'publication_id' AS rank_alias_id,
                decision.ranking_publication_id AS indexed_id
                FROM analysis.ticker_decision_read expanded
                JOIN analysis.ticker_decision decision ON decision.id = expanded.id
                WHERE expanded.id = %s""", [decision_id]).fetchone()
            size = len(before["manifest"].encode("utf-8"))
            if consumed + size > MAX_EVIDENCE_BATCH_BYTES:
                if not checked:
                    raise ValueError("individual ranking reference exceeds the 64 MiB maintenance budget")
                break
            publication_id = ranking_publication_reference(
                before["plan_publication_id"], before["rank_publication_id"],
                before["rank_alias_id"],
            )
            if before["indexed_id"] is not None and before["indexed_id"] != publication_id:
                raise ValueError("ranking publication reference conflicts with exact legacy plan; batch rolled back")
            if publication_id is not None and connection.execute(
                "SELECT 1 FROM app.publication WHERE id = %s AND scope = 'ticker-opportunity-ranking'",
                [publication_id],
            ).fetchone() is None:
                raise ValueError(f"ranking publication {publication_id} is missing or has the wrong scope; batch rolled back")
            connection.execute("""UPDATE analysis.ticker_decision
                SET ranking_publication_id = %s, ranking_ref_checked = true WHERE id = %s""",
                [publication_id, decision_id])
            after = connection.execute("SELECT input_manifest::text AS manifest FROM analysis.ticker_decision_read WHERE id = %s",
                                       [decision_id]).fetchone()
            if before["manifest"] != after["manifest"]:
                raise ValueError("ranking reference changed decision read; batch rolled back")
            consumed += size
            checked += 1
    return {"phase": "ranking-refs", "dry_run": False, "checked": checked,
            "input_bytes_processed": consumed}


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
        lock_decision_evidence_writer(connection)
        candidates = connection.execute(
            f"""SELECT id,
                       COALESCE(octet_length(market_state_snapshot::text), 0)
                         + COALESCE(octet_length(risk_policy_snapshot::text), 0) AS bytes
                FROM analysis.ticker_decision WHERE {predicate}
                ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""", [batch_size],
        ).fetchall()
        selected, total_bytes = [], 0
        for candidate in candidates:
            size = int(candidate["bytes"])
            if total_bytes + size > MAX_CONTEXT_BATCH_BYTES:
                if not selected:
                    raise ValueError("individual decision context exceeds the 64 MiB maintenance budget")
                break
            selected.append(candidate["id"])
            total_bytes += size
        rows = connection.execute(
            """SELECT id, market_state_snapshot, risk_policy_snapshot,
                      market_state_context_hash, risk_policy_context_hash
               FROM analysis.ticker_decision WHERE id = ANY(%s) ORDER BY id""", [selected],
        ).fetchall() if selected else []
        for row in rows:
            market_hash = row["market_state_context_hash"] or store_context(connection, row["market_state_snapshot"])
            policy_hash = row["risk_policy_context_hash"] or store_context(connection, row["risk_policy_snapshot"])
            updated = connection.execute(
                """UPDATE analysis.ticker_decision
                   SET market_state_context_hash = %s, risk_policy_context_hash = %s,
                       market_state_snapshot = CASE WHEN %s::text IS NULL THEN market_state_snapshot ELSE '{}'::jsonb END,
                       risk_policy_snapshot = CASE WHEN %s::text IS NULL THEN risk_policy_snapshot ELSE '{}'::jsonb END
                   WHERE id = %s
                     AND ((market_state_context_hash IS NOT NULL AND market_state_snapshot = '{}'::jsonb)
                          OR %s::text IS NULL OR market_state_snapshot = (
                         SELECT payload FROM analysis.decision_context WHERE content_hash = %s))
                     AND ((risk_policy_context_hash IS NOT NULL AND risk_policy_snapshot = '{}'::jsonb)
                          OR %s::text IS NULL OR risk_policy_snapshot = (
                         SELECT payload FROM analysis.decision_context WHERE content_hash = %s))
                   RETURNING id""",
                [market_hash, policy_hash, market_hash, policy_hash, row["id"],
                 market_hash, market_hash, policy_hash, policy_hash],
            ).fetchone()
            if updated is None:
                raise ValueError("context normalization changed original JSON values; batch rolled back")
        return {"phase": "decision-context", "dry_run": False, "compacted": len(rows),
                "status": "batch_complete" if rows else "nothing_due",
                "filesystem_reclaim": "not_until_separate_compaction"}


def compact_decision_evidence_batch(
    runtime: DatabaseRuntime, *, batch_size: int = 25, execute: bool = False,
) -> dict[str, Any]:
    """Normalize one bounded legacy batch and compare expanded PostgreSQL JSON text."""
    if not 1 <= batch_size <= 100:
        raise ValueError("decision-evidence batch_size must be between 1 and 100")
    if execute:
        require_maintenance_headroom(minimum_bytes=2 * 1024**3)
    with runtime.transaction(JOB_PROFILE) if execute else runtime.read(JOB_PROFILE) as connection:
        if not execute:
            remaining = connection.execute(
                "SELECT count(*) AS n FROM analysis.ticker_decision WHERE NOT evidence_normalized"
            ).fetchone()["n"]
            return {"phase": "decision-evidence", "dry_run": True, "remaining": int(remaining)}
        lock_decision_evidence_writer(connection)
        candidates = connection.execute("""
            SELECT id, octet_length(input_manifest::text) + octet_length(resolution::text)
                 + octet_length(capital_action::text)
                 + octet_length(expressions::text)
                 + COALESCE(octet_length(selected_expression::text), 0)
                 + octet_length(opportunity_episode::text) + octet_length(portfolio_impacts::text) AS bytes
            FROM analysis.ticker_decision WHERE NOT evidence_normalized
            ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED
        """, [batch_size]).fetchall()
        consumed = compacted = 0
        projection = """SELECT input_manifest::text, resolution::text, capital_action::text, expressions::text,
            selected_expression::text, opportunity_episode::text, portfolio_impacts::text
            FROM analysis.ticker_decision_read WHERE id = %s"""
        for candidate in candidates:
            size = int(candidate["bytes"])
            if consumed + size > MAX_EVIDENCE_BATCH_BYTES:
                if not compacted:
                    raise ValueError("individual decision exceeds the 64 MiB evidence budget")
                break
            decision_id = candidate["id"]
            before = connection.execute(projection, [decision_id]).fetchone()
            normalized = connection.execute("""
                SELECT n.compact_manifest::text AS manifest,
                       n.compact_resolution::text AS resolution,
                       n.compact_capital::text AS capital,
                       n.compact_expressions::text AS expressions,
                       n.compact_selected::text AS selected,
                       n.compact_impacts::text AS impacts,
                       n.refs::text AS refs
                FROM analysis.ticker_decision d
                CROSS JOIN LATERAL analysis.intern_decision_evidence(
                    d.input_manifest, d.resolution, d.capital_action, d.expressions,
                    d.selected_expression, d.opportunity_episode, d.portfolio_impacts) n
                WHERE d.id = %s
            """, [decision_id]).fetchone()
            connection.execute("""
                UPDATE analysis.ticker_decision SET
                  input_manifest = %s::jsonb, resolution = %s::jsonb, capital_action = %s::jsonb,
                  expressions = %s::jsonb, selected_expression = %s::jsonb,
                  portfolio_impacts = %s::jsonb, evidence_refs = %s::jsonb,
                  evidence_normalized = true
                WHERE id = %s
            """, [normalized["manifest"], normalized["resolution"], normalized["capital"], normalized["expressions"],
                  normalized["selected"], normalized["impacts"], normalized["refs"], decision_id])
            after = connection.execute(projection, [decision_id]).fetchone()
            if before != after:
                raise ValueError("decision evidence read changed; batch rolled back")
            consumed += size
            compacted += 1
    return {"phase": "decision-evidence", "dry_run": False, "compacted": compacted,
            "input_bytes_processed": consumed,
            "filesystem_reclaim": "reusable_after_vacuum_not_OS_bytes"}
