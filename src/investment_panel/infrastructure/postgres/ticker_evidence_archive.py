"""Verified cold storage for completed ticker source inputs.

The decision identity, plan, forecast, outcomes and compact results stay local.
Only the frozen input groups move after their exact typed rows have restored
successfully in scratch PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import gzip
import json
import os
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.decision_storage import require_maintenance_headroom
from investment_panel.infrastructure.postgres.hot_retention import MAINTENANCE_PROFILE
from investment_panel.infrastructure.postgres.row_archive import RowArchive
from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService
from investment_panel.infrastructure.postgres.ticker_decisions import _decision_from_row, semantic_decision_fingerprint


CHECKPOINT = "ticker-evidence-v1"
OUTCOME_KEYS = frozenset((horizon, sessions) for horizon, values in (
    ("TACTICAL", (1, 5, 20)), ("FUNDAMENTAL", (63, 126, 252)),
) for sessions in values)
RELATIONS = (
    "catalog.instrument", "analysis.decision_context", "analysis.strategy_forecast",
    "analysis.decision_input_payload", "analysis.ticker_decision",
)


class TickerEvidenceArchive:
    def __init__(self, service: StorageArchiveService) -> None:
        self.service = service

    def run(self, *, batch_size: int = 10, now: datetime | None = None,
            execute: bool = False, backup_token: str | None = None) -> dict[str, Any]:
        if not 1 <= batch_size <= 10:
            raise ValueError("ticker evidence batch_size must be 1..10")
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("ticker evidence cutoff must be timezone-aware")
        if execute:
            backup = self.service._require_verified_backup(backup_token)
            created_at = datetime.fromisoformat(str(backup.get("created_at") or ""))
            if (created_at.tzinfo is None or created_at > datetime.now(UTC)
                or created_at < datetime.now(UTC) - timedelta(days=1)
                or backup.get("format") != "postgresql-custom"):
                raise ValueError("ticker evidence compaction requires a fresh verified PostgreSQL backup")
            require_maintenance_headroom(minimum_bytes=2 * 1024**3)
        with self.service.runtime.job_lock(CHECKPOINT) as acquired:
            if not acquired:
                return {"phase": CHECKPOINT, "status": "already_running"}
            context = (self.service.runtime.transaction(MAINTENANCE_PROFILE) if execute
                       else self.service.runtime.read(MAINTENANCE_PROFILE))
            with context as connection:
                lock = "FOR SHARE OF decision SKIP LOCKED" if execute else ""
                rows = connection.execute(f"""
                    SELECT decision.id, decision.instrument_id, decision.input_payload_refs,
                           decision.evidence_refs, decision.input_manifest,
                           decision.market_state_context_hash, decision.risk_policy_context_hash
                    FROM analysis.ticker_decision decision
                    WHERE decision.as_of < %s AND decision.evidence_state = 'local'
                      AND decision.inputs_normalized AND decision.evidence_normalized
                      AND decision.ranking_ref_checked
                      AND decision.status IN ('published', 'superseded')
                      AND EXISTS (SELECT 1 FROM analysis.ticker_decision newer
                                  WHERE newer.instrument_id = decision.instrument_id
                                    AND newer.as_of > decision.as_of
                                    AND newer.status = 'published')
                      AND NOT EXISTS (SELECT 1 FROM app.paper_order paper
                                      WHERE paper.ticker_decision_id = decision.id
                                        AND paper.status NOT IN
                                          ('closed', 'cancelled', 'rejected', 'exited', 'invalidated'))
                      AND NOT EXISTS (SELECT 1 FROM analysis.ticker_outcome outcome
                                      WHERE outcome.ticker_decision_id = decision.id
                                        AND outcome.state = 'observing')
                      AND (SELECT count(*) FROM analysis.ticker_outcome outcome
                           WHERE outcome.ticker_decision_id = decision.id
                             AND outcome.state = 'resolved'
                             AND (outcome.horizon, outcome.horizon_sessions) IN
                               (('TACTICAL', 1), ('TACTICAL', 5), ('TACTICAL', 20),
                                ('FUNDAMENTAL', 63), ('FUNDAMENTAL', 126),
                                ('FUNDAMENTAL', 252))) = 6
                      AND NOT EXISTS (SELECT 1 FROM analysis.ticker_data_request request
                                      WHERE request.ticker_decision_id = decision.id
                                        AND request.status IN ('open', 'running'))
                    ORDER BY decision.as_of, decision.id LIMIT %s {lock}
                """, [reference - timedelta(days=30), batch_size]).fetchall()
                if not rows:
                    return {"phase": CHECKPOINT, "status": "pass_complete",
                            "archived": 0, "dry_run": not execute}
                if not execute:
                    return {"phase": CHECKPOINT, "status": "eligible",
                            "eligible": len(rows), "dry_run": True}
                # The row lock conflicts with new FK references. Check active
                # work again after waiting for locks and before any source edit.
                selected = [row["id"] for row in rows]
                outcomes = connection.execute("""SELECT id, ticker_decision_id, horizon,
                       horizon_sessions, state
                    FROM analysis.ticker_outcome WHERE ticker_decision_id = ANY(%s)
                    FOR UPDATE SKIP LOCKED""", [selected]).fetchall()
                if any({(item["horizon"], item["horizon_sessions"])
                        for item in outcomes if item["ticker_decision_id"] == decision_id
                        and item["state"] == "resolved"} != OUTCOME_KEYS for decision_id in selected):
                    return {"phase": CHECKPOINT, "status": "protected", "archived": 0,
                            "reason": "ticker_outcomes_locked_or_incomplete", "dry_run": False}
                upgraded = connection.execute("""SELECT id FROM analysis.ticker_decision
                    WHERE id = ANY(%s) FOR UPDATE SKIP LOCKED""", [selected]).fetchall()
                if len(upgraded) != len(selected):
                    return {"phase": CHECKPOINT, "status": "protected", "archived": 0,
                            "reason": "ticker_consumer_lock", "dry_run": False}
                if connection.execute("""SELECT EXISTS (
                    SELECT 1 FROM app.paper_order WHERE ticker_decision_id = ANY(%s)
                      AND status NOT IN ('closed', 'cancelled', 'rejected', 'exited', 'invalidated')
                    UNION ALL SELECT 1 FROM analysis.ticker_outcome
                      WHERE ticker_decision_id = ANY(%s) AND state = 'observing'
                    UNION ALL SELECT 1 FROM analysis.ticker_data_request
                      WHERE ticker_decision_id = ANY(%s) AND status IN ('open', 'running')
                ) AS active""", [selected, selected, selected]).fetchone()["active"]:
                    raise ValueError("ticker evidence gained unfinished work; source retained")
                for decision_id in selected:
                    row = connection.execute("""
                        SELECT instrument.symbol AS ticker, decision.*
                        FROM analysis.ticker_decision_read decision
                        JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                        WHERE decision.id = %s AND decision.semantic_fingerprint IS NULL
                    """, [decision_id]).fetchone()
                    if row is None:
                        continue
                    try:
                        fingerprint = semantic_decision_fingerprint(_decision_from_row(row))
                    except (TypeError, ValueError, KeyError):
                        continue
                    connection.execute("""
                        UPDATE analysis.ticker_decision SET semantic_fingerprint = %s
                        WHERE id = %s AND semantic_fingerprint IS NULL
                    """, [fingerprint, decision_id])
                packs, hashes, contexts = self._dependencies(connection, rows)
                receipt = {"contract": "ticker-evidence.v1",
                           "decision_ids": [str(value) for value in selected],
                           "source_versions": {str(row["id"]): (row["input_manifest"] or {}).get("source_versions", {})
                                               for row in rows},
                           "dependencies": packs}
                artifact = self.service._write_json_gzip(
                    "derived", receipt, source_relation="analysis.ticker_decision",
                    row_count=1, metadata={"archive_contract": receipt["contract"],
                                           "source_row_ids": receipt["decision_ids"],
                                           "dependency_manifests": packs},
                )
                manifest_id = int(artifact["manifest_id"])
                if self.service.verify(manifest_id=manifest_id)["verified"] != 1:
                    raise ValueError("ticker archive receipt failed verification; source retained")
                self.restore_decision(selected[0], manifest_id=manifest_id, keep=False, verify_all=True)
                updated = connection.execute("""UPDATE analysis.ticker_decision decision
                    SET archived_input_refs = input_payload_refs,
                        input_payload_refs = '{}'::jsonb,
                        input_manifest = jsonb_set(input_manifest, '{inputs}', '{}'::jsonb, true),
                        archived_context_refs = jsonb_strip_nulls(jsonb_build_object(
                            'market', market_state_context_hash, 'policy', risk_policy_context_hash)),
                        market_state_snapshot = analysis.compact_market_snapshot(
                          CASE WHEN decision.market_state_context_hash IS NULL
                            THEN decision.market_state_snapshot ELSE (
                              SELECT context.payload FROM analysis.decision_context context
                              WHERE context.content_hash = decision.market_state_context_hash)
                            END),
                        risk_policy_snapshot = analysis.compact_risk_policy_snapshot(
                          CASE WHEN decision.risk_policy_context_hash IS NULL
                            THEN decision.risk_policy_snapshot ELSE (
                              SELECT context.payload FROM analysis.decision_context context
                              WHERE context.content_hash = decision.risk_policy_context_hash)
                            END),
                        market_state_context_hash = NULL, risk_policy_context_hash = NULL,
                        evidence_state = 'archived', evidence_archive_manifest_id = %s
                    WHERE id = ANY(%s) AND evidence_state = 'local'""",
                    [manifest_id, selected]).rowcount
                if updated != len(selected):
                    raise ValueError("ticker archive source changed; transaction rolled back")
                tail = rows[-1]
                connection.execute("""INSERT INTO ops.storage_archive_checkpoint
                    (checkpoint_key, archive_kind, source_relation, cursor, run_status, counts, updated_at)
                    VALUES (%s, 'derived', 'analysis.ticker_decision', %s, 'paused', %s, now())
                    ON CONFLICT (checkpoint_key) DO UPDATE SET cursor = EXCLUDED.cursor,
                      run_status = EXCLUDED.run_status, counts = EXCLUDED.counts,
                      updated_at = EXCLUDED.updated_at""",
                    [CHECKPOINT, Jsonb({"decision_id": str(tail["id"])}),
                     Jsonb({"archived": updated, "payloads_pending_gc": len(hashes),
                            "contexts_pending_gc": len(contexts),
                            "manifest_id": manifest_id})])
                return {"phase": CHECKPOINT, "status": "batch_complete",
                        "archived": updated, "payloads_pending_gc": len(hashes),
                        "contexts_pending_gc": len(contexts),
                        "manifest_id": manifest_id, "dry_run": False}

    def collect_for_decision(self, decision_id: Any, *, execute: bool = False,
                             backup_token: str | None = None) -> dict[str, Any]:
        """Collect one archived owner's shared rows after writers have left the barrier."""
        selected = str(UUID(str(decision_id)))
        with self.service.runtime.read(MAINTENANCE_PROFILE) as connection:
            owner = connection.execute("""SELECT evidence_state, evidence_archive_manifest_id,
                       archived_input_refs, archived_context_refs
                FROM analysis.ticker_decision WHERE id = %s""", [selected]).fetchone()
        if owner is None or owner["evidence_state"] != "archived":
            raise ValueError("ticker shared evidence collection requires an archived decision")
        hashes = sorted(set((owner["archived_input_refs"] or {}).values()))
        contexts = sorted(set((owner["archived_context_refs"] or {}).values()))
        result = {"decision_id": selected, "payload_candidates": len(hashes),
                  "context_candidates": len(contexts), "dry_run": not execute}
        if not execute:
            return result
        if not hashes and not contexts:
            return {**result, "dry_run": False, "payloads_released": 0,
                    "contexts_released": 0}
        backup = self.service._require_verified_backup(backup_token)
        created_at = datetime.fromisoformat(str(backup.get("created_at") or ""))
        if (created_at.tzinfo is None or created_at > datetime.now(UTC)
            or created_at < datetime.now(UTC) - timedelta(days=1)
            or backup.get("format") != "postgresql-custom"):
            raise ValueError("ticker shared evidence collection requires a fresh verified PostgreSQL backup")
        require_maintenance_headroom(minimum_bytes=2 * 1024**3)
        with self.service.runtime.read(MAINTENANCE_PROFILE) as connection:
            owners = connection.execute("""SELECT DISTINCT decision.id, decision.evidence_archive_manifest_id
                FROM analysis.ticker_decision decision
                WHERE decision.evidence_state = 'archived'
                  AND (EXISTS (SELECT 1 FROM jsonb_each_text(decision.archived_input_refs) ref
                               WHERE ref.value = ANY(%s))
                       OR decision.archived_context_refs->>'market' = ANY(%s)
                       OR decision.archived_context_refs->>'policy' = ANY(%s))
                ORDER BY decision.id""", [hashes, contexts, contexts]).fetchall()
        if not owners:
            raise ValueError("ticker shared evidence collection has no archive owners")
        for archived_owner in owners:
            self.restore_decision(archived_owner["id"],
                                  manifest_id=int(archived_owner["evidence_archive_manifest_id"]),
                                  keep=False)
        with self.service.runtime.transaction(MAINTENANCE_PROFILE) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                               ["market-decision-evidence-gc"])
            removed = connection.execute(
                "SELECT analysis.gc_archived_decision_payloads(%s::text[]) AS removed", [hashes],
            ).fetchone()["removed"]
            released_contexts = connection.execute(
                "SELECT analysis.gc_archived_decision_contexts(%s::text[]) AS removed", [contexts],
            ).fetchone()["removed"]
        return {**result, "dry_run": False, "payloads_released": removed,
                "contexts_released": released_contexts}

    def _dependencies(self, connection: Any, rows: list[Any]) -> tuple[dict[str, list[int]], list[str], list[str]]:
        ids = [row["id"] for row in rows]
        instruments = sorted({row["instrument_id"] for row in rows})
        contexts = sorted({value for row in rows for value in (
            row["market_state_context_hash"], row["risk_policy_context_hash"])
                           if value is not None})
        hashes = sorted({value for row in rows for value in (row["input_payload_refs"] or {}).values()}
                        | {value for row in rows for value in _evidence_hashes(row["evidence_refs"] or {})})
        manifest = connection.execute("""SELECT input_manifest FROM analysis.ticker_decision_read
            WHERE id = ANY(%s)""", [ids]).fetchall()
        forecast_ids = sorted({str(value) for item in manifest for value in _forecast_ids(item["input_manifest"])
                               if value})
        predicates: dict[str, tuple[str, list[Any], int]] = {
            "catalog.instrument": ("id = ANY(%s)", [instruments], len(instruments)),
            "analysis.decision_context": ("content_hash = ANY(%s)", [contexts], len(contexts)),
            "analysis.strategy_forecast": ("id = ANY(%s)", [forecast_ids], len(forecast_ids)),
            "analysis.decision_input_payload": ("content_hash = ANY(%s)", [hashes], len(hashes)),
            "analysis.ticker_decision": ("id = ANY(%s)", [ids], len(ids)),
        }
        packs: dict[str, list[int]] = {}
        for relation in RELATIONS:
            predicate, params, expected = predicates[relation]
            records = connection.execute(
                f"SELECT to_jsonb(source)::text AS row_json FROM {relation} source "
                f"WHERE {predicate} ORDER BY 1", params,
            ).fetchall()
            if len(records) != expected:
                raise ValueError(f"ticker archive dependency incomplete: {relation}; source retained")
            # Per-row content addresses prevent repeated copies of a shared
            # payload or context in later decision receipts.
            packs[relation] = [pack for record in records
                               for pack in RowArchive(self.service).write(connection, relation, [record])]
        return packs, hashes, contexts

    def full_evidence(self, decision_id: Any) -> dict[str, Any]:
        selected = str(UUID(str(decision_id)))
        with self.service.runtime.read(MAINTENANCE_PROFILE) as connection:
            state = connection.execute("""SELECT evidence_state, evidence_archive_manifest_id,
                       market_state_context_hash, risk_policy_context_hash
                FROM analysis.ticker_decision WHERE id = %s""", [selected]).fetchone()
            if state is None:
                return {"status": "unavailable", "reason": "decision_missing"}
            if state["evidence_state"] != "local":
                return {"status": state["evidence_state"],
                        "archive_manifest_id": state["evidence_archive_manifest_id"]}
            try:
                row = connection.execute("SELECT * FROM analysis.ticker_decision_read WHERE id = %s",
                                         [selected]).fetchone()
            except psycopg.errors.RaiseException:
                return {"status": "unavailable", "reason": "local_dependency_missing"}
            if row is None:
                return {"status": "unavailable", "reason": "decision_read_missing"}
            if ((state["market_state_context_hash"] and row["market_state_snapshot"] is None)
                or (state["risk_policy_context_hash"] and row["risk_policy_snapshot"] is None)):
                return {"status": "unavailable", "reason": "local_dependency_missing"}
            return {"status": "complete", "decision": dict(row)}

    def restore_decision(self, decision_id: Any, *, manifest_id: int | None = None,
                         destination_dsn: str | None = None, keep: bool = True,
                         verify_all: bool = False) -> dict[str, Any]:
        selected = str(UUID(str(decision_id)))
        with self.service.runtime.read(MAINTENANCE_PROFILE) as connection:
            if manifest_id is None:
                owner = connection.execute("""SELECT evidence_archive_manifest_id FROM analysis.ticker_decision
                    WHERE id = %s AND evidence_state = 'archived'""", [selected]).fetchone()
                if owner is None or owner["evidence_archive_manifest_id"] is None:
                    raise ValueError("selected ticker decision has no archive reference")
                manifest_id = int(owner["evidence_archive_manifest_id"])
            root = connection.execute("SELECT nas_uri, format FROM ops.storage_archive_manifest WHERE id = %s",
                                      [manifest_id]).fetchone()
        if root is None or root["format"] != "json.gz" or self.service.verify(manifest_id=manifest_id)["verified"] != 1:
            raise ValueError("ticker archive receipt is missing or corrupt")
        with gzip.open(root["nas_uri"], "rt") as handle:
            receipt = json.load(handle)
        if receipt.get("contract") != "ticker-evidence.v1" or selected not in receipt.get("decision_ids", []):
            raise ValueError("selected ticker decision is not covered by the archive")
        dependencies = receipt.get("dependencies") or {}
        if set(dependencies) != set(RELATIONS):
            raise ValueError("ticker archive dependency list is incomplete")
        included = set(receipt["decision_ids"]) if verify_all else {selected}
        schema = "market_restore_" + uuid4().hex[:16]
        target_dsn = destination_dsn or os.environ.get("MARKET_STORAGE_RESTORE_DATABASE_URL")
        if keep and not target_dsn:
            raise ValueError("set MARKET_STORAGE_RESTORE_DATABASE_URL to a migrated staging PostgreSQL database")
        target = (psycopg.connect(target_dsn, row_factory=dict_row) if keep
                  else self.service.runtime.transaction(MAINTENANCE_PROFILE))
        counts: dict[str, int] = {}
        with target as connection:
            if keep:
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            for relation in RELATIONS:
                packs = dependencies[relation]
                if not packs:
                    counts[relation] = 0
                    continue
                table = sql.Identifier(schema, relation.replace(".", "_")) if keep else sql.Identifier(
                    schema + "_" + relation.replace(".", "_"))
                connection.execute(sql.SQL("CREATE {}TABLE {} (LIKE {} INCLUDING CONSTRAINTS INCLUDING INDEXES) {}").format(
                    sql.SQL("") if keep else sql.SQL("TEMP "), table,
                    sql.Identifier(*relation.split(".")),
                    sql.SQL("") if keep else sql.SQL("ON COMMIT DROP")))
                count = 0
                for pack_id in packs:
                    if self.service.verify(manifest_id=int(pack_id))["verified"] != 1:
                        raise ValueError("ticker archive dependency is corrupt")
                    with self.service.runtime.read(MAINTENANCE_PROFILE) as source:
                        pack = source.execute("SELECT nas_uri, format FROM ops.storage_archive_manifest WHERE id = %s",
                                              [pack_id]).fetchone()
                    if pack is None or pack["format"] not in {"json.gz", "postgres-copy-text-gzip.v1"}:
                        raise ValueError("ticker archive dependency format is unsupported")
                    if pack["format"] == "json.gz":
                        with gzip.open(pack["nas_uri"], "rt") as handle:
                            entries = json.load(handle)
                    else:
                        envelope = sql.Identifier("ticker_archive_copy_" + uuid4().hex)
                        connection.execute(sql.SQL("""CREATE TEMP TABLE {} (
                            relation text, source_columns jsonb, source_database jsonb,
                            row_json text) ON COMMIT DROP""").format(envelope))
                        with gzip.open(pack["nas_uri"], "rb") as handle, connection.cursor() as cursor:
                            with cursor.copy(sql.SQL("COPY {} FROM STDIN WITH (FORMAT text)").format(envelope)) as copy:
                                for block in iter(lambda: handle.read(1024**2), b""):
                                    copy.write(block)
                        entries = connection.execute(sql.SQL(
                            "SELECT relation, row_json FROM {}"
                        ).format(envelope)).fetchall()
                        if len(entries) != 1:
                            raise ValueError("ticker COPY dependency row count mismatch")
                        connection.execute(sql.SQL("DROP TABLE {}").format(envelope))
                    for entry in entries:
                        if entry.get("relation") != relation:
                            raise ValueError("ticker archive relation mismatch")
                        raw = entry["row_json"]
                        if relation == "analysis.ticker_decision" and str(json.loads(raw).get("id")) not in included:
                            continue
                        restored = connection.execute(sql.SQL("""INSERT INTO {} AS restored
                            SELECT (jsonb_populate_record(NULL::{}, %s::jsonb)).*
                            ON CONFLICT DO NOTHING RETURNING to_jsonb(restored) = %s::jsonb AS same""").format(
                                table, sql.Identifier(*relation.split("."))), [raw, raw]).fetchone()
                        if restored is None:
                            identical = connection.execute(sql.SQL(
                                "SELECT count(*) = 1 AS same FROM {} existing WHERE to_jsonb(existing) = %s::jsonb"
                            ).format(table), [raw]).fetchone()
                            if identical is None or identical["same"] is not True:
                                raise ValueError("ticker archived dependency conflicts with staged row")
                        elif restored["same"] is not True:
                            raise ValueError("ticker typed restoration changed original values")
                        else:
                            count += 1
                counts[relation] = count
            if counts.get("analysis.ticker_decision") != len(included):
                raise ValueError("ticker archive lacks the selected decision")
        return {"decision_id": selected, "manifest_id": manifest_id,
                "staging_schema": schema if keep else None, "typed_rows": counts,
                "status": "restored" if keep else "verified"}


def _evidence_hashes(refs: dict[str, Any]) -> set[str]:
    values = {str(refs[key]) for key in ("plan_impact", "resolution_impact") if refs.get(key)}
    for key in ("manifest", "portfolio_impacts"):
        values.update(str(value) for value in (refs.get(key) or {}).values())
    return values


def _forecast_ids(manifest: dict[str, Any]) -> set[str]:
    ids = {str((manifest.get("trade_plan") or {}).get("strategy_forecast_id") or "")}
    ids.add(str((manifest.get("opportunity_rank") or {}).get("strategy_forecast_id") or ""))
    ids.update(str(signal.get("strategy_forecast_id") or "")
               for signal in manifest.get("alpha_signals") or [] if isinstance(signal, dict))
    return ids - {""}
