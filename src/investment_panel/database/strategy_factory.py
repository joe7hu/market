"""PostgreSQL owner for Phase 3 strategy definitions and evidence tapes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from psycopg.types.json import Jsonb

from investment_panel.core.strategy_factory import (
    MANIFEST_PARTS,
    StrategySignal,
    StrategySpec,
    daily_gap_regime,
    daily_trend_underreaction,
    event_propagation,
    is_martingale_family,
    options_recovery_v2,
)
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


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

    def register(self, spec: StrategySpec, *, status: str = "candidate", supersedes_id: int | None = None) -> int:
        family = "martingale" if is_martingale_family(
            spec.strategy_key, spec.mechanism_class, spec.name, spec.strategy_family,
        ) else spec.strategy_family
        authority_group = f"phase3:{spec.strategy_key}"
        with self.runtime.transaction(JOB_PROFILE) as connection:
            existing = connection.execute(
                """SELECT id, name, mechanism_class, economic_mechanism, falsification_rule,
                          source_definition_version, strategy_family, promotability,
                          actionability, p3_enabled, parameters, authority_group
                     FROM analysis.strategy_revision
                    WHERE strategy_key = %s AND revision = %s""",
                [spec.strategy_key, spec.revision],
            ).fetchone()
            if existing is not None:
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
                    or existing["p3_enabled"] is not True
                    or existing["parameters"] != spec.parameters
                    or existing["authority_group"] != authority_group
                    or manifest["source_definition_version"] != spec.source_definition_version
                    or any(manifest[f"{key}_manifest"] != spec.manifest[key] for key in MANIFEST_PARTS)
                ):
                    raise ValueError("strategy revision or manifest identity conflicts")
                return int(existing["id"])
            revision = connection.execute(
                """INSERT INTO analysis.strategy_revision
                   (strategy_key, revision, name, status, parameters, supersedes_id,
                    mechanism_class, economic_mechanism, falsification_rule,
                    source_definition_version, strategy_family, promotability, actionability,
                    p3_enabled, authority_group)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s)
                   RETURNING id""",
                [spec.strategy_key, spec.revision, spec.name, status, Jsonb(spec.parameters), supersedes_id,
                 spec.mechanism_class, spec.economic_mechanism, spec.falsification_rule,
                 spec.source_definition_version, family, spec.promotability, spec.actionability,
                 authority_group],
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO analysis.strategy_manifest
                   (strategy_revision_id, source_definition_version, source_manifest,
                    data_manifest, cost_manifest, capacity_manifest, failure_manifest, manifest_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [revision, spec.source_definition_version, *[Jsonb(spec.manifest[key]) for key in MANIFEST_PARTS], "0" * 64],
            )
            return int(revision)

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
                          manifest.cost_manifest, manifest.capacity_manifest, manifest.failure_manifest
                     FROM analysis.strategy_revision revision
                     JOIN analysis.strategy_manifest manifest ON manifest.strategy_revision_id = revision.id
                    WHERE revision.strategy_key = %s
                      AND (%s::integer IS NULL OR revision.revision = %s::integer)
                    ORDER BY revision.revision DESC LIMIT 1""",
                [strategy_key, revision, revision],
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown PostgreSQL strategy key: {strategy_key}")
        return StrategySpec(
            strategy_key=row["strategy_key"], revision=row["revision"], name=row["name"],
            mechanism_class=row["mechanism_class"], economic_mechanism=row["economic_mechanism"],
            falsification_rule=row["falsification_rule"], source_definition_version=row["source_definition_version"],
            strategy_family=row["strategy_family"], promotability=row["promotability"], actionability=row["actionability"],
            parameters=row["parameters"], manifest={key: row[f"{key}_manifest"] for key in MANIFEST_PARTS},
        )

    def forecast(self, strategy_key: str, inputs: Mapping[str, Any], *, revision: int | None = None) -> StrategySignal:
        spec = self.resolve(strategy_key, revision)
        if is_martingale_family(spec.strategy_key, spec.mechanism_class, spec.name, spec.strategy_family):
            return StrategySignal(strategy_key=spec.strategy_key, status="blocked", actionability="research_only", blockers=("permanent_negative_control",))
        handlers = {
            "trend_underreaction": daily_trend_underreaction,
            "gap_regime": daily_gap_regime,
            "event_propagation": event_propagation,
            "options_recovery": options_recovery_v2,
        }
        handler = handlers.get(spec.mechanism_class)
        if handler is None:
            return StrategySignal(strategy_key=spec.strategy_key, status="blocked", actionability=spec.actionability, blockers=("strategy_handler_unregistered",))
        return handler(inputs, strategy_key=spec.strategy_key)

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
