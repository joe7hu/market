"""Persistence helpers for thesis-monitor automation runs."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.instruments import canonical_symbol
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class ThesisAutomationRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def eligible(
        self,
        symbol: str,
        *,
        trigger: str,
        debounce_minutes: int,
        max_material_runs_per_day: int,
        force: bool,
    ) -> tuple[bool, str]:
        if force or trigger == "preopen":
            return True, "eligible"
        normalized = canonical_symbol(symbol)
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                SELECT instrument.id FROM catalog.instrument instrument
                WHERE instrument.symbol = %s
                """,
                [normalized],
            ).fetchone()
            if row is None:
                return True, "new_symbol"
            recent = connection.execute(
                """
                SELECT count(*) AS count
                FROM app.thesis_automation_run
                WHERE instrument_id = %s
                  AND trigger = %s
                  AND started_at >= now() - (%s || ' minutes')::interval
                """,
                [row["id"], trigger, debounce_minutes],
            ).fetchone()
            if int(recent["count"] or 0):
                return False, "debounced"
            today = connection.execute(
                """
                SELECT count(*) AS count
                FROM app.thesis_automation_run
                WHERE instrument_id = %s
                  AND trigger = %s
                  AND started_at::date = (now() AT TIME ZONE 'America/New_York')::date
                  AND status IN ('succeeded', 'failed', 'timeout')
                """,
                [row["id"], trigger],
            ).fetchone()
            if int(today["count"] or 0) >= max_material_runs_per_day:
                return False, "daily_cap"
        return True, "eligible"

    def start_run(
        self,
        symbol: str,
        *,
        trigger: str,
        model: str,
        reasoning_effort: str,
        prompt_version: str,
        evidence_snapshot: list[dict[str, Any]],
        status: str = "running",
    ) -> str:
        normalized = canonical_symbol(symbol)
        fingerprint = evidence_fingerprint(evidence_snapshot)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            instrument = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [normalized]).fetchone()
            run = connection.execute(
                """
                INSERT INTO app.thesis_automation_run (
                    instrument_id, trigger, model, reasoning_effort, prompt_version,
                    evidence_fingerprint, evidence_snapshot, input_symbol, status, started_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                RETURNING id
                """,
                [
                    instrument["id"] if instrument else None,
                    trigger,
                    model,
                    reasoning_effort,
                    prompt_version,
                    fingerprint,
                    Jsonb(_jsonable(evidence_snapshot)),
                    normalized,
                    status,
                ],
            ).fetchone()
        return str(run["id"])

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
        cost_usd: float | None = None,
    ) -> None:
        usage = usage or {}
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                UPDATE app.thesis_automation_run
                SET status = %s, error = %s, finished_at = now(),
                    input_tokens = %s, output_tokens = %s, cost_usd = %s
                WHERE id = %s
                """,
                [
                    status,
                    error,
                    int(usage.get("input_tokens") or 0) or None,
                    int(usage.get("output_tokens") or 0) or None,
                    cost_usd,
                    run_id,
                ],
            )

    def store_assessments(
        self,
        symbol: str,
        *,
        revision_id: int,
        run_id: str,
        assessments: list[dict[str, Any]],
    ) -> int:
        if not assessments:
            return 0
        normalized = canonical_symbol(symbol)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            instrument = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [normalized]).fetchone()
            if instrument is None:
                return 0
            for item in assessments:
                connection.execute(
                    """
                    INSERT INTO app.thesis_evidence_assessment (
                        thesis_revision_id, automation_run_id, instrument_id,
                        evidence_reference, evidence_title, evidence_date, stance,
                        materiality, affected_pillar_ids, confidence, rationale
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        revision_id,
                        run_id,
                        instrument["id"],
                        str(item["evidence_reference"]),
                        item.get("evidence_title"),
                        item.get("evidence_date"),
                        str(item.get("stance") or "neutral"),
                        str(item.get("materiality") or "low"),
                        list(item.get("affected_pillar_ids") or []),
                        float(item.get("confidence") or 0),
                        str(item.get("rationale") or ""),
                    ],
                )
        return len(assessments)

    def create_health_alert(self, symbol: str, *, title: str, detail: str) -> None:
        normalized = canonical_symbol(symbol)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            instrument = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [normalized]).fetchone()
            connection.execute(
                """
                INSERT INTO app.alert (instrument_id, alert_type, severity, title, detail)
                VALUES (%s, 'thesis_automation_health', 'warning', %s, %s)
                """,
                [instrument["id"] if instrument else None, title, detail],
            )


def evidence_fingerprint(evidence_snapshot: list[dict[str, Any]]) -> str:
    stable = json.dumps(evidence_snapshot, sort_keys=True, default=str)
    return hashlib.sha256(stable.encode()).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    return value
