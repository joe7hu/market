"""Register the default strategy and archetype families."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_PARAMETERS, DEFAULT_STRATEGY_VERSION, STRATEGY_FAMILY_PRESETS)

def register_default_strategy(con: Any, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> None:
    now = datetime.utcnow().isoformat()
    con.execute(
        """
        INSERT OR IGNORE INTO option_strategy_versions
        (strategy_version, strategy_name, version, created_at, status, parameters, promoted_at, supersedes, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            strategy_version,
            DEFAULT_STRATEGY_PARAMETERS["strategy_name"],
            DEFAULT_STRATEGY_PARAMETERS["version"],
            now,
            "shadow",
            json_dumps(DEFAULT_STRATEGY_PARAMETERS),
            None,
            None,
            "Deterministic 10x LEAP reversal baseline. Agents may propose changes, but code/backtests promote versions.",
        ],
    )


def register_strategy_families(con: Any) -> int:
    """Register the additional archetype families as forward_test (shadow) strategies.
    Idempotent — INSERT OR IGNORE never disturbs a promoted/edited version."""

    now = datetime.utcnow().isoformat()
    written = 0
    for version, params in STRATEGY_FAMILY_PRESETS.items():
        con.execute(
            """
            INSERT OR IGNORE INTO option_strategy_versions
            (strategy_version, strategy_name, version, created_at, status, parameters, promoted_at, supersedes, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                version,
                params["strategy_name"],
                params.get("version", 1),
                now,
                "forward_test",
                json_dumps(params),
                None,
                None,
                f"{params['strategy_family']} archetype — shadow-traded until backtest/forward-test promote it.",
            ],
        )
        written += 1
    return written


def candidate_strategy_versions(con: Any, primary: str = DEFAULT_STRATEGY_VERSION) -> list[str]:
    """Strategy versions that should generate candidates: the primary plus every
    registered active/shadow/forward_test family, deduped with the primary first."""

    rows = query_rows(
        con,
        "SELECT strategy_version FROM option_strategy_versions WHERE status IN ('active', 'shadow', 'forward_test')",
    )
    versions = [primary]
    for row in rows:
        version = str(row.get("strategy_version"))
        if version and version not in versions:
            versions.append(version)
    return versions
