"""PostgreSQL owner for Phase 3 strategy definitions and evidence tapes."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from psycopg.types.json import Jsonb

from investment_panel.core.strategy_factory import MANIFEST_PARTS, StrategySpec, manifest_hash
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class StrategyFactoryRepository:
    """Extend the existing strategy revision and research authorities."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def register(self, spec: StrategySpec, *, status: str = "candidate", supersedes_id: int | None = None) -> int:
        digest = manifest_hash(spec.manifest)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            existing = connection.execute(
                "SELECT id, status, parameters, mechanism_class, economic_mechanism, falsification_rule, source_definition_version, promotability, actionability FROM analysis.strategy_revision WHERE strategy_key = %s AND revision = %s",
                [spec.strategy_key, spec.revision],
            ).fetchone()
            if existing is not None:
                manifest = connection.execute("SELECT manifest_hash FROM analysis.strategy_manifest WHERE strategy_revision_id = %s", [existing["id"]]).fetchone()
                if manifest is None or str(manifest["manifest_hash"]) != digest:
                    raise ValueError("strategy revision or manifest identity conflicts")
                return int(existing["id"])
            revision = connection.execute(
                """INSERT INTO analysis.strategy_revision
                   (strategy_key, revision, name, status, parameters, supersedes_id,
                    mechanism_class, economic_mechanism, falsification_rule,
                    source_definition_version, promotability, actionability, p3_enabled)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)
                   RETURNING id""",
                [spec.strategy_key, spec.revision, spec.name, status, Jsonb(spec.parameters), supersedes_id,
                 spec.mechanism_class, spec.economic_mechanism, spec.falsification_rule,
                 spec.source_definition_version, spec.promotability, spec.actionability],
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO analysis.strategy_manifest
                   (strategy_revision_id, source_definition_version, source_manifest,
                    data_manifest, cost_manifest, capacity_manifest, failure_manifest, manifest_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [revision, spec.source_definition_version, *[Jsonb(spec.manifest[key]) for key in MANIFEST_PARTS], digest],
            )
            return int(revision)

    def record_pnl_tape(self, rows: Iterable[Mapping[str, Any]]) -> int:
        records = tuple(rows)
        if len(records) > 10_000:
            raise ValueError("strategy P&L tape exceeds bound")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for row in records:
                input_hash = str(row.get("input_hash") or "")
                if len(input_hash) != 64 or any(char not in "0123456789abcdef" for char in input_hash.lower()):
                    raise ValueError("strategy P&L tape requires a SHA-256 input hash")
                connection.execute(
                    """INSERT INTO analysis.strategy_pnl_tape
                       (strategy_revision_id, instrument_id, pnl_date, input_cutoff,
                        gross_return, cost, net_return, tail_return, regime,
                        observed_at, available_at, input_hash, metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (strategy_revision_id, instrument_id, pnl_date, input_hash) DO NOTHING""",
                    [row["strategy_revision_id"], row.get("instrument_id"), row["pnl_date"], row["input_cutoff"],
                     row.get("gross_return"), row.get("cost"), row.get("net_return"), row.get("tail_return"),
                     row.get("regime"), row["observed_at"], row["available_at"], input_hash, Jsonb(dict(row.get("metadata") or {}))],
                )
        return len(records)

    def record_monitoring(self, *, strategy_revision_id: int, evidence_kind: str, input_cutoff: Any, observed_at: Any, available_at: Any, input_hash: str, metrics: Mapping[str, Any], evidence: Mapping[str, Any] | None = None) -> None:
        if evidence_kind not in {"correlation", "tail_correlation", "crowding", "capacity", "decay", "regime"}:
            raise ValueError("unknown strategy monitoring evidence kind")
        if len(input_hash) != 64:
            raise ValueError("strategy monitoring requires a SHA-256 input hash")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """INSERT INTO analysis.strategy_monitoring_evidence
                   (strategy_revision_id, evidence_kind, input_cutoff, observed_at,
                    available_at, input_hash, metrics, evidence)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (strategy_revision_id, evidence_kind, input_cutoff, input_hash) DO NOTHING""",
                [strategy_revision_id, evidence_kind, input_cutoff, observed_at, available_at, input_hash, Jsonb(dict(metrics)), Jsonb(dict(evidence or {}))],
            )

    def record_comparison(self, *, champion_revision_id: int, challenger_revision_id: int, input_cutoff: Any, observed_at: Any, available_at: Any, input_hash: str, distinctness: str, explanation: str, metrics: Mapping[str, Any] | None = None) -> None:
        if champion_revision_id == challenger_revision_id:
            raise ValueError("champion and challenger must be different revisions")
        if distinctness not in {"distinct", "replica", "exposure_sleeve", "inconclusive"}:
            raise ValueError("unknown champion/challenger distinctness")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """INSERT INTO analysis.strategy_comparison
                   (champion_revision_id, challenger_revision_id, input_cutoff,
                    observed_at, available_at, input_hash, distinctness, explanation, metrics)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (champion_revision_id, challenger_revision_id, input_cutoff, input_hash) DO NOTHING""",
                [champion_revision_id, challenger_revision_id, input_cutoff, observed_at, available_at, input_hash, distinctness, explanation, Jsonb(dict(metrics or {}))],
            )

    def promote(self, strategy_revision_id: int) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("UPDATE analysis.strategy_revision SET status = 'active', promoted_at = clock_timestamp() WHERE id = %s", [strategy_revision_id])

    def rows(self, *, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
        safe_limit = max(1, min(int(limit), 500))
        with self.runtime.read() as connection:
            return {
                "strategy_registry": [dict(row) for row in connection.execute("SELECT * FROM analysis.strategy_registry ORDER BY strategy_key, revision LIMIT %s", [safe_limit]).fetchall()],
                "strategy_trial_accounting": [dict(row) for row in connection.execute("SELECT * FROM analysis.strategy_trial_accounting ORDER BY input_cutoff DESC, trial_key LIMIT %s", [safe_limit]).fetchall()],
                "strategy_pnl_tape": [dict(row) for row in connection.execute("SELECT * FROM analysis.strategy_pnl_tape ORDER BY pnl_date DESC, id DESC LIMIT %s", [safe_limit]).fetchall()],
                "strategy_monitoring": [dict(row) for row in connection.execute("SELECT * FROM analysis.strategy_monitoring_evidence ORDER BY input_cutoff DESC, id DESC LIMIT %s", [safe_limit]).fetchall()],
                "strategy_comparisons": [dict(row) for row in connection.execute("SELECT * FROM analysis.strategy_comparison ORDER BY input_cutoff DESC, id DESC LIMIT %s", [safe_limit]).fetchall()],
            }


__all__ = ["StrategyFactoryRepository"]
