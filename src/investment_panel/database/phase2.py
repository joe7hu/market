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

    def record_observations(self, observations: Iterable[PITObservation]) -> int:
        rows = tuple(observations)
        if not rows:
            return 0
        with self.runtime.transaction() as connection:
            for row in rows:
                self._ensure_source(connection, row.source_id)
                connection.execute(
                    """INSERT INTO raw.market_observation
                       (observation_id, field_name, dimension, asset_class, source_id,
                        source_version, value, unit, observed_at, available_at,
                        publication_at, release_at, vintage_at, status, confidence, metadata)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (observation_id) DO NOTHING""",
                    [
                        row.observation_id, row.field_name, row.dimension, row.asset_class,
                        row.source_id, row.source_version, Jsonb(row.value), row.unit,
                        row.observed_at, row.available_at, row.publication_at, row.release_at,
                        row.vintage_at, row.status.value, row.confidence, Jsonb(row.metadata),
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
        paths = tuple(scenarios)
        with self.runtime.transaction() as connection:
            connection.execute(
                """INSERT INTO analysis.market_state_posterior
                   (posterior_id, as_of, input_cutoff, model_version, payload)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT (posterior_id) DO NOTHING""",
                [posterior.posterior_id, posterior.as_of, posterior.input_cutoff, posterior.contract_version, Jsonb(posterior.model_dump(mode="json"))],
            )
            connection.execute(
                """INSERT INTO analysis.market_coverage_vector (vector_id, as_of, payload)
                   VALUES (%s,%s,%s) ON CONFLICT (vector_id) DO NOTHING""",
                [coverage.vector_id, coverage.as_of, Jsonb(coverage.model_dump(mode="json"))],
            )
            for path in paths:
                connection.execute(
                    """INSERT INTO analysis.market_scenario_path
                       (scenario_hash, snapshot_id, model_version, path)
                       VALUES (%s,%s,%s,%s) ON CONFLICT (scenario_hash) DO NOTHING""",
                    [path.scenario_hash, path.snapshot_id, path.model_version, Jsonb(path.model_dump(mode="json"))],
                )
        return {"posterior_id": posterior.posterior_id, "vector_id": coverage.vector_id, "scenario_count": len(paths)}

    def record_option_liquidity_sla(self, *, as_of: Any, source_id: str, payload: dict[str, Any]) -> str:
        """Store one immutable OI/volume SLA result from an approved seam."""

        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        sla_id = hashlib.sha256(f"{as_of.isoformat()}|{source_id}|{encoded}".encode("utf-8")).hexdigest()
        with self.runtime.transaction() as connection:
            self._ensure_source(connection, source_id)
            connection.execute(
                """INSERT INTO analysis.option_liquidity_sla
                   (sla_id, as_of, source_id, payload)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (sla_id) DO NOTHING""",
                [sla_id, as_of, source_id, Jsonb(payload)],
            )
        return sla_id

    @staticmethod
    def _ensure_source(connection: Any, source_id: str) -> None:
        """Register a source only when it produces a real Phase 2 artifact."""

        connection.execute(
            """INSERT INTO ingest.source
               (id, name, family, kind, origin, enabled, ingestion_mode,
                capabilities, operational_state, health_owner, freshness_seconds)
               VALUES (%s,%s,'phase2','external','phase2',false,'external',
                       %s,'standby','update_phase2_sources',86400)
               ON CONFLICT (id) DO NOTHING""",
            [source_id, source_id, Jsonb({"phase2": True})],
        )

    def rows(self, *, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
        safe_limit = max(1, min(int(limit), 500))
        with self.runtime.read() as connection:
            return {
                "market_state_posterior": [dict(row) for row in connection.execute("SELECT posterior_id, as_of, input_cutoff, model_version, payload, created_at FROM analysis.market_state_posterior ORDER BY as_of DESC, posterior_id DESC LIMIT %s", [safe_limit]).fetchall()],
                "market_coverage_vector": [dict(row) for row in connection.execute("SELECT vector_id, as_of, payload, created_at FROM analysis.market_coverage_vector ORDER BY as_of DESC, vector_id DESC LIMIT %s", [safe_limit]).fetchall()],
                "market_scenario_paths": [dict(row) for row in connection.execute("SELECT scenario_hash, snapshot_id, model_version, path, created_at FROM analysis.market_scenario_path ORDER BY created_at DESC, scenario_hash LIMIT %s", [safe_limit]).fetchall()],
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
