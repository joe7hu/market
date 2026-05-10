"""OpenCLI runner with a small JSON-first interface."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


class OpenCliError(RuntimeError):
    """Raised when an OpenCLI command cannot return structured JSON."""


@dataclass(frozen=True)
class OpenCliRunner:
    command: str = "opencli"
    timeout_seconds: int = 25

    def read_json(self, args: list[str]) -> Any:
        command = [self.command, *args, "-f", "json"]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise OpenCliError(f"OpenCLI command not found: {self.command}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OpenCliError(f"OpenCLI timed out after {self.timeout_seconds}s: {' '.join(command)}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise OpenCliError(detail or f"OpenCLI exited {completed.returncode}: {' '.join(command)}")
        try:
            return json.loads(completed.stdout or "null")
        except json.JSONDecodeError as exc:
            raise OpenCliError(f"OpenCLI returned non-JSON output for {' '.join(command)}") from exc


def ensure_list(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []
