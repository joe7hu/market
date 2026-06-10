"""OpenCLI runner with a small JSON-first interface."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any


class OpenCliError(RuntimeError):
    """Raised when an OpenCLI command cannot return structured JSON."""


class OpenCliRateLimitError(OpenCliError):
    """Raised when OpenCLI reports an upstream rate limit (HTTP 429)."""


# Matches the upstream rate-limit signal regardless of surrounding text, e.g.
# "scanner 429:", "Too Many Requests", "rate limit exceeded".
_RATE_LIMIT_PATTERN = re.compile(r"\b429\b|too many requests|rate limit", re.IGNORECASE)


def _is_rate_limited(detail: str) -> bool:
    return bool(_RATE_LIMIT_PATTERN.search(detail or ""))


@dataclass(frozen=True)
class OpenCliRunner:
    command: str = "opencli"
    timeout_seconds: int = 25
    # Bounded retry with exponential backoff on upstream rate limits (429).
    # TradingView's scanner endpoint is shared by quotes, screener, and options,
    # so a burst of option-chain calls can trip the limiter; a short backoff
    # rides out a transient limit without dragging the whole refresh. Kept small
    # (1.5s, 3s) because the free_sources options loop also has a run-level
    # circuit breaker for *sustained* limits.
    max_rate_limit_retries: int = 2
    rate_limit_backoff_seconds: float = 1.5

    def read_json(self, args: list[str]) -> Any:
        command = [self.command, *args, "-f", "json"]
        attempt = 0
        while True:
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
                message = detail or f"OpenCLI exited {completed.returncode}: {' '.join(command)}"
                if _is_rate_limited(message) and attempt < self.max_rate_limit_retries:
                    # Exponential backoff: 2s, 4s, 8s. Bounded so a sustained
                    # outage still surfaces an error to the caller's handler.
                    time.sleep(self.rate_limit_backoff_seconds * (2**attempt))
                    attempt += 1
                    continue
                if _is_rate_limited(message):
                    raise OpenCliRateLimitError(message)
                raise OpenCliError(message)
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
