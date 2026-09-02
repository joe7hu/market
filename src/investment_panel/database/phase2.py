"""PostgreSQL owner for immutable Phase 2 artifacts."""

from __future__ import annotations

import json
import hashlib
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.core.phase2 import (
    CoverageVector,
    MarketStatePosterior,
    PITObservation,
    ScenarioPath,
)
from investment_panel.database.runtime import DatabaseRuntime


class Phase2Repository:
    """Persist and read Phase 2 rows without a second market-state authority."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def record_observations(
        self,
        observations: Iterable[PITObservation],
        *,
        ingest_run_id: str | None = None,
        payload_id: int | None = None,
        parent_snapshot_id: str | None = None,
    ) -> int:
        rows = tuple(observations)
        if not rows:
            return 0
        with self.runtime.transaction() as connection:
            for row in rows:
                effective_run_id = row.ingest_run_id or ingest_run_id
                effective_payload_id = row.payload_id if row.payload_id is not None else payload_id
                if not effective_run_id or effective_payload_id is None or not row.content_hash:
                    raise ValueError("persisted Phase 2 observations require ingest_run_id, payload_id, and content_hash")
                self._ensure_source(connection, row.source_id)
                event_values = {
                    "actual": getattr(row, "actual", None),
                    "consensus": getattr(row, "consensus", None),
                    "surprise": getattr(row, "surprise", None),
                    "revision": getattr(row, "revision", None),
                }
                existing = connection.execute(
                    "SELECT source_id, source_version, value, unit, ingest_run_id, payload_id, content_hash, parent_snapshot_id, observed_at, available_at, publication_at, release_at, vintage_at, status, confidence, metadata, actual, consensus, surprise, revision FROM raw.market_observation WHERE observation_id = %s",
                    [row.observation_id],
                ).fetchone()
                expected = (
                    row.source_id, row.source_version, row.value, row.unit, str(effective_run_id), effective_payload_id,
                    row.content_hash, parent_snapshot_id or row.parent_snapshot_id, row.observed_at, row.available_at,
                    row.publication_at, row.release_at, row.vintage_at, row.status.value, row.confidence, row.metadata,
                    event_values["actual"], event_values["consensus"], event_values["surprise"], event_values["revision"],
                )
                if existing is not None:
                    actual = tuple(existing[key] for key in ("source_id", "source_version", "value", "unit", "ingest_run_id", "payload_id", "content_hash", "parent_snapshot_id", "observed_at", "available_at", "publication_at", "release_at", "vintage_at", "status", "confidence", "metadata", "actual", "consensus", "surprise", "revision"))
                    actual = (*actual[:4], str(actual[4]), *actual[5:])
                    if actual != expected:
                        raise ValueError(f"Phase 2 observation identity conflicts: {row.observation_id}")
                    continue
                connection.execute(
                    """INSERT INTO raw.market_observation
                       (observation_id, field_name, dimension, asset_class, source_id,
                        source_version, value, unit, ingest_run_id, payload_id,
                        content_hash, parent_snapshot_id, observed_at, available_at,
                        publication_at, release_at, vintage_at, actual, consensus,
                        surprise, revision, status, confidence, metadata)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [
                        row.observation_id, row.field_name, row.dimension, row.asset_class,
                        row.source_id, row.source_version, Jsonb(row.value), row.unit,
                        effective_run_id, effective_payload_id, row.content_hash,
                        parent_snapshot_id or row.parent_snapshot_id, row.observed_at, row.available_at,
                        row.publication_at, row.release_at, row.vintage_at, event_values["actual"],
                        event_values["consensus"], event_values["surprise"], event_values["revision"],
                        row.status.value, row.confidence, Jsonb(row.metadata),
                    ],
                )
        return len(rows)

    def publish(
        self,
        posterior: MarketStatePosterior,
        coverage: CoverageVector,
        scenarios: Iterable[ScenarioPath] = (),
    ) -> dict[str, Any]:
        if posterior.input_cutoff != coverage.as_of:
            raise ValueError("Phase 2 coverage and posterior must share one cutoff")
        if posterior.input_content_hash != coverage.input_content_hash:
            raise ValueError("Phase 2 coverage and posterior input hashes must match")
        if tuple(posterior.ingest_run_ids) != tuple(coverage.ingest_run_ids):
            raise ValueError("Phase 2 coverage and posterior ingest runs must match")
        paths = tuple(scenarios)
        with self.runtime.transaction() as connection:
            if coverage.parent_snapshot_id != posterior.parent_snapshot_id:
                raise ValueError("Phase 2 coverage and posterior parent snapshots must match")
            self._assert_or_insert(
                connection,
                "analysis.market_state_posterior",
                "posterior_id",
                posterior.posterior_id,
                {"as_of": posterior.as_of, "input_cutoff": posterior.input_cutoff, "model_version": posterior.contract_version, "status": posterior.status.value, "payload": posterior.model_dump(mode="json"), "ingest_run_ids": list(posterior.ingest_run_ids), "input_content_hash": posterior.input_content_hash, "parent_snapshot_id": posterior.parent_snapshot_id},
            )
            connection.execute(
                """INSERT INTO analysis.market_state_posterior
                   (posterior_id, as_of, input_cutoff, model_version, status, payload,
                    ingest_run_ids, input_content_hash, parent_snapshot_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (posterior_id) DO NOTHING""",
                [posterior.posterior_id, posterior.as_of, posterior.input_cutoff, posterior.contract_version, posterior.status.value, Jsonb(posterior.model_dump(mode="json")), Jsonb(list(posterior.ingest_run_ids)), posterior.input_content_hash, posterior.parent_snapshot_id],
            )
            self._assert_or_insert(connection, "analysis.market_coverage_vector", "vector_id", coverage.vector_id, {"as_of": coverage.as_of, "status": coverage.rows[0].status.value if coverage.rows else "MISSING_HISTORY", "payload": coverage.model_dump(mode="json"), "ingest_run_ids": list(coverage.ingest_run_ids), "input_content_hash": coverage.input_content_hash, "parent_snapshot_id": coverage.parent_snapshot_id})
            connection.execute("""INSERT INTO analysis.market_coverage_vector
                   (vector_id, as_of, status, payload, ingest_run_ids, input_content_hash, parent_snapshot_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (vector_id) DO NOTHING""", [coverage.vector_id, coverage.as_of, coverage.rows[0].status.value if coverage.rows else "MISSING_HISTORY", Jsonb(coverage.model_dump(mode="json")), Jsonb(list(coverage.ingest_run_ids)), coverage.input_content_hash, coverage.parent_snapshot_id])
            for path in paths:
                if (path.snapshot_id != coverage.parent_snapshot_id or path.parent_snapshot_id != coverage.parent_snapshot_id
                        or path.posterior_id != posterior.posterior_id or path.model_version != posterior.contract_version
                        or path.input_content_hash != posterior.input_content_hash
                        or tuple(path.ingest_run_ids) != tuple(posterior.ingest_run_ids)):
                    raise ValueError("scenario path lineage does not reference its parent snapshot and posterior")
                self._assert_or_insert(connection, "analysis.market_scenario_path", "scenario_hash", path.scenario_hash, {"snapshot_id": path.snapshot_id, "parent_snapshot_id": path.parent_snapshot_id, "posterior_id": path.posterior_id, "model_version": path.model_version, "ingest_run_ids": list(path.ingest_run_ids), "input_content_hash": path.input_content_hash, "path": path.model_dump(mode="json")})
                connection.execute(
                    """INSERT INTO analysis.market_scenario_path
                       (scenario_hash, snapshot_id, parent_snapshot_id, posterior_id,
                        model_version, ingest_run_ids, input_content_hash, path)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (scenario_hash) DO NOTHING""",
                    [path.scenario_hash, path.snapshot_id, path.parent_snapshot_id, path.posterior_id, path.model_version, Jsonb(list(path.ingest_run_ids)), path.input_content_hash, Jsonb(path.model_dump(mode="json"))],
                )
        return {"posterior_id": posterior.posterior_id, "vector_id": coverage.vector_id, "scenario_count": len(paths)}

    def record_option_liquidity_sla(self, *, as_of: Any, source_id: str, payload: dict[str, Any], ingest_run_id: str | None = None, payload_id: int | None = None, parent_snapshot_id: str | None = None) -> str:
        """Store one immutable OI/volume SLA result from an approved seam."""

        if not ingest_run_id or payload_id is None:
            raise ValueError("option liquidity SLA requires ingest_run_id and payload_id")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        sla_id = hashlib.sha256(f"{as_of.isoformat()}|{source_id}|{encoded}".encode("utf-8")).hexdigest()
        with self.runtime.transaction() as connection:
            self._ensure_source(connection, source_id)
            self._assert_or_insert(
                connection,
                "analysis.option_liquidity_sla",
                "sla_id",
                sla_id,
                {"as_of": as_of, "source_id": source_id, "ingest_run_id": ingest_run_id, "payload_id": payload_id,
                 "payload_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                 "parent_snapshot_id": parent_snapshot_id, "payload": payload},
            )
            connection.execute(
                """INSERT INTO analysis.option_liquidity_sla
                   (sla_id, as_of, source_id, ingest_run_id, payload_id, payload_hash, parent_snapshot_id, payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (sla_id) DO NOTHING""",
                [sla_id, as_of, source_id, ingest_run_id, payload_id, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), parent_snapshot_id, Jsonb(payload)],
            )
        return sla_id

    @staticmethod
    def _assert_or_insert(connection: Any, table: str, key_name: str, key: str, expected: dict[str, Any]) -> None:
        existing = connection.execute(f"SELECT * FROM {table} WHERE {key_name} = %s", [key]).fetchone()
        if existing is None:
            return
        comparisons = {
            column: expected[column]
            for column in (
                "as_of", "input_cutoff", "model_version", "status", "payload", "ingest_run_ids",
                "input_content_hash", "parent_snapshot_id", "snapshot_id", "path", "source_id",
                "ingest_run_id", "payload_id", "payload_hash",
            )
            if column in expected
        }
        for column, value in comparisons.items():
            actual = existing[column] if column in existing else None
            if column == "ingest_run_id" and actual is not None:
                actual = str(actual)
            if column in existing and actual != value:
                raise ValueError(f"immutable Phase 2 identity conflicts: {table}.{key_name}={key} column={column}")

    @staticmethod
    def _ensure_source(connection: Any, source_id: str) -> None:
        """Register a source only when it produces a real Phase 2 artifact."""

        connection.execute(
            """INSERT INTO ingest.source
               (id, name, family, kind, origin, enabled, ingestion_mode,
                capabilities, operational_state, health_owner, freshness_seconds)
               VALUES (%s,%s,'phase2','external','phase2',true,'external',
                       %s,'active','update_phase2_sources',86400)
               ON CONFLICT (id) DO NOTHING""",
            [source_id, source_id, Jsonb({"phase2": True})],
        )

    def rows(self, *, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
        safe_limit = max(1, min(int(limit), 500))
        with self.runtime.read() as connection:
            return {
                "market_state_posterior": [dict(row) for row in connection.execute("SELECT posterior_id, as_of, input_cutoff, model_version, status, payload, ingest_run_ids, input_content_hash, parent_snapshot_id, created_at FROM analysis.market_state_posterior ORDER BY as_of DESC, posterior_id DESC LIMIT %s", [safe_limit]).fetchall()],
                "market_coverage_vector": [dict(row) for row in connection.execute("SELECT vector_id, as_of, status, payload, ingest_run_ids, input_content_hash, parent_snapshot_id, created_at FROM analysis.market_coverage_vector ORDER BY as_of DESC, vector_id DESC LIMIT %s", [safe_limit]).fetchall()],
                "market_scenario_paths": [dict(row) for row in connection.execute("SELECT scenario_hash, snapshot_id, parent_snapshot_id, posterior_id, model_version, ingest_run_ids, input_content_hash, path, created_at FROM analysis.market_scenario_path ORDER BY created_at DESC, scenario_hash LIMIT %s", [safe_limit]).fetchall()],
                "option_liquidity_sla": [dict(row) for row in connection.execute("SELECT sla_id, as_of, source_id, payload, created_at FROM analysis.option_liquidity_sla ORDER BY as_of DESC, sla_id DESC LIMIT %s", [safe_limit]).fetchall()],
            }


def phase2_row_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize psycopg JSONB and timestamps for API read models."""

    payload = row.get("payload", row.get("path"))
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            pass
    result = {key: value for key, value in row.items() if key not in {"payload", "path"}}
    if payload is not None:
        result["payload"] = payload
    return result


__all__ = ["Phase2Repository", "phase2_row_payload"]
