"""Child-process boundary helpers for PostgreSQL-backed agents."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
import shlex
import shutil
import sys
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo


def agent_env(
    *,
    provider: str,
    model: str,
    reasoning_effort: str,
    timeout_seconds: int | None = None,
) -> dict[str, str]:
    values: dict[str, str] = {}
    selected_provider = provider.strip().lower()
    if selected_provider:
        values["MARKET_OPTION_AGENT_PROVIDER"] = selected_provider
    if model:
        if selected_provider == "deepseek":
            values["MARKET_DEEPSEEK_MODEL"] = model
        else:
            values["MARKET_CODEX_MODEL"] = model
    if reasoning_effort:
        if selected_provider == "deepseek":
            values["MARKET_DEEPSEEK_REASONING_EFFORT"] = reasoning_effort
        else:
            values["MARKET_CODEX_REASONING_EFFORT"] = reasoning_effort
    if timeout_seconds is not None:
        child_timeout = str(max(30, int(timeout_seconds) - 15))
        if selected_provider == "deepseek":
            values["MARKET_DEEPSEEK_TIMEOUT_SECONDS"] = child_timeout
        else:
            values["MARKET_CODEX_TIMEOUT_SECONDS"] = child_timeout
    return values


def agent_error_meta(detail: str) -> dict[str, Any]:
    for line in str(detail).splitlines():
        if not line.startswith("MARKET_AGENT_META="):
            continue
        try:
            value = json.loads(line.removeprefix("MARKET_AGENT_META="))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def command_args(command: str) -> list[str]:
    args = shlex.split(command)
    if not args:
        raise ValueError("agent command is empty")
    executable = args[0]
    if "/" not in executable and shutil.which(executable) is None:
        # Keep the virtualenv path itself; resolving its Python symlink jumps to
        # uv's shared interpreter directory where console scripts do not live.
        local_executable = Path(sys.executable).parent / executable
        if local_executable.is_file():
            args[0] = str(local_executable)
    return args


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


def _numeric_value(value: Any, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def validate_result(
    task_kind: str,
    payload: dict[str, Any],
    *,
    request: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> None:
    if task_kind == "option_thesis":
        required = {
            "ticker": str(payload.get("ticker") or "").strip().upper(),
            "core_thesis": str(payload.get("core_thesis") or payload.get("thesis") or "").strip(),
            "bull_target_date": str(payload.get("bull_target_date") or "").strip(),
            "bear_case": str(payload.get("bear_case") or "").strip(),
        }
        missing = [key for key, value in required.items() if not value]
        targets: dict[str, float] = {}
        for key in ("bull_target_price", "base_target_price", "bear_target_price"):
            value = _numeric_value(payload.get(key), 0.0)
            targets[key] = value
            if not math.isfinite(value) or value <= 0:
                missing.append(key)
        if not (
            targets["bull_target_price"] > targets["base_target_price"] > targets["bear_target_price"]
        ):
            missing.append("scenario_target_order")
        for key in ("required_proofs", "catalysts", "invalidation", "preferred_structures"):
            if not isinstance(payload.get(key), list) or not payload[key]:
                missing.append(key)
        direction = str(payload.get("direction") or "").lower()
        if direction not in {"long", "short"}:
            missing.append("direction")
        allowed_structures = {
            "long_call", "long_put", "call_debit_spread", "put_debit_spread", "cash_secured_put",
        }
        structures = {str(item) for item in payload.get("preferred_structures") or []}
        if structures - allowed_structures:
            missing.append("preferred_structures")
        direction_structures = {
            "long": {"long_call", "call_debit_spread", "cash_secured_put"},
            "short": {"long_put", "put_debit_spread"},
        }
        if direction in direction_structures and not structures.issubset(direction_structures[direction]):
            missing.append("preferred_structures_direction")
        confidence = _numeric_value(payload.get("confidence"), -1.0)
        if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
            missing.append("confidence")
        try:
            datetime.fromisoformat(required["bull_target_date"])
        except ValueError:
            missing.append("bull_target_date")
        probabilities = payload.get("scenario_probabilities")
        if not isinstance(probabilities, dict):
            missing.append("scenario_probabilities")
        else:
            try:
                values = [_numeric_value(probabilities[key], float("nan")) for key in ("base", "bull", "bear")]
            except KeyError:
                values = []
            if (
                len(values) != 3
                or any(not math.isfinite(value) or value < 0 or value > 1 for value in values)
                or abs(sum(values) - 1.0) > 0.02
            ):
                missing.append("scenario_probabilities")
        if missing:
            raise ValueError(f"agent thesis missing or invalid required fields: {', '.join(sorted(set(missing)))}")
        if request is not None:
            _validate_thesis_identity(payload, request, task_id)
    elif task_kind == "option_postmortem":
        if not str(payload.get("failure_type") or payload.get("outcome_type") or "").strip():
            raise ValueError("agent postmortem requires failure_type or outcome_type")


def _validate_thesis_identity(
    payload: dict[str, Any], request: dict[str, Any], task_id: str | None,
) -> None:
    envelope = request.get("request_envelope")
    envelope = envelope if isinstance(envelope, dict) else {}
    expected_id = str(
        task_id or request.get("request_id") or envelope.get("request_id") or envelope.get("request") or ""
    ).strip()
    if not expected_id:
        raise ValueError("agent thesis request identity is unavailable")
    for stored_id in (request.get("request_id"), envelope.get("request_id"), envelope.get("request")):
        if stored_id and str(stored_id).strip() != expected_id:
            raise ValueError("agent thesis request identity mismatch")
    request_ticker = str(request.get("ticker") or "").strip().upper()
    envelope_ticker = str(envelope.get("ticker") or "").strip().upper()
    if request_ticker and envelope_ticker and request_ticker != envelope_ticker:
        raise ValueError("agent thesis ticker identity mismatch")
    ticker = request_ticker or envelope_ticker
    if str(payload.get("ticker") or "").strip().upper() != ticker:
        raise ValueError(f"agent thesis ticker mismatch: expected {ticker}")
    supplied_task = str(payload.get("task_kind") or payload.get("task") or "").strip()
    if supplied_task and supplied_task != "option_thesis":
        raise ValueError("agent thesis task identity mismatch")
    if envelope.get("task") and str(envelope["task"]).strip() != "option_thesis":
        raise ValueError("agent thesis task identity mismatch")
    supplied_id = str(payload.get("request_id") or "").strip()
    references = payload.get("evidence_refs")
    if not isinstance(references, list):
        raise ValueError("agent thesis evidence_refs must be an array")
    normalized: list[tuple[str, str]] = []
    for reference in references:
        if not isinstance(reference, dict):
            raise ValueError("agent thesis evidence reference must be an object")
        reference_type = str(reference.get("type") or "").strip()
        reference_id = str(reference.get("id") or "").strip()
        if not reference_type or not reference_id:
            raise ValueError("agent thesis evidence reference is malformed")
        normalized.append((reference_type, reference_id))
    if supplied_id and supplied_id != expected_id:
        raise ValueError("agent thesis request identity mismatch")
    if supplied_id != expected_id and ("agent_request", expected_id) not in normalized:
        raise ValueError("agent thesis request identity is missing")
    request_decision = str(
        request.get("decision_id") or (request.get("decision") or {}).get("id") or ""
    ).strip()
    envelope_decision = str(envelope.get("decision_id") or "").strip()
    if request_decision and envelope_decision and request_decision != envelope_decision:
        raise ValueError("agent thesis decision identity mismatch")
    expected_decision = request_decision or envelope_decision
    supplied_decision = str(payload.get("decision_id") or "").strip()
    if supplied_decision and supplied_decision != expected_decision:
        raise ValueError("agent thesis decision identity mismatch")
    allowed = {
        (str(item.get("type") or ""), str(item.get("id") or ""))
        for item in list(envelope.get("evidence_refs") or [])
        if isinstance(item, dict) and item.get("type") and item.get("id")
    }
    if not set(normalized) <= allowed:
        raise ValueError("agent thesis references unknown or unavailable evidence")


def market_day_start_utc(now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo("America/New_York"))
    return datetime.combine(local.date(), time.min, tzinfo=local.tzinfo).astimezone(UTC)
