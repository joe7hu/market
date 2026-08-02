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


def agent_env(*, provider: str, model: str, reasoning_effort: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if provider:
        values["MARKET_OPTION_AGENT_PROVIDER"] = provider
    if model:
        values["MARKET_CODEX_MODEL"] = model
        values["MARKET_OPENAI_MODEL"] = model
    if reasoning_effort:
        values["MARKET_CODEX_REASONING_EFFORT"] = reasoning_effort
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
        thesis = str(payload.get("core_thesis") or payload.get("thesis") or "").strip()
        if not thesis:
            raise ValueError("agent thesis requires core_thesis")
    elif task_kind == "option_postmortem":
        if not str(payload.get("failure_type") or payload.get("outcome_type") or "").strip():
            raise ValueError("agent postmortem requires failure_type or outcome_type")


def market_day_start_utc(now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo("America/New_York"))
    return datetime.combine(local.date(), time.min, tzinfo=local.tzinfo).astimezone(UTC)
