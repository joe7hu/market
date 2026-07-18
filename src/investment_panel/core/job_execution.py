"""One process-execution contract for every refresh-job adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Awaitable, Callable, Mapping

from investment_panel.core.job_policy import job_timeout_seconds


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = PROJECT_ROOT / "src"
FailureHandler = Callable[[str], dict[str, Any]]
AsyncFailureHandler = Callable[[str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class RefreshProcessSpec:
    job_id: str
    job_name: str
    database_url: str
    config_path: str = "config.yaml"
    database_reference: str | None = None
    python_executable: str = field(default_factory=lambda: sys.executable)


def process_command(spec: RefreshProcessSpec) -> list[str]:
    command = [
        spec.python_executable,
        "-m",
        "investment_panel.core.refresh_jobs",
        spec.job_name,
        "--job-id",
        spec.job_id,
    ]
    if spec.database_reference is not None:
        command.extend(["--db-path", spec.database_reference])
    command.extend(["--config", spec.config_path])
    return command


def process_environment(spec: RefreshProcessSpec, base: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment.update(
        {
            "MARKET_DATABASE_URL": spec.database_url,
            "PYTHONPATH": os.pathsep.join(
                part for part in (str(SOURCE_ROOT), existing_pythonpath) if part
            ),
        }
    )
    return environment


def execute_sync(
    spec: RefreshProcessSpec,
    on_failure: FailureHandler,
    *,
    timeout_overrides: Mapping[str, int] | None = None,
    run_process: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    command = process_command(spec)
    timeout_seconds = job_timeout_seconds(spec.job_name, timeout_overrides)
    try:
        completed = run_process(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=process_environment(spec),
            cwd=PROJECT_ROOT,
        )
    except subprocess.TimeoutExpired:
        return on_failure(f"refresh subprocess timed out after {timeout_seconds}s")
    return _completed_result(
        spec,
        int(completed.returncode),
        completed.stdout or "",
        completed.stderr or "",
        on_failure,
    )


async def execute_async(
    spec: RefreshProcessSpec,
    on_failure: AsyncFailureHandler,
    *,
    timeout_overrides: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        *process_command(spec),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=process_environment(spec),
        cwd=PROJECT_ROOT,
    )
    timeout_seconds = job_timeout_seconds(spec.job_name, timeout_overrides)
    try:
        communicate = proc.communicate()
        if timeout_seconds is not None:
            stdout, stderr = await asyncio.wait_for(communicate, timeout=timeout_seconds)
        else:
            stdout, stderr = await communicate
    except asyncio.TimeoutError:
        await terminate_process(proc)
        return await on_failure(f"refresh subprocess timed out after {timeout_seconds}s")
    except asyncio.CancelledError:
        await terminate_process(proc)
        await on_failure("refresh subprocess cancelled during scheduler shutdown/reload")
        raise

    def fail(error: str) -> dict[str, Any]:
        raise _AsyncFailure(error)

    try:
        return _completed_result(
            spec,
            int(proc.returncode or 0),
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            fail,
        )
    except _AsyncFailure as failure:
        return await on_failure(str(failure))


async def terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


class _AsyncFailure(RuntimeError):
    pass


def _completed_result(
    spec: RefreshProcessSpec,
    returncode: int,
    stdout: str,
    stderr: str,
    on_failure: FailureHandler,
) -> dict[str, Any]:
    stdout_text = stdout.strip()
    stderr_text = stderr.strip()
    if returncode != 0:
        detail = stderr_text or stdout_text or f"refresh subprocess exited with code {returncode}"
        return on_failure(f"refresh subprocess exited with code {returncode}: {detail[-2000:]}")
    try:
        return json.loads(stdout_text)
    except json.JSONDecodeError:
        return {
            "id": spec.job_id,
            "job_name": spec.job_name,
            "status": "succeeded",
            "summary": {"stdout": stdout_text[-2000:]},
        }
