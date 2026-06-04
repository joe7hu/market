"""Orchestrate the daily full-market refresh workflow."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import socket
import time
import traceback
from typing import Any, Callable

from investment_panel.core.config import AppConfig, load_config
from investment_panel.jobs import (
    daily_screen,
    snapshot_database,
    update_arco_data,
    update_broker_sources,
    update_disclosures,
    update_event_calendar,
    update_free_sources,
    refresh_options_radar,
)


StepRunner = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class RefreshStep:
    name: str
    runner: StepRunner


def run(
    config_path: str | None = None,
    *,
    online_check: bool = False,
    max_filings: int = 3,
    fetch_holdings: bool = False,
    continue_on_error: bool = False,
) -> dict[str, Any]:
    """Run the full daily refresh sequence and write an orchestration status."""

    config = load_config(config_path)
    started_at = utc_now()
    step_results: list[dict[str, Any]] = []
    failed_step: str | None = None

    for step in refresh_steps(config_path, online_check=online_check, max_filings=max_filings, fetch_holdings=fetch_holdings):
        step_started = time.perf_counter()
        try:
            result = step.runner()
            step_results.append(
                {
                    "name": step.name,
                    "ok": True,
                    "durationSeconds": round(time.perf_counter() - step_started, 3),
                    "result": result,
                }
            )
        except Exception as exc:
            failed_step = step.name
            step_results.append(
                {
                    "name": step.name,
                    "ok": False,
                    "durationSeconds": round(time.perf_counter() - step_started, 3),
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
            if not continue_on_error:
                payload = full_refresh_payload(config, started_at, step_results, failed_step)
                write_full_refresh_status(config, payload)
                raise

    payload = full_refresh_payload(config, started_at, step_results, failed_step)
    status_path = write_full_refresh_status(config, payload)
    return {**payload, "status_path": str(status_path)}


def refresh_steps(
    config_path: str | None,
    *,
    online_check: bool,
    max_filings: int,
    fetch_holdings: bool,
) -> list[RefreshStep]:
    return [
        RefreshStep("arco_import", lambda: update_arco_data.run(config_path)),
        RefreshStep("daily_screen", lambda: daily_screen.run(config_path, online_check=online_check)),
        RefreshStep("free_sources_and_analyses", lambda: update_free_sources.run(config_path, equity_data=True, analyses=True)),
        RefreshStep("options_radar", lambda: refresh_options_radar.run(config_path)),
        RefreshStep("broker_sources", lambda: update_broker_sources.run(config_path)),
        RefreshStep(
            "disclosures",
            lambda: update_disclosures.run(
                config_path,
                online_check=online_check,
                max_filings=max_filings,
                fetch_holdings=fetch_holdings,
            ),
        ),
        RefreshStep("event_calendar", lambda: update_event_calendar.run(config_path)),
        RefreshStep("database_snapshot", lambda: snapshot_database.run(config_path)),
    ]


def full_refresh_payload(
    config: AppConfig,
    started_at: str,
    steps: list[dict[str, Any]],
    failed_step: str | None,
) -> dict[str, Any]:
    ok = failed_step is None and all(step.get("ok") for step in steps)
    return {
        "ok": ok,
        "status": "ok" if ok else "failed",
        "source": "market-mini",
        "job": "full_market_refresh",
        "origin": "autonomous_collector",
        "database": str(config.database.duckdb_path),
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "failedStep": failed_step,
        "steps": steps,
    }


def write_full_refresh_status(config: AppConfig, payload: dict[str, Any]) -> Path:
    config.nas.status_dir.mkdir(parents=True, exist_ok=True)
    status_path = config.nas.status_dir / "mini-market-full-refresh.json"
    body = {
        "host": socket.gethostname(),
        **payload,
    }
    status_path.write_text(json.dumps(body, indent=2, default=str) + "\n", encoding="utf-8")
    return status_path


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--online-check", action="store_true")
    parser.add_argument("--max-filings", type=int, default=3)
    parser.add_argument("--fetch-holdings", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.config,
                online_check=args.online_check,
                max_filings=args.max_filings,
                fetch_holdings=args.fetch_holdings,
                continue_on_error=args.continue_on_error,
            ),
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
