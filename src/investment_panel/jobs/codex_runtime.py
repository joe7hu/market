"""Resolve the local Codex CLI under interactive shells and launchd."""

from __future__ import annotations

import os
from pathlib import Path
import shutil


CODEX_CANDIDATES = ("/opt/homebrew/bin/codex", "/usr/local/bin/codex")


def resolve_codex_bin() -> str:
    configured = os.environ.get("MARKET_CODEX_BIN", "").strip()
    if configured:
        return configured
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    for candidate in CODEX_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return "codex"
