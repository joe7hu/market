"""Resolve the local Codex CLI under interactive shells and launchd."""

from __future__ import annotations

from investment_panel.core.executable import resolve_executable


CODEX_CANDIDATES = ("/opt/homebrew/bin/codex", "/usr/local/bin/codex")


def resolve_codex_bin() -> str:
    return resolve_executable("codex", env_var="MARKET_CODEX_BIN", candidates=CODEX_CANDIDATES)
