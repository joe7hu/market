"""PostgreSQL owner for Phase 3 strategy definitions and evidence tapes."""

from __future__ import annotations

from datetime import datetime
import os
from typing import Any, Iterable, Mapping

from psycopg.types.json import Jsonb

from investment_panel.domain.strategies.catalog import (
    IMPLEMENTATION_CATALOG,
    MANIFEST_PARTS,
    StrategySignal,
    StrategySpec,
    content_hash,
    is_martingale_family,
)
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime, JOB_PROFILE


def _require_pit(input_cutoff: Any, available_at: Any) -> None:
    if not isinstance(input_cutoff, datetime) or input_cutoff.tzinfo is None:
        raise ValueError("Phase 3 input cutoff must be timezone-aware")
    if not isinstance(available_at, datetime) or available_at.tzinfo is None or available_at > input_cutoff:
        raise ValueError("Phase 3 evidence is not point-in-time available")


def _require_result_pit(connection: Any, research_trial_id: Any, trial_result_id: Any, input_cutoff: datetime) -> None:
    row = connection.execute(
        "SELECT available_at FROM analysis.trial_result WHERE id = %s AND research_trial_id = %s",
        [trial_result_id, research_trial_id],
    ).fetchone()
    if row is None or row["available_at"] is None or row["available_at"] > input_cutoff:
        raise ValueError("Phase 3 trial result is not point-in-time available")


class StrategyFactoryRepository:
    """Extend the existing strategy revision and research authorities."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    @staticmethod
    def _validate_supersedes_parent(connection: Any, spec: StrategySpec, supersedes_id: int) -> None:
        parent = connection.execute(
            """SELECT id, strategy_key, revision, authority_group
                 FROM analysis.strategy_revision
                WHERE id = %s
                FOR UPDATE""",
            [supersedes_id],
        ).fetchone()
        if parent is None:
            raise ValueError("superseded strategy revision is missing")
        parent_base, separator, _parent_version = str(parent["strategy_key"]).rpartition("_v")
        strategy_base, strategy_separator, _strategy_version = spec.strategy_key.rpartition("_v")
        if (
            not separator
            or not strategy_separator
            or parent_base != strategy_base
            or int(parent["revision"]) >= spec.revision
            or parent["authority_group"] != f"phase3:{parent['strategy_key']}"
        ):
            raise ValueError("superseded strategy revision is not a valid parent")

    def start_strategy_run(
        self,
        *,
        strategy_keys: tuple[str, ...],
        strategy_revisions: tuple[tuple[str, int], ...] = (),
        scopes: tuple[str, ...],
        input_cutoff: datetime,
        mode: str,
    ) -> dict[str, Any]:
        if mode not in {"research", "replay"}:
            raise ValueError("strategy run mode is invalid")
        input_hash = content_hash({
            "strategy_keys": strategy_keys, "strategy_revisions": strategy_revisions,
            "scopes": scopes, "cutoff": input_cutoff.isoformat(), "mode": mode,
        })
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                """INSERT INTO analysis.run
                    (run_type, input_cutoff, code_version, feature_versions,
                     input_hash, started_at, status, summary, inputs)
                   VALUES ('strategy_research', %s, %s, %s, %s, clock_timestamp(), 'running', %s, %s)
                RETURNING id, started_at""",
                [input_cutoff, os.environ.get("MARKET_BACKEND_COMMIT", "unknown"),
                 Jsonb({"strategy_workflow": "v2"}), input_hash,
                 Jsonb({"planned_count": len(strategy_keys) * len(scopes), "mode": mode}),
                 Jsonb({"strategy_keys": list(strategy_keys), "strategy_revisions": [list(item) for item in strategy_revisions],
                        "scopes": list(scopes), "mode": mode})],
            ).fetchone()
        return {"run_id": str(row["id"]), "started_at": row["started_at"], "input_hash": input_hash}

    def finish_strategy_run(
        self,
        run_id: str,
        *,
        status: str,
        summary: Mapping[str, Any],
        input_manifest: Mapping[str, Any] | None = None,
    ) -> None:
        if status not in {"succeeded", "partial", "failed", "canceled"}:
            raise ValueError("strategy run terminal status is invalid")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            run = connection.execute(
                """SELECT status, input_hash
                     FROM analysis.run
                    WHERE id = %s AND run_type = 'strategy_research'
                    FOR UPDATE""",
                [run_id],
            ).fetchone()
            if run is None or run["status"] != "running":
                raise ValueError("strategy run is missing or already terminal")
            if input_manifest is None:
                connection.execute(
                    """UPDATE analysis.run
                          SET status = %s, finished_at = clock_timestamp(), summary = %s
                        WHERE id = %s""",
                    [status, Jsonb(dict(summary)), run_id],
                )
                return
            resolved_manifest = dict(input_manifest)
            final_input_hash = content_hash({
                "planned_input_hash": run["input_hash"],
                "resolved_input_manifest": resolved_manifest,
            })
            connection.execute(
                """UPDATE analysis.run
                      SET status = %s, finished_at = clock_timestamp(), summary = %s,
                          input_hash = %s,
                          inputs = inputs || %s
                    WHERE id = %s""",
                [status, Jsonb(dict(summary)), final_input_hash,
                 Jsonb({"resolved_input_manifest": resolved_manifest}), run_id],
            )

    def register(self, spec: StrategySpec, *, status: str = "candidate", supersedes_id: int | None = None) -> int:
        family = "martingale" if is_martingale_family(
            spec.strategy_key, spec.mechanism_class, spec.name, spec.strategy_family,
        ) else spec.strategy_family
        authority_group = f"phase3:{spec.strategy_key}"
        implementation = IMPLEMENTATION_CATALOG.get(spec.implementation_id or "")
        executable = bool(
            implementation is not None
            and spec.implementation_version is not None
            and implementation.implementation_version == spec.implementation_version
        )
        with self.runtime.transaction(JOB_PROFILE) as connection:
            existing = connection.execute(
                """SELECT id, name, mechanism_class, economic_mechanism, falsification_rule,
                          source_definition_version, strategy_family, promotability,
                          actionability, p3_enabled, parameters, authority_group,
                          implementation_id, implementation_version, definition_blockers, supersedes_id
                     FROM analysis.strategy_revision
                    WHERE strategy_key = %s AND revision = %s""",
                [spec.strategy_key, spec.revision],
            ).fetchone()
            if existing is not None:
                if supersedes_id is not None:
                    if existing["supersedes_id"] != supersedes_id:
                        raise ValueError("strategy revision supersession identity conflicts")
                    self._validate_supersedes_parent(connection, spec, supersedes_id)
                manifest = connection.execute(
                    """SELECT source_definition_version, source_manifest, data_manifest,
                              cost_manifest, capacity_manifest, failure_manifest
                         FROM analysis.strategy_manifest WHERE strategy_revision_id = %s""",
                    [existing["id"]],
                ).fetchone()
                if manifest is None or (
                    existing["name"] != spec.name
                    or existing["mechanism_class"] != spec.mechanism_class
                    or existing["economic_mechanism"] != spec.economic_mechanism
                    or existing["falsification_rule"] != spec.falsification_rule
                    or existing["source_definition_version"] != spec.source_definition_version
                    or existing["strategy_family"] != family
                    or existing["promotability"] != spec.promotability
                    or existing["actionability"] != spec.actionability
                    or existing["p3_enabled"] is not executable
                    or existing["parameters"] != spec.parameters
                    or existing["authority_group"] != authority_group
                    or existing["implementation_id"] != spec.implementation_id
                    or existing["implementation_version"] != spec.implementation_version
                    or tuple(existing["definition_blockers"] or ()) != tuple(spec.blockers)
                    or manifest["source_definition_version"] != spec.source_definition_version
                    or any(manifest[f"{key}_manifest"] != spec.manifest[key] for key in MANIFEST_PARTS)
                ):
                    raise ValueError("strategy revision or manifest identity conflicts")
                return int(existing["id"])
            if supersedes_id is not None:
                self._validate_supersedes_parent(connection, spec, supersedes_id)
                connection.execute(
                    "UPDATE analysis.strategy_revision SET p3_enabled = false WHERE id = %s",
                    [supersedes_id],
                )
            revision = connection.execute(
                """INSERT INTO analysis.strategy_revision
                   (strategy_key, revision, name, status, parameters, supersedes_id,
                    mechanism_class, economic_mechanism, falsification_rule,
                    source_definition_version, strategy_family, promotability, actionability,
                    p3_enabled, authority_group, implementation_id, implementation_version, definition_blockers)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                [spec.strategy_key, spec.revision, spec.name, status, Jsonb(spec.parameters), supersedes_id,
                 spec.mechanism_class, spec.economic_mechanism, spec.falsification_rule,
                 spec.source_definition_version, family, spec.promotability, spec.actionability,
                 executable, authority_group, spec.implementation_id, spec.implementation_version, Jsonb(list(spec.blockers))],
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO analysis.strategy_manifest
                   (strategy_revision_id, source_definition_version, source_manifest,
                    data_manifest, cost_manifest, capacity_manifest, failure_manifest, manifest_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [revision, spec.source_definition_version, *[Jsonb(spec.manifest[key]) for key in MANIFEST_PARTS], "0" * 64],
            )
            return int(revision)

    def record_signal_evaluation(
        self,
        strategy_key: str,
        revision: int,
        signal: StrategySignal,
        *,
        scope: str | None = None,
        input_snapshot_identity: str | None = None,
        input_cutoff: datetime,
        mode: str,
    ) -> str:
        """Publish an advisory signal as immutable research evidence.

        This deliberately does not write ``strategy_forecast``: a factor or
        signal is not a qualified model artifact.
        """
        if mode not in {"research", "replay"}:
            raise ValueError("strategy signal publication mode is invalid")
        if input_cutoff.tzinfo is None:
            raise ValueError("strategy signal input cutoff must be timezone-aware")
        if signal.strategy_key != strategy_key:
            raise ValueError("strategy signal identity does not match strategy key")
        payload = signal.model_dump(mode="json")
        input_hash = content_hash({"strategy_key": strategy_key, "revision": revision, "scope": scope,
                                   "input_snapshot_identity": input_snapshot_identity,
                                   "cutoff": input_cutoff.isoformat(), "signal": payload})
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                "SELECT id, implementation_id, implementation_version FROM analysis.strategy_revision "
                "WHERE strategy_key = %s AND revision = %s",
                [strategy_key, revision],
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown PostgreSQL strategy key: {strategy_key}")
            connection.execute(
                """
                INSERT INTO analysis.strategy_evaluation
                    (strategy_revision_id, evaluation_type, evaluated_at, period_start,
                     period_end, verdict, metrics, evidence, input_hash, lineage)
                VALUES (%s, 'strategy_signal', statement_timestamp(), %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    row["id"], input_cutoff, input_cutoff, signal.status,
                    Jsonb({"value": signal.value, "direction": signal.direction,
                           "actionability": signal.actionability, "horizon": signal.horizon,
                           "regime": signal.regime}),
                    Jsonb([signal.evidence]), input_hash,
                           Jsonb({"mode": mode, "scope": scope, "input_snapshot_identity": input_snapshot_identity,
                           "strategy_key": strategy_key, "revision": revision,
                           "implementation_id": row["implementation_id"],
                           "implementation_version": row["implementation_version"],
                           "blockers": list(signal.blockers)}),
                ],
            )
        return input_hash

    def record_signal_evaluation_record(
        self,
        strategy_key: str,
        revision: int,
        signal: StrategySignal,
        *,
        run_id: str,
        scope: str,
        input_snapshot_identity: str | None,
        input_cutoff: datetime,
        mode: str,
        input_manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist one immutable signal with separate input and output identities."""
        if mode not in {"research", "replay"}:
            raise ValueError("strategy signal publication mode is invalid")
        if input_cutoff.tzinfo is None:
            raise ValueError("strategy signal input cutoff must be timezone-aware")
        if signal.strategy_key != strategy_key:
            raise ValueError("strategy signal identity does not match strategy key")
        payload = signal.model_dump(mode="json")
        output_hash = content_hash(payload)
        input_hash = content_hash({
            "strategy_key": strategy_key, "revision": revision, "scope": scope,
            "input_snapshot_identity": input_snapshot_identity,
            "cutoff": input_cutoff.isoformat(), "mode": mode, "manifest": input_manifest,
        })
        with self.runtime.transaction(JOB_PROFILE) as connection:
            run = connection.execute(
                """SELECT run_type, status, input_cutoff, inputs
                     FROM analysis.run
                    WHERE id = %s
                    FOR UPDATE""",
                [run_id],
            ).fetchone()
            if run is None or run["run_type"] != "strategy_research" or run["status"] != "running":
                raise ValueError("strategy evaluation requires a running strategy research run")
            if run["input_cutoff"] != input_cutoff:
                raise ValueError("strategy evaluation cutoff does not match its run")
            run_inputs = run["inputs"] if isinstance(run["inputs"], Mapping) else {}
            if run_inputs.get("mode") != mode:
                raise ValueError("strategy evaluation mode does not match its run")
            planned_revisions = {
                (str(item[0]), int(item[1]))
                for item in run_inputs.get("strategy_revisions", ())
                if isinstance(item, (list, tuple)) and len(item) == 2
            }
            if (strategy_key, revision) not in planned_revisions:
                raise ValueError("strategy evaluation revision is not part of its run")
            if scope not in {str(item) for item in run_inputs.get("scopes", ())}:
                raise ValueError("strategy evaluation scope is not part of its run")
            row = connection.execute(
                """SELECT id, implementation_id, implementation_version
                     FROM analysis.strategy_revision
                    WHERE strategy_key = %s AND revision = %s""",
                [strategy_key, revision],
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown PostgreSQL strategy key: {strategy_key}")
            existing = connection.execute(
                """INSERT INTO analysis.strategy_evaluation
                    (strategy_revision_id, evaluation_type, evaluated_at, period_start,
                     period_end, verdict, metrics, evidence, input_hash, lineage,
                     run_id, scope, mode, input_manifest, output_hash)
                   VALUES (%s, 'strategy_signal', clock_timestamp(), %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s)
                   ON CONFLICT (run_id, strategy_revision_id, scope, mode, input_hash) WHERE run_id IS NOT NULL DO NOTHING
                RETURNING id, evaluated_at, available_at""",
                [row["id"], input_cutoff, input_cutoff, signal.status,
                 Jsonb({"value": signal.value, "direction": signal.direction,
                        "actionability": signal.actionability, "horizon": signal.horizon,
                        "regime": signal.regime}),
                 Jsonb(signal.evidence), input_hash,
                 Jsonb({"mode": mode, "scope": scope, "input_snapshot_identity": input_snapshot_identity,
                        "strategy_key": strategy_key, "revision": revision,
                        "implementation_id": row["implementation_id"],
                        "implementation_version": row["implementation_version"],
                        "blockers": list(signal.blockers)}), run_id, scope, mode,
                 Jsonb(dict(input_manifest)), output_hash],
            ).fetchone()
            if existing is None:
                existing = connection.execute(
                    """SELECT id, evaluated_at, available_at, output_hash
                         FROM analysis.strategy_evaluation
                        WHERE run_id = %s AND strategy_revision_id = %s
                          AND scope = %s AND mode = %s AND input_hash = %s""",
                    [run_id, row["id"], scope, mode, input_hash],
                ).fetchone()
                if existing is None or existing.get("output_hash") != output_hash:
                    raise ValueError("strategy evaluation retry identity conflicts")
            return {
                "evaluation_id": str(existing["id"]),
                "evaluated_at": existing["evaluated_at"],
                "available_at": existing["available_at"],
                "input_hash": input_hash,
                "output_hash": output_hash,
            }

    def record_pnl_tape(self, rows: Iterable[Mapping[str, Any]]) -> int:
        records = tuple(rows)
        if len(records) > 10_000:
            raise ValueError("strategy P&L tape exceeds bound")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for row in records:
                _require_pit(row["input_cutoff"], row["available_at"])
                _require_result_pit(connection, row["research_trial_id"], row["trial_result_id"], row["input_cutoff"])
                connection.execute(
                    """INSERT INTO analysis.strategy_pnl_tape
                       (strategy_revision_id, instrument_id, pnl_date, strategy_forecast_id,
                        research_trial_id, trial_result_id, universe_manifest_hash, result_hash,
                        input_cutoff, gross_return, cost, net_return, tail_return, regime,
                        observed_at, available_at, input_hash, metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (strategy_revision_id, instrument_id, pnl_date, input_hash) DO NOTHING""",
                    [row["strategy_revision_id"], row["instrument_id"], row["pnl_date"], row["strategy_forecast_id"],
                     row["research_trial_id"], row["trial_result_id"], row["universe_manifest_hash"], row["result_hash"], row["input_cutoff"],
                     row.get("gross_return"), row.get("cost"), row.get("net_return"), row.get("tail_return"),
                     row.get("regime"), row["observed_at"], row["available_at"], "0" * 64, Jsonb(dict(row.get("metadata") or {}))],
                )
        return len(records)

    def record_monitoring(self, *, strategy_revision_id: int, research_trial_id: Any, trial_result_id: Any, universe_manifest_hash: str, result_hash: str, evidence_kind: str, input_cutoff: Any, observed_at: Any, available_at: Any, input_hash: str = "", metrics: Mapping[str, Any] | None = None, evidence: Mapping[str, Any] | None = None) -> None:
        if evidence_kind not in {"correlation", "tail_correlation", "crowding", "capacity", "decay", "regime"}:
            raise ValueError("unknown strategy monitoring evidence kind")
        _require_pit(input_cutoff, available_at)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            _require_result_pit(connection, research_trial_id, trial_result_id, input_cutoff)
            connection.execute(
                """INSERT INTO analysis.strategy_monitoring_evidence
                   (strategy_revision_id, research_trial_id, trial_result_id, universe_manifest_hash,
                    result_hash, evidence_kind, input_cutoff, observed_at, available_at, input_hash, metrics, evidence)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (strategy_revision_id, evidence_kind, input_cutoff, input_hash) DO NOTHING""",
                [strategy_revision_id, research_trial_id, trial_result_id, universe_manifest_hash, result_hash,
                 evidence_kind, input_cutoff, observed_at, available_at, "0" * 64,
                 Jsonb(dict(metrics or {})), Jsonb(dict(evidence or {}))],
            )

    def record_comparison(self, *, champion_revision_id: int, challenger_revision_id: int, champion_trial_id: Any, challenger_trial_id: Any, champion_result_id: Any, challenger_result_id: Any, champion_result_hash: str, challenger_result_hash: str, champion_manifest_hash: str, challenger_manifest_hash: str, input_cutoff: Any, observed_at: Any, available_at: Any, input_hash: str = "", distinctness: str = "inconclusive", explanation: str = "", metrics: Mapping[str, Any] | None = None) -> None:
        if champion_revision_id == challenger_revision_id:
            raise ValueError("champion and challenger must be different revisions")
        _require_pit(input_cutoff, available_at)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            _require_result_pit(connection, champion_trial_id, champion_result_id, input_cutoff)
            _require_result_pit(connection, challenger_trial_id, challenger_result_id, input_cutoff)
            connection.execute(
                """INSERT INTO analysis.strategy_comparison
                   (champion_revision_id, challenger_revision_id, champion_trial_id, challenger_trial_id,
                    champion_result_id, challenger_result_id, champion_result_hash, challenger_result_hash,
                    champion_manifest_hash, challenger_manifest_hash, input_cutoff, observed_at, available_at,
                    input_hash, distinctness, explanation, metrics)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (champion_revision_id, challenger_revision_id, input_cutoff, input_hash) DO NOTHING""",
                [champion_revision_id, challenger_revision_id, champion_trial_id, challenger_trial_id, champion_result_id,
                 challenger_result_id, champion_result_hash, challenger_result_hash, champion_manifest_hash,
                 challenger_manifest_hash, input_cutoff, observed_at, available_at, "0" * 64,
                 distinctness, explanation or "caller value ignored", Jsonb(dict(metrics or {}))],
            )

    def resolve(self, strategy_key: str, revision: int | None = None) -> StrategySpec:
        with self.runtime.read() as connection:
            row = connection.execute(
                """SELECT revision.id, revision.strategy_key, revision.revision, revision.name,
                          revision.mechanism_class, revision.economic_mechanism,
                          revision.falsification_rule, revision.source_definition_version,
                          revision.strategy_family, revision.promotability, revision.actionability,
                          revision.parameters, manifest.source_manifest, manifest.data_manifest,
                          manifest.cost_manifest, manifest.capacity_manifest, manifest.failure_manifest,
                          revision.implementation_id, revision.implementation_version,
                          revision.p3_enabled,
                          revision.definition_blockers
                     FROM analysis.strategy_revision revision
                     JOIN analysis.strategy_manifest manifest ON manifest.strategy_revision_id = revision.id
                    WHERE revision.strategy_key = %s
                      AND (%s::integer IS NULL OR revision.revision = %s::integer)
                    ORDER BY revision.revision DESC LIMIT 1""",
                [strategy_key, revision, revision],
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown PostgreSQL strategy key: {strategy_key}")
        values = {
            "strategy_key": row["strategy_key"], "revision": row["revision"], "name": row["name"],
            "mechanism_class": row["mechanism_class"], "economic_mechanism": row["economic_mechanism"],
            "falsification_rule": row["falsification_rule"], "source_definition_version": row["source_definition_version"],
            "strategy_family": row["strategy_family"], "promotability": row["promotability"], "actionability": row["actionability"],
            "enabled": bool(row["p3_enabled"]),
            "parameters": row["parameters"], "manifest": {key: row[f"{key}_manifest"] for key in MANIFEST_PARTS},
            "implementation_id": row["implementation_id"],
            "implementation_version": row["implementation_version"],
            "blockers": tuple(row["definition_blockers"] or ()),
        }
        return StrategySpec(**values)

    def promote(self, strategy_revision_id: int) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("SELECT analysis.promote_phase3_strategy(%s)", [strategy_revision_id])

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
