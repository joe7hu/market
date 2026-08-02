"""Child-process boundary helpers for PostgreSQL-backed agents."""

from __future__ import annotations

import json
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
    if provider:
        values["MARKET_OPTION_AGENT_PROVIDER"] = provider
    if model:
        values["MARKET_CODEX_MODEL"] = model
        values["MARKET_OPENAI_MODEL"] = model
    if reasoning_effort:
        values["MARKET_CODEX_REASONING_EFFORT"] = reasoning_effort
    if timeout_seconds is not None:
        values["MARKET_CODEX_TIMEOUT_SECONDS"] = str(max(30, int(timeout_seconds) - 15))
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


def validate_result(task_kind: str, payload: dict[str, Any]) -> None:
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
            try:
                value = float(payload.get(key))
            except (TypeError, ValueError):
                value = 0.0
            targets[key] = value
            if value <= 0:
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
        try:
            confidence = float(payload.get("confidence"))
        except (TypeError, ValueError):
            confidence = -1.0
        if confidence < 0 or confidence > 1:
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
                values = [float(probabilities[key]) for key in ("base", "bull", "bear")]
            except (KeyError, TypeError, ValueError):
                values = []
            if len(values) != 3 or any(value < 0 or value > 1 for value in values) or abs(sum(values) - 1.0) > 0.02:
                missing.append("scenario_probabilities")
        if missing:
            raise ValueError(f"agent thesis missing or invalid required fields: {', '.join(sorted(set(missing)))}")
    elif task_kind == "option_postmortem":
        if not str(payload.get("failure_type") or payload.get("outcome_type") or "").strip():
            raise ValueError("agent postmortem requires failure_type or outcome_type")


def market_day_start_utc(now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo("America/New_York"))
    return datetime.combine(local.date(), time.min, tzinfo=local.tzinfo).astimezone(UTC)
