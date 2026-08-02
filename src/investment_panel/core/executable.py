"""Resolve local command-line adapters consistently under shells and launchd."""

from __future__ import annotations

import os
from pathlib import Path
import shutil


DEFAULT_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def resolve_executable(
    command: str,
    *,
    env_var: str | None = None,
    candidates: tuple[str, ...] = (),
) -> str:
    """Return an executable path without depending on the caller's PATH."""

    configured = os.environ.get(env_var, "").strip() if env_var else ""
    if configured:
        return configured
    requested = str(command or "").strip()
    if not requested or "/" in requested:
        return requested
    discovered = shutil.which(requested)
    if discovered:
        return discovered
    ordered = (*candidates, *(str(Path(directory) / requested) for directory in DEFAULT_BIN_DIRS))
    for candidate in ordered:
        if Path(candidate).is_file():
            return candidate
    return requested


def executable_environment(executable: str) -> dict[str, str]:
    """Return an environment where script interpreters resolve under launchd."""

    env = os.environ.copy()
    path_parts = [str(Path(executable).parent), *DEFAULT_BIN_DIRS]
    path_parts.extend(part for part in env.get("PATH", "").split(os.pathsep) if part)
    env["PATH"] = os.pathsep.join(dict.fromkeys(path_parts))
    return env
