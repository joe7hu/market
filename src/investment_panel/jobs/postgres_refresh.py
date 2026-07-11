"""Small PostgreSQL-only refresh compositions used by the live app."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable

from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.retention import RetentionRepository
from investment_panel.database.today_analysis import refresh_today_publication
from investment_panel.jobs import refresh_options_radar, run_option_agents


def premarket(config_path: str | None = None) -> dict[str, Any]:
    """Publish the daily decision snapshot from already-ingested raw facts."""

    before_agents = refresh_options_radar.run(config_path)
    agents = run_option_agents.run(config_path)
    after_agents = refresh_options_radar.run_deterministic_only(config_path)
    today = refresh_today_publication(runtime_for_config(load_config(config_path)))
    return {
        "status": "ok",
        "database": load_config(config_path).database.url,
        "cadence": "daily_premarket",
        "before_agents": before_agents,
        "agents": agents,
        "after_agents": after_agents,
        "today": today,
    }


def full(config_path: str | None = None, *, continue_on_error: bool = True) -> dict[str, Any]:
    """Run bounded option ingestion, analysis, agents, and retention.

    Raw providers are independent steps so one unavailable broker cannot prevent
    publication from the latest good facts. PostgreSQL job rows provide the
    single-flight boundary; no file lock or application shutdown is required.
    """

    from investment_panel.jobs import update_ibkr_options, update_robinhood_options

    config = load_config(config_path)
    steps: list[tuple[str, bool, Callable[[], dict[str, Any]]]] = [
        ("robinhood_options", False, lambda: update_robinhood_options.run(config_path)),
        ("ibkr_options", False, lambda: update_ibkr_options.run(config_path)),
        ("options_radar", True, lambda: refresh_options_radar.run(config_path)),
        ("option_agents", True, lambda: run_option_agents.run(config_path)),
        ("today_publication", True, lambda: refresh_today_publication(runtime_for_config(config))),
        ("retention", True, lambda: RetentionRepository(runtime_for_config(config)).prune()),
    ]
    results: list[dict[str, Any]] = []
    failed: list[str] = []
    warnings: list[str] = []
    for name, required, runner in steps:
        started = datetime.now(UTC)
        try:
            result = runner()
            status = str(result.get("status") or "ok").lower()
            if name in {"robinhood_options", "ibkr_options"}:
                step_failed = status not in {"ok", "partial"}
            else:
                step_failed = status in {"error", "failed", "unsafe_config"}
            results.append({"name": name, "ok": not step_failed, "started_at": started, "result": result})
            if step_failed:
                (failed if required else warnings).append(name)
                if required and not continue_on_error:
                    break
        except Exception as exc:  # provider boundary is reflected in job status
            results.append({"name": name, "ok": False, "started_at": started, "error": f"{type(exc).__name__}: {exc}"})
            (failed if required else warnings).append(name)
            if required and not continue_on_error:
                break
    status = "failed" if failed and not any(row["ok"] for row in results) else "partial" if failed or warnings else "ok"
    return {
        "ok": not failed,
        "status": status,
        "database": config.database.url,
        "started_at": results[0]["started_at"] if results else datetime.now(UTC),
        "finished_at": datetime.now(UTC),
        "failed_steps": failed,
        "warning_steps": warnings,
        "steps": results,
    }
