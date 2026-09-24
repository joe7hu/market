"""Lossless input normalization; PostgreSQL owns numeric precision and hydration."""
from __future__ import annotations

from typing import Any, Mapping
from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.decision_storage import require_maintenance_headroom
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime, JOB_PROFILE

MAX_INPUT_BATCH_BYTES = 64 * 1024**2


def intern_input_manifest(connection: Any, manifest: Mapping[str, Any]) -> tuple[str, str]:
    row = connection.execute(
        """SELECT compact_manifest::text, input_refs::text
           FROM analysis.intern_decision_inputs(%s::jsonb)""", [Jsonb(dict(manifest))],
    ).fetchone()
    return row["compact_manifest"], row["input_refs"]


def compact_input_batch(runtime: DatabaseRuntime, *, batch_size: int = 25, execute: bool = False) -> dict[str, Any]:
    if not 1 <= batch_size <= 100:
        raise ValueError("decision-inputs batch_size must be between 1 and 100")
    if execute:
        require_maintenance_headroom(minimum_bytes=2 * 1024**3)
    with runtime.transaction(JOB_PROFILE) if execute else runtime.read(JOB_PROFILE) as connection:
        if not execute:
            row = connection.execute("SELECT count(*) AS remaining FROM analysis.ticker_decision WHERE NOT inputs_normalized").fetchone()
            return {"phase": "decision-inputs", "dry_run": True, "remaining": int(row["remaining"])}
        rows = connection.execute("""
            SELECT id, octet_length(input_manifest::text) AS bytes FROM analysis.ticker_decision
            WHERE NOT inputs_normalized ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED
        """, [batch_size]).fetchall()
        consumed = compacted = 0
        for row in rows:
            size = int(row["bytes"])
            if consumed + size > MAX_INPUT_BATCH_BYTES:
                if not compacted:
                    raise ValueError("individual decision exceeds the 64 MiB input maintenance budget")
                break
            # No Python decode/reserialize: exact PostgreSQL numerics survive.
            connection.execute("""
                WITH normalized AS MATERIALIZED (
                    SELECT n.* FROM analysis.ticker_decision d
                    CROSS JOIN LATERAL analysis.intern_decision_inputs(d.input_manifest) n
                    WHERE d.id = %s
                )
                UPDATE analysis.ticker_decision d
                SET input_manifest = n.compact_manifest, input_payload_refs = n.input_refs,
                    inputs_normalized = true
                FROM normalized n WHERE d.id = %s
            """, [row["id"], row["id"]])
            consumed += size
            compacted += 1
    return {"phase": "decision-inputs", "dry_run": False, "compacted": compacted,
            "input_bytes_processed": consumed, "filesystem_reclaim": "reusable_after_vacuum_not_OS_bytes"}
