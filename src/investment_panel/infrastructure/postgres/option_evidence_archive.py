"""Verified 30-day archive for completed option-scan detail and its typed dependencies."""

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


CHECKPOINT = "option-scan-evidence-v1"
RELATIONS = (
    "ingest.source", "ingest.run", "ingest.payload", "catalog.instrument",
    "analysis.hypothesis", "analysis.experiment_family", "analysis.strategy_revision",
    "analysis.run", "raw.option_snapshot", "raw.option_capture_generation", "catalog.option_contract",
    "raw.option_quote", "analysis.option_relative_value",
    "analysis.option_relative_value_verification", "analysis.option_feature",
    "analysis.decision", "analysis.option_decision", "analysis.decision_evidence",
)
_SUMMARY_KEYS = (
    "probability_semantics", "scenario_count", "conservative_expected_value",
    "optimistic_expected_value", "lower_95_expected_value", "assignment_policy",
    "catalyst", "invalidation", "reassessment_date", "thesis", "calibration",
)
_SCENARIO_KEYS = ("lower_95_expected_value", "probability_profit", "scenario_count")
_QUOTE_KEYS = ("max_quote_age_seconds", "interleg_skew_seconds", "liquidity")


class OptionEvidenceArchive:
    def __init__(self, service: StorageArchiveService) -> None:
        self.service = service

    def run(self, *, batch_size: int = 10, now: datetime | None = None,
            execute: bool = False, backup_token: str | None = None) -> dict[str, Any]:
        if not 1 <= batch_size <= 25:
            raise ValueError("option evidence batch_size must be 1..25")
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("option evidence cutoff must be timezone-aware")
        if execute:
            backup = self.service._require_verified_backup(backup_token)
            created_at = datetime.fromisoformat(str(backup.get("created_at") or ""))
            if (created_at.tzinfo is None or created_at > datetime.now(UTC)
                or created_at < datetime.now(UTC) - timedelta(days=1)
                or backup.get("format") != "postgresql-custom"):
                raise ValueError("option evidence compaction requires a fresh verified PostgreSQL backup")
            require_maintenance_headroom(minimum_bytes=2 * 1024**3)
        with self.service.runtime.job_lock(CHECKPOINT) as acquired:
            if not acquired:
                return {"phase": CHECKPOINT, "status": "already_running"}
            with (self.service.runtime.transaction(MAINTENANCE_PROFILE) if execute
                  else self.service.runtime.read(MAINTENANCE_PROFILE)) as connection:
                saved = connection.execute(
                    "SELECT cursor FROM ops.storage_archive_checkpoint WHERE checkpoint_key = %s",
                    [CHECKPOINT],
                ).fetchone()
                cursor = dict(saved["cursor"] or {}) if saved else {}
                lock = "FOR UPDATE OF decision, scan SKIP LOCKED" if execute else ""
                rows = connection.execute(f"""
                    SELECT scan.decision_id, decision.as_of, decision.run_id,
                           scan.snapshot_id, scan.contract_id, scan.quote_observed_at,
                           scan.relative_value_id, scan.synthetic_legs
                    FROM analysis.decision decision
                    JOIN analysis.option_decision scan ON scan.decision_id = decision.id
                    JOIN analysis.run run ON run.id = decision.run_id
                    WHERE decision.kind = 'option' AND decision.as_of < %s
                      AND scan.evidence_state = 'local' AND run.status = 'succeeded'
                      AND scan.thesis_id IS NULL AND scan.primary_decision_id IS NULL
                      AND NOT EXISTS (SELECT 1 FROM analysis.option_decision dependent
                                      WHERE dependent.primary_decision_id = decision.id
                                        AND dependent.evidence_state = 'local')
                      AND (decision.as_of, decision.id) > (%s, %s::uuid)
                      AND EXISTS (SELECT 1 FROM analysis.run newer
                                  JOIN analysis.decision successor
                                    ON successor.run_id = newer.id
                                   AND successor.kind = decision.kind
                                   AND successor.instrument_id = decision.instrument_id
                                   AND successor.lane IS NOT DISTINCT FROM decision.lane
                                  JOIN analysis.option_decision successor_scan
                                    ON successor_scan.decision_id = successor.id
                                  WHERE newer.run_type = run.run_type
                                    AND newer.input_cutoff > run.input_cutoff
                                    AND newer.status = 'succeeded')
                      AND NOT EXISTS (SELECT 1 FROM app.publication publication
                                      WHERE publication.analysis_run_id = run.id
                                        AND publication.status = 'published')
                      AND NOT EXISTS (SELECT 1 FROM app.paper_order paper
                                      WHERE paper.decision_id = decision.id
                                        AND paper.status NOT IN
                                          ('closed', 'cancelled', 'rejected', 'exited', 'invalidated'))
                      AND NOT EXISTS (SELECT 1 FROM analysis.agent_task task
                                      WHERE task.decision_id = decision.id
                                        AND task.status NOT IN
                                          ('succeeded', 'completed', 'failed', 'cancelled'))
                      AND NOT EXISTS (SELECT 1 FROM analysis.shadow_trade shadow
                                      WHERE shadow.decision_id = decision.id
                                        AND shadow.status NOT IN
                                          ('closed', 'unfilled', 'unmeasurable', 'rejected', 'expired'))
                    ORDER BY decision.as_of, decision.id LIMIT %s
                    {lock}
                """, [reference - timedelta(days=30),
                      datetime.fromisoformat(cursor["as_of"]) if cursor.get("as_of")
                      else datetime.min.replace(tzinfo=UTC),
                      cursor.get("decision_id") or "00000000-0000-0000-0000-000000000000",
                      batch_size]).fetchall()
                if not rows:
                    if execute:
                        self._checkpoint(connection, {}, 0, "succeeded")
                    return {"phase": CHECKPOINT, "status": "pass_complete",
                            "archived": 0, "dry_run": not execute}
                if not execute:
                    return {"phase": CHECKPOINT, "status": "eligible",
                            "eligible": len(rows), "dry_run": True}
                selected = [row["decision_id"] for row in rows]
                run_ids = list({row["run_id"] for row in rows})
                connection.execute(
                    "SELECT id FROM analysis.run WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                    [run_ids],
                ).fetchall()
                active = connection.execute("""
                    SELECT EXISTS (
                      SELECT 1 FROM app.paper_order paper
                      WHERE paper.decision_id = ANY(%s)
                        AND paper.status NOT IN ('closed', 'cancelled', 'rejected', 'exited', 'invalidated')
                      UNION ALL
                      SELECT 1 FROM analysis.agent_task task
                      WHERE task.decision_id = ANY(%s)
                        AND task.status NOT IN ('succeeded', 'completed', 'failed', 'cancelled')
                      UNION ALL
                      SELECT 1 FROM analysis.shadow_trade shadow
                      WHERE shadow.decision_id = ANY(%s)
                        AND shadow.status NOT IN
                          ('closed', 'unfilled', 'unmeasurable', 'rejected', 'expired')
                      UNION ALL
                      SELECT 1 FROM app.publication publication
                      WHERE publication.analysis_run_id = ANY(%s)
                        AND publication.status = 'published'
                      UNION ALL
                      SELECT 1 FROM analysis.option_decision dependent
                      WHERE dependent.primary_decision_id = ANY(%s)
                        AND dependent.evidence_state = 'local'
                    ) AS found
                """, [selected, selected, selected, run_ids, selected]).fetchone()["found"]
                if active:
                    raise ValueError("option scan gained an active reference; source retained")
                archived_rows, packs, skipped = self._write_eligible_dependencies(connection, rows)
                tail = rows[-1]
                if not archived_rows:
                    self._checkpoint(connection, {"as_of": tail["as_of"].isoformat(),
                                                  "decision_id": str(tail["decision_id"])},
                                     0, "paused", skipped=skipped)
                    return {"phase": CHECKPOINT, "status": "batch_complete",
                            "archived": 0, "skipped": skipped, "dry_run": False}
                selected = [row["decision_id"] for row in archived_rows]
                receipt = {"contract": "option-scan-evidence.v1",
                           "decision_ids": [str(value) for value in selected],
                           "dependencies": packs}
                artifact = self.service._write_json_gzip(
                    "derived", receipt, source_relation="analysis.option_decision",
                    row_count=1, metadata={"archive_contract": receipt["contract"],
                                           "source_row_ids": receipt["decision_ids"],
                                           "dependency_manifests": packs},
                )
                manifest_id = int(artifact["manifest_id"])
                if self.service.verify(manifest_id=manifest_id)["verified"] != 1:
                    raise ValueError("option dependency receipt failed verification; source retained")
                self.restore_scan(selected[0], manifest_id=manifest_id, keep=False, verify_all=True)
                marker = Jsonb({"evidence_state": "archived", "archive_manifest_id": manifest_id})
                changed = connection.execute("""
                    UPDATE analysis.option_decision scan SET details =
                        %s::jsonb
                        || COALESCE((SELECT jsonb_object_agg(key, value)
                                     FROM jsonb_each(scan.details)
                                     WHERE key = ANY(%s)), '{}'::jsonb)
                        || CASE WHEN scan.details ? 'historical_paths' THEN
                             jsonb_build_object('historical_paths',
                               COALESCE((SELECT jsonb_object_agg(key, value)
                                         FROM jsonb_each(scan.details->'historical_paths')
                                         WHERE key = ANY(%s)), '{}'::jsonb))
                           ELSE '{}'::jsonb END
                        || CASE WHEN scan.details ? 'quote_package' THEN
                             jsonb_build_object('quote_package',
                               COALESCE((SELECT jsonb_object_agg(key, value)
                                         FROM jsonb_each(scan.details->'quote_package')
                                         WHERE key = ANY(%s)), '{}'::jsonb))
                           ELSE '{}'::jsonb END,
                        evidence_state = 'archived', evidence_archive_manifest_id = %s
                    WHERE decision_id = ANY(%s) AND evidence_state = 'local'
                """, [marker, list(_SUMMARY_KEYS), list(_SCENARIO_KEYS),
                      list(_QUOTE_KEYS), manifest_id, selected]).rowcount
                if changed != len(selected):
                    raise ValueError("option scan changed during archival; source retained")
                connection.execute("""
                    UPDATE analysis.decision_evidence SET detail = %s,
                        evidence_state = 'archived', evidence_archive_manifest_id = %s
                    WHERE decision_id = ANY(%s) AND evidence_state = 'local'
                """, [marker, manifest_id, selected])
                self._checkpoint(connection, {"as_of": tail["as_of"].isoformat(),
                                              "decision_id": str(tail["decision_id"])},
                                 changed, "paused", skipped=skipped)
                return {"phase": CHECKPOINT, "status": "batch_complete",
                        "archived": changed, "skipped": skipped,
                        "manifest_id": manifest_id, "dry_run": False}

    def _write_eligible_dependencies(self, connection: Any, rows: list[Any]
                                     ) -> tuple[list[Any], dict[str, list[int]], list[str]]:
        try:
            with connection.transaction():
                return rows, self._write_dependencies(connection, rows), []
        except ValueError as exc:
            if "dependency incomplete" not in str(exc):
                raise
        accepted: list[Any] = []
        packs: dict[str, set[int]] = {}
        skipped: list[str] = []
        for row in rows:
            try:
                with connection.transaction():
                    row_packs = self._write_dependencies(connection, [row])
            except ValueError as exc:
                if "dependency incomplete" not in str(exc):
                    raise
                skipped.append(str(row["decision_id"]))
                continue
            accepted.append(row)
            for relation, ids in row_packs.items():
                packs.setdefault(relation, set()).update(ids)
        return accepted, {relation: sorted(ids) for relation, ids in packs.items()}, skipped

    def _write_dependencies(self, connection: Any, rows: list[Any]) -> dict[str, list[int]]:
        selected = [row["decision_id"] for row in rows]
        run_ids = sorted({row["run_id"] for row in rows})
        snapshot_ids = sorted({row["snapshot_id"] for row in rows})
        quote_keys = {(row["snapshot_id"], row["contract_id"], row["quote_observed_at"])
                      for row in rows}
        feature_keys = {(row["run_id"], row["snapshot_id"], row["contract_id"])
                        for row in rows}
        for row in rows:
            for leg in row["synthetic_legs"] or []:
                if not isinstance(leg, dict) or leg.get("contract_id") is None:
                    raise ValueError("option archive synthetic leg dependency incomplete; source retained")
                try:
                    contract_id = int(leg["contract_id"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("option archive synthetic leg dependency incomplete; source retained") from exc
                if contract_id <= 0:
                    raise ValueError("option archive synthetic leg dependency incomplete; source retained")
                quote_keys.add((row["snapshot_id"], contract_id, row["quote_observed_at"]))
                feature_keys.add((row["run_id"], row["snapshot_id"], contract_id))
        ordered_features = sorted(feature_keys)
        features = connection.execute(
            "SELECT snapshot_id, contract_id, quote_observed_at FROM analysis.option_feature "
            "WHERE (run_id, snapshot_id, contract_id) IN "
            "(SELECT * FROM unnest(%s::uuid[], %s::bigint[], %s::bigint[]))",
            [[key[0] for key in ordered_features], [key[1] for key in ordered_features],
             [key[2] for key in ordered_features]],
        ).fetchall()
        quote_keys.update((row["snapshot_id"], row["contract_id"], row["quote_observed_at"])
                          for row in features)
        ordered_quotes = sorted(quote_keys)
        contract_ids = sorted({key[1] for key in quote_keys})
        relative_ids = sorted({row["relative_value_id"] for row in rows if row["relative_value_id"] is not None})
        snapshots = connection.execute(
            "SELECT id, source_id, ingest_run_id, payload_id, latest_complete_generation_id "
            "FROM raw.option_snapshot WHERE id = ANY(%s)", [snapshot_ids],
        ).fetchall()
        quotes = connection.execute(
            "SELECT capture_generation_id FROM raw.option_quote WHERE "
            "(snapshot_id, contract_id, observed_at) IN "
            "(SELECT * FROM unnest(%s::bigint[], %s::bigint[], %s::timestamptz[]))",
            [[key[0] for key in ordered_quotes], [key[1] for key in ordered_quotes],
             [key[2] for key in ordered_quotes]],
        ).fetchall()
        contracts = connection.execute(
            "SELECT id, underlying_instrument_id FROM catalog.option_contract WHERE id = ANY(%s)",
            [contract_ids],
        ).fetchall()
        relatives = connection.execute(
            "SELECT id, capture_generation_id, analysis_run_id "
            "FROM analysis.option_relative_value WHERE id = ANY(%s)",
            [relative_ids],
        ).fetchall()
        if (len(snapshots) != len(snapshot_ids) or len(quotes) != len(quote_keys)
            or len(contracts) != len(contract_ids) or len(relatives) != len(relative_ids)):
            raise ValueError("option archive source parent dependency incomplete; source retained")
        run_ids = sorted(set(run_ids) | {row["analysis_run_id"] for row in relatives
                                         if row["analysis_run_id"] is not None})
        scan_refs = connection.execute(
            "SELECT decision_id, primary_decision_id, thesis_id "
            "FROM analysis.option_decision WHERE decision_id = ANY(%s)", [selected],
        ).fetchall()
        if len(scan_refs) != len(selected) or any(
            row["thesis_id"] is not None or
            (row["primary_decision_id"] is not None and row["primary_decision_id"] not in selected)
            for row in scan_refs
        ):
            raise ValueError("option archive linked thesis or primary decision requires local evidence; source retained")
        generation_ids = sorted({value for value in (
            *(row["latest_complete_generation_id"] for row in snapshots),
            *(row["capture_generation_id"] for row in quotes),
            *(row["capture_generation_id"] for row in relatives),
        ) if value is not None})
        generations = connection.execute(
            "SELECT id, snapshot_id, ingest_run_id FROM raw.option_capture_generation WHERE id = ANY(%s)",
            [generation_ids],
        ).fetchall()
        if len(generations) != len(generation_ids) or any(
            row["snapshot_id"] not in snapshot_ids for row in generations
        ):
            raise ValueError("option archive capture generation dependency incomplete; source retained")
        payload_ids = sorted({row["payload_id"] for row in snapshots if row["payload_id"] is not None})
        payloads = connection.execute(
            "SELECT id, run_id FROM ingest.payload WHERE id = ANY(%s)", [payload_ids],
        ).fetchall()
        if len(payloads) != len(payload_ids):
            raise ValueError("option archive ingest payload dependency incomplete; source retained")
        ingest_run_ids = sorted({row["ingest_run_id"] for row in (*snapshots, *generations)}
                                | {row["run_id"] for row in payloads})
        ingest_runs = connection.execute(
            "SELECT id, source_id FROM ingest.run WHERE id = ANY(%s)", [ingest_run_ids],
        ).fetchall()
        if len(ingest_runs) != len(ingest_run_ids):
            raise ValueError("option archive ingest run dependency incomplete; source retained")
        source_ids = sorted({row["source_id"] for row in (*snapshots, *ingest_runs)})
        decisions = connection.execute(
            "SELECT instrument_id, strategy_revision_id "
            "FROM analysis.decision WHERE id = ANY(%s)", [selected],
        ).fetchall()
        instrument_ids = sorted({row["underlying_instrument_id"] for row in contracts}
                                | {row["instrument_id"] for row in decisions})
        runs = connection.execute(
            "SELECT id, strategy_revision_id FROM analysis.run WHERE id = ANY(%s)", [run_ids],
        ).fetchall()
        if len(runs) != len(run_ids) or len(decisions) != len(selected):
            raise ValueError("option archive decision run dependency incomplete; source retained")
        strategy_ids = sorted({row["strategy_revision_id"] for row in (*runs, *decisions)
                               if row["strategy_revision_id"] is not None})
        revisions = connection.execute("""
            WITH RECURSIVE lineage AS (
              SELECT id, supersedes_id, hypothesis_id, experiment_family_id
              FROM analysis.strategy_revision WHERE id = ANY(%s)
              UNION
              SELECT parent.id, parent.supersedes_id, parent.hypothesis_id,
                     parent.experiment_family_id
              FROM analysis.strategy_revision parent
              JOIN lineage child ON child.supersedes_id = parent.id
            ) SELECT * FROM lineage
        """, [strategy_ids]).fetchall()
        if not set(strategy_ids) <= {row["id"] for row in revisions}:
            raise ValueError("option archive strategy revision dependency incomplete; source retained")
        revision_ids = sorted({row["id"] for row in revisions})
        family_ids = sorted({row["experiment_family_id"] for row in revisions
                             if row["experiment_family_id"] is not None})
        families = connection.execute(
            "SELECT id, hypothesis_id FROM analysis.experiment_family WHERE id = ANY(%s)",
            [family_ids],
        ).fetchall()
        if len(families) != len(family_ids):
            raise ValueError("option archive experiment family dependency incomplete; source retained")
        hypothesis_ids = sorted({row["hypothesis_id"] for row in revisions
                                 if row["hypothesis_id"] is not None}
                                | {row["hypothesis_id"] for row in families})
        predicates: dict[str, tuple[str, list[Any]]] = {
            "ingest.source": ("id = ANY(%s)", [source_ids]),
            "ingest.run": ("id = ANY(%s)", [ingest_run_ids]),
            "ingest.payload": ("id = ANY(%s)", [payload_ids]),
            "catalog.instrument": ("id = ANY(%s)", [instrument_ids]),
            "analysis.hypothesis": ("id = ANY(%s)", [hypothesis_ids]),
            "analysis.experiment_family": ("id = ANY(%s)", [family_ids]),
            "analysis.strategy_revision": ("id = ANY(%s)", [revision_ids]),
            "analysis.run": ("id = ANY(%s)", [run_ids]),
            "raw.option_snapshot": ("id = ANY(%s)", [snapshot_ids]),
            "raw.option_capture_generation": ("id = ANY(%s)", [generation_ids]),
            "catalog.option_contract": ("id = ANY(%s)", [contract_ids]),
            "raw.option_quote": ("(snapshot_id, contract_id, observed_at) IN "
                                 "(SELECT * FROM unnest(%s::bigint[], %s::bigint[], %s::timestamptz[]))",
                                 [[key[0] for key in ordered_quotes],
                                  [key[1] for key in ordered_quotes],
                                  [key[2] for key in ordered_quotes]]),
            "analysis.option_relative_value": ("id = ANY(%s)", [relative_ids]),
            "analysis.option_relative_value_verification":
                ("relative_value_id = ANY(%s)", [relative_ids]),
            "analysis.option_feature": ("(run_id, snapshot_id, contract_id) IN "
                                        "(SELECT * FROM unnest(%s::uuid[], %s::bigint[], %s::bigint[]))",
                                        [[key[0] for key in ordered_features],
                                         [key[1] for key in ordered_features],
                                         [key[2] for key in ordered_features]]),
            "analysis.decision": ("id = ANY(%s)", [selected]),
            "analysis.option_decision": ("decision_id = ANY(%s)", [selected]),
            "analysis.decision_evidence": ("decision_id = ANY(%s)", [selected]),
        }
        required = {
            "ingest.source": len(source_ids), "ingest.run": len(ingest_run_ids),
            "ingest.payload": len(payload_ids), "catalog.instrument": len(instrument_ids),
            "analysis.hypothesis": len(hypothesis_ids),
            "analysis.experiment_family": len(family_ids),
            "analysis.strategy_revision": len(revision_ids),
            "analysis.run": len(run_ids), "raw.option_snapshot": len(snapshot_ids),
            "raw.option_capture_generation": len(generation_ids),
            "catalog.option_contract": len(contract_ids),
            "raw.option_quote": len(quote_keys),
            "analysis.option_relative_value": len(relative_ids),
            "analysis.decision": len(rows), "analysis.option_decision": len(rows),
        }
        packs: dict[str, list[int]] = {}
        for relation in RELATIONS:
            predicate, params = predicates[relation]
            records = connection.execute(
                f"SELECT to_jsonb(source)::text AS row_json FROM {relation} source "
                f"WHERE {predicate} ORDER BY 1", params,
            ).fetchall()
            if relation in required and len(records) != required[relation]:
                raise ValueError(f"option archive dependency incomplete: {relation}; source retained")
            if records:
                packs[relation] = RowArchive(self.service).write(connection, relation, records)
        return packs

    def restore_scan(self, decision_id: Any, *, manifest_id: int | None = None,
                     keep: bool = True, destination_dsn: str | None = None,
                     verify_all: bool = False) -> dict[str, Any]:
        """Restore a selected scan and archived dependencies into typed staging tables."""
        selected = str(UUID(str(decision_id)))
        with self.service.runtime.read(MAINTENANCE_PROFILE) as connection:
            if manifest_id is None:
                owner = connection.execute("""
                    SELECT evidence_archive_manifest_id FROM analysis.option_decision
                    WHERE decision_id = %s AND evidence_state = 'archived'
                """, [selected]).fetchone()
                if owner is None or owner["evidence_archive_manifest_id"] is None:
                    raise ValueError("selected scan has no archive reference")
                manifest_id = int(owner["evidence_archive_manifest_id"])
            root = connection.execute("""
                SELECT nas_uri, format, metadata FROM ops.storage_archive_manifest WHERE id = %s
            """, [manifest_id]).fetchone()
        if root is None or root["format"] != "json.gz":
            raise ValueError("option archive receipt is missing or unsupported")
        if self.service.verify(manifest_id=manifest_id)["verified"] != 1:
            raise ValueError("option archive receipt failed verification")
        with gzip.open(root["nas_uri"], "rt") as handle:
            receipt = json.load(handle)
        if receipt.get("contract") != "option-scan-evidence.v1" or selected not in receipt.get("decision_ids", []):
            raise ValueError("selected scan is not covered by the archive receipt")
        included = set(receipt["decision_ids"]) if verify_all else {selected}
        dependencies = receipt.get("dependencies") or {}
        if set(dependencies) != set(RELATIONS):
            # Evidence and relative-value rows are optional; core typed owners are not.
            required = {"ingest.source", "ingest.run", "catalog.instrument",
                        "analysis.run", "raw.option_snapshot", "catalog.option_contract",
                        "raw.option_quote", "analysis.decision", "analysis.option_decision"}
            if not required <= set(dependencies) or set(dependencies) - set(RELATIONS):
                raise ValueError("option archive dependency list is incomplete")
        pack_files: dict[int, tuple[str, str]] = {}
        with self.service.runtime.read(MAINTENANCE_PROFILE) as connection:
            for pack_id in {int(value) for values in dependencies.values() for value in values}:
                pack = connection.execute(
                    "SELECT nas_uri, format FROM ops.storage_archive_manifest WHERE id = %s",
                    [pack_id],
                ).fetchone()
                if pack is None or pack["format"] not in {"json.gz", "postgres-copy-text-gzip.v1"}:
                    raise ValueError("option archive dependency format unsupported")
                pack_files[pack_id] = (str(pack["nas_uri"]), str(pack["format"]))
        schema = "market_restore_" + uuid4().hex[:16]
        counts: dict[str, int] = {}
        if keep:
            target_dsn = destination_dsn or os.environ.get("MARKET_STORAGE_RESTORE_DATABASE_URL")
            if not target_dsn:
                raise ValueError("set MARKET_STORAGE_RESTORE_DATABASE_URL to a migrated staging PostgreSQL database")
            target = psycopg.connect(target_dsn, row_factory=dict_row)
        else:
            target = self.service.runtime.transaction(MAINTENANCE_PROFILE)
        with target as connection:
            if keep:
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            for relation in RELATIONS:
                ids = dependencies.get(relation) or []
                if not ids:
                    continue
                table = relation.replace(".", "_")
                target_table = sql.Identifier(schema, table) if keep else sql.Identifier(schema + "_" + table)
                connection.execute(sql.SQL("CREATE {}TABLE {} (LIKE {} INCLUDING CONSTRAINTS INCLUDING INDEXES) {}").format(
                    sql.SQL("") if keep else sql.SQL("TEMP "), target_table,
                    sql.Identifier(*relation.split(".")),
                    sql.SQL("") if keep else sql.SQL("ON COMMIT DROP")))
                count = 0
                for pack_id in ids:
                    if self.service.verify(manifest_id=int(pack_id))["verified"] != 1:
                        raise ValueError(f"option archive dependency corrupt: {pack_id}")
                    path, archive_format = pack_files[int(pack_id)]
                    if archive_format == "json.gz":
                        with gzip.open(path, "rt") as handle:
                            entries = json.load(handle)
                    else:
                        envelope = sql.Identifier("option_archive_copy_" + uuid4().hex)
                        connection.execute(sql.SQL("""
                            CREATE TEMP TABLE {} (relation text, source_columns jsonb,
                              source_database jsonb, row_json text) ON COMMIT DROP
                        """).format(envelope))
                        with gzip.open(path, "rb") as handle, connection.cursor() as cursor:
                            with cursor.copy(sql.SQL("COPY {} FROM STDIN WITH (FORMAT text)").format(envelope)) as copy:
                                for block in iter(lambda: handle.read(1024**2), b""):
                                    copy.write(block)
                        entries = connection.execute(sql.SQL(
                            "SELECT relation, row_json FROM {}"
                        ).format(envelope)).fetchall()
                        if len(entries) != 1:
                            raise ValueError("option COPY dependency row count mismatch")
                        connection.execute(sql.SQL("DROP TABLE {}").format(envelope))
                    for entry in entries:
                        if entry.get("relation") != relation:
                            raise ValueError("option archive relation mismatch")
                        raw = entry["row_json"]
                        if relation in {"analysis.decision", "analysis.option_decision", "analysis.decision_evidence"}:
                            payload = json.loads(raw)
                            key = payload.get("decision_id") if relation != "analysis.decision" else payload.get("id")
                            if str(key) not in included:
                                continue
                        restored = connection.execute(sql.SQL("""
                            INSERT INTO {} AS restored
                            SELECT (jsonb_populate_record(NULL::{}, %s::jsonb)).*
                            ON CONFLICT DO NOTHING
                            RETURNING to_jsonb(restored) = %s::jsonb AS same
                        """).format(target_table,
                                    sql.Identifier(*relation.split("."))), [raw, raw]).fetchone()
                        if restored is None:
                            identical = connection.execute(sql.SQL(
                                "SELECT count(*) = 1 AS same FROM {} existing "
                                "WHERE to_jsonb(existing) = %s::jsonb"
                            ).format(target_table), [raw]).fetchone()
                            if identical is None or identical["same"] is not True:
                                raise ValueError("option archived dependency conflicts with restored row")
                            continue
                        if restored["same"] is not True:
                            raise ValueError("option typed scratch restoration changed original values")
                        count += 1
                counts[relation] = count
            if counts.get("analysis.decision") != len(included) or counts.get("analysis.option_decision") != len(included):
                raise ValueError("option archive lacks the selected decision")
        return {"decision_id": selected, "manifest_id": manifest_id,
                "staging_schema": schema if keep else None, "typed_rows": counts,
                "status": "restored" if keep else "verified"}

    @staticmethod
    def _checkpoint(connection: Any, cursor: dict[str, Any], archived: int, status: str,
                    *, skipped: list[str] | None = None) -> None:
        connection.execute("""
            INSERT INTO ops.storage_archive_checkpoint
              (checkpoint_key, archive_kind, source_relation, cursor, run_status, counts, updated_at)
            VALUES (%s, 'derived', 'analysis.option_decision', %s, %s, %s, now())
            ON CONFLICT (checkpoint_key) DO UPDATE SET cursor = EXCLUDED.cursor,
              run_status = EXCLUDED.run_status, counts = EXCLUDED.counts, updated_at = now()
        """, [CHECKPOINT, Jsonb(cursor), status,
              Jsonb({"archived": archived, "skipped_dependency_ids": skipped or []})])
