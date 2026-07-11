"""Persisted local refresh-job launcher for the API."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from typing import Any, Callable
from uuid import uuid4

from investment_panel.core.db import db, init_db, json_dumps, query_rows
from investment_panel.jobs import (
    daily_screen,
    full_market_refresh,
    hourly_options_radar,
    premarket_options_intelligence,
    refresh_options_radar,
    run_option_agents,
    refresh_decision_models,
    update_arco_data,
    update_disclosures,
    update_broker_sources,
    update_event_calendar,
    update_free_sources,
    update_ibkr_options,
    update_market_environment,
    update_preopen_daily_brief,
    update_research_sources,
    update_robinhood_options,
    update_social_sources,
)


JobRunner = Callable[[str | None], dict[str, Any]]

JOB_TIMEOUT_SECONDS: dict[str, int] = {
    "options_radar_hard_refresh": 5400,
}


def _runtime_dir(db_path: Any) -> Path:
    path = Path(db_path)
    return path.parent / ".refresh-jobs"


def _runtime_path(db_path: Any, job_id: str) -> Path:
    return _runtime_dir(db_path) / f"{job_id}.json"


def _write_runtime_job(db_path: Any, row: dict[str, Any]) -> None:
    job_id = str(row.get("id") or "").strip()
    if not job_id:
        return
    runtime_dir = _runtime_dir(db_path)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = _runtime_path(db_path, job_id)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}
    path.write_text(json_dumps({**existing, **row}), encoding="utf-8")


def _remove_runtime_job(db_path: Any, job_id: str) -> None:
    try:
        _runtime_path(db_path, job_id).unlink()
    except FileNotFoundError:
        pass


def _runtime_job_rows(db_path: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    runtime_dir = _runtime_dir(db_path)
    if not runtime_dir.exists():
        return rows
    for path in runtime_dir.glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(row, dict):
            row["summary"] = parse_json(row.get("summary"))
            rows.append(row)
    return sorted(rows, key=lambda row: str(row.get("started_at") or ""), reverse=True)


def _merge_runtime_rows(rows: list[dict[str, Any]], runtime_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {str(row.get("id")): row for row in rows if row.get("id")}
    for row in runtime_rows:
        job_id = str(row.get("id") or "")
        if job_id and job_id not in merged:
            merged[job_id] = row
    return sorted(merged.values(), key=lambda row: str(row.get("started_at") or ""), reverse=True)[:50]


def _is_duckdb_lock_error(exc: Exception) -> bool:
    return "Could not set lock on file" in str(exc)


def _job_timeout_seconds(job_name: str) -> int | None:
    env_key = f"MARKET_REFRESH_JOB_TIMEOUT_{job_name.upper()}"
    raw = os.environ.get(env_key)
    if raw is not None:
        try:
            value = int(raw.strip())
            return value if value > 0 else None
        except ValueError:
            pass
    return JOB_TIMEOUT_SECONDS.get(job_name)


def run_options_radar_hard_refresh(config_path: str | None = "config.yaml") -> dict[str, Any]:
    """Pull fresh option chains, then rematerialize the visible radar snapshot."""

    source = update_robinhood_options.run(config_path)
    source_status = str(source.get("status") or "").strip().lower()
    if source_status not in {"ok", "partial"}:
        return {
            "ok": False,
            "status": "failed",
            "failedStep": "update_robinhood_options",
            "error": f"Robinhood option refresh returned {source_status or 'unknown'}",
            "source": source,
        }
    source_symbols = source.get("symbols")
    radar_symbols = [str(symbol).upper() for symbol in source_symbols if symbol] if isinstance(source_symbols, list) else None
    if radar_symbols == []:
        radar = {"status": "skipped", "reason": "no_incremental_symbols", "source": "robinhood"}
    else:
        radar = refresh_options_radar.run_signal_only(config_path, symbols=radar_symbols, source="robinhood")
    return {
        "ok": True,
        "status": "succeeded",
        "source": source,
        "options_radar": radar,
    }


ALLOWLIST: dict[str, JobRunner] = {
    "full_market_refresh": lambda config_path: full_market_refresh.run(config_path, continue_on_error=True),
    "refresh_decision_models": lambda config_path: refresh_decision_models.run(config_path),
    "daily_screen": lambda config_path: daily_screen.run(config_path, online_check=False),
    "hourly_options_radar": lambda config_path: hourly_options_radar.run(config_path),
    "premarket_options_intelligence": lambda config_path: premarket_options_intelligence.run(config_path),
    "update_arco_data": lambda config_path: update_arco_data.run(config_path),
    "update_free_sources": lambda config_path: update_free_sources.run(config_path, analyses=True),
    "update_market_environment": lambda config_path: update_market_environment.run(config_path),
    "update_preopen_daily_brief": lambda config_path: update_preopen_daily_brief.run(config_path),
    "update_preopen_daily_brief_scheduled": lambda config_path: update_preopen_daily_brief.run(config_path, scheduled=True),
    # Radar-focused source pull for the continuous scheduler: TradingView option
    # chains/quotes plus yfinance option liquidity (open interest / volume), which
    # the radar data contract requires for trade-readiness. The equity-price
    # refresh and run_all_analyses pass are skipped — they are not needed for
    # radar option freshness and run in the daily full_market_refresh.
    "update_free_sources_radar": lambda config_path: update_free_sources.run(
        config_path, equity_data=False, tradingview=True, yfinance=True, analyses=False
    ),
    # IBKR option chains (price/greeks/OI/volume) persisted as source='ibkr' — the
    # reliable option source replacing the rate-limited TradingView+yfinance combo.
    "update_ibkr_options": lambda config_path: update_ibkr_options.run(config_path),
    # Robinhood option chains (price/greeks/OI/volume) persisted as
    # source='robinhood'. This is market-data only; no account or order tools.
    "update_robinhood_options": lambda config_path: update_robinhood_options.run(config_path),
    "refresh_options_radar": lambda config_path: refresh_options_radar.run(config_path),
    # Agent-free rematerialization for the in-process continuous scheduler. Codex
    # thesis/postmortem workers stay on the daily premarket cadence; this path
    # only recomputes deterministic option math, gates, and ranking.
    "refresh_options_radar_deterministic": lambda config_path: refresh_options_radar.run_deterministic_only(config_path),
    # Fast fresh-signal rematerialization (no agents, no heavy learning pass) for
    # the continuous 15-min loop; the full deterministic refresh (with learning)
    # runs on a slower cadence.
    "refresh_options_radar_signal": lambda config_path: refresh_options_radar.run_signal_only(config_path),
    # IBKR-scoped fast signal refresh for the cutover: rematerializes from the
    # reliable source='ibkr' chains only (clean OI/volume/greeks, no peer conflict).
    "refresh_options_radar_signal_ibkr": lambda config_path: refresh_options_radar.run_signal_only(config_path, source="ibkr"),
    "refresh_options_radar_signal_robinhood": lambda config_path: refresh_options_radar.run_signal_only(config_path, source="robinhood"),
    "options_radar_hard_refresh": run_options_radar_hard_refresh,
    "refresh_options_radar_learning_marks": lambda config_path: refresh_options_radar.run_learning_marks(config_path),
    "run_option_agents": lambda config_path: run_option_agents.run(config_path),
    # Manual run: forces the consolidated agent over the full open queue whenever a
    # command is configured, independent of the auto-run (enabled) toggle.
    "run_option_agents_force": lambda config_path: run_option_agents.run(config_path, force=True),
    # On-demand run: processes only user-requested (ondemand:) thesis requests.
    "run_option_agents_ondemand": lambda config_path: run_option_agents.run(config_path, ondemand=True),
    "update_broker_sources": lambda config_path: update_broker_sources.run(config_path),
    # Live opencli social (X list + per-account fallback) and research (news +
    # blogs) ingestion. Both record source_runs with ok/rate_limited/failed.
    "update_social_sources": lambda config_path: update_social_sources.run(config_path),
    "update_research_sources": lambda config_path: update_research_sources.run(config_path),
    "update_disclosures": lambda config_path: update_disclosures.run(config_path, online_check=False, max_filings=3, fetch_holdings=False),
    "update_event_calendar": lambda config_path: update_event_calendar.run(config_path),
}


def refresh_job_rows(db_path: Any) -> list[dict[str, Any]]:
    runtime_rows = _runtime_job_rows(db_path)
    try:
        init_db(db_path, retries=0)
        mark_stale_running_jobs(db_path, retries=0)
        read_only = False
    except Exception as exc:
        if _is_duckdb_lock_error(exc):
            return runtime_rows
        if "different configuration than existing connections" not in str(exc):
            raise
        read_only = True
    try:
        con_context = db(db_path, read_only=read_only, retries=0)
        with con_context as con:
            rows = query_rows(
                con,
                """
                SELECT id, job_name, status, started_at, finished_at, error, summary
                FROM refresh_jobs
                ORDER BY started_at DESC
                LIMIT 50
                """,
            )
    except Exception as exc:
        if _is_duckdb_lock_error(exc):
            return runtime_rows
        raise
    for row in rows:
        row["summary"] = parse_json(row.get("summary"))
    return _merge_runtime_rows(rows, runtime_rows)


def _update_refresh_job_failed(con: Any, job_id: str, error: str, summary: Any | None = None) -> None:
    con.execute(
        """
        UPDATE refresh_jobs
        SET status = 'failed', finished_at = ?, error = ?, summary = ?
        WHERE id = ?
        """,
        [datetime.now(UTC), error, json_dumps(summary if summary is not None else {"error": error}), job_id],
    )


def _update_refresh_job_succeeded(con: Any, job_id: str, summary: Any) -> None:
    con.execute(
        """
        UPDATE refresh_jobs
        SET status = 'succeeded', finished_at = ?, error = NULL, summary = ?
        WHERE id = ?
        """,
        [datetime.now(UTC), json_dumps(summary), job_id],
    )


def _refresh_job_row(con: Any, job_id: str) -> dict[str, Any] | None:
    rows = query_rows(
        con,
        """
        SELECT id, job_name, status, started_at, finished_at, error, summary
        FROM refresh_jobs
        WHERE id = ?
        """,
        [job_id],
    )
    if not rows:
        return None
    row = rows[0]
    row["summary"] = parse_json(row.get("summary"))
    return row


def fail_running_jobs(db_path: Any, reason: str) -> int:
    init_db(db_path)
    finished_at = datetime.now(UTC)
    with db(db_path, read_only=False) as con:
        result = con.execute(
            """
            UPDATE refresh_jobs
            SET status = 'failed', finished_at = ?, error = ?, summary = ?
            WHERE status = 'running'
            """,
            [finished_at, reason, json_dumps({"error": reason})],
        )
        count = int(result.fetchone()[0] if result.description else 0)
    for row in _runtime_job_rows(db_path):
        _remove_runtime_job(db_path, str(row.get("id") or ""))
    return count


def mark_stale_running_jobs(
    db_path: Any,
    *,
    stale_after: timedelta = timedelta(hours=3),
    retries: int = 30,
) -> int:
    cutoff = datetime.now(UTC) - stale_after
    reason = f"Refresh job did not finish within {stale_after}."
    with db(db_path, read_only=False, retries=retries) as con:
        result = con.execute(
            """
            UPDATE refresh_jobs
            SET status = 'failed', finished_at = ?, error = ?, summary = ?
            WHERE status = 'running'
              AND started_at < ?
            """,
            [datetime.now(UTC), reason, json_dumps({"error": reason}), cutoff],
        )
        count = int(result.fetchone()[0] if result.description else 0)
    if count:
        for row in _runtime_job_rows(db_path):
            started_at = row.get("started_at")
            try:
                started = datetime.fromisoformat(str(started_at))
            except (TypeError, ValueError):
                continue
            if started < cutoff:
                _remove_runtime_job(db_path, str(row.get("id") or ""))
    return count


def start_refresh_job(job_name: str, db_path: Any) -> dict[str, Any]:
    if job_name not in ALLOWLIST:
        allowed = ", ".join(sorted(ALLOWLIST))
        raise ValueError(f"refresh job is not allowlisted: {job_name}. Allowed jobs: {allowed}")

    init_db(db_path)
    mark_stale_running_jobs(db_path)
    job_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{job_name}-{uuid4().hex[:8]}"
    started_at = datetime.now(UTC)
    with db(db_path, read_only=False) as con:
        existing = query_rows(
            con,
            """
            SELECT id, job_name, status, started_at
            FROM refresh_jobs
            WHERE job_name = ?
              AND status = 'running'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            [job_name],
        )
        if existing:
            existing_row = {**existing[0], "created": False}
            _write_runtime_job(db_path, {**existing_row, "summary": {}})
            return existing_row
        con.execute(
            """
            INSERT INTO refresh_jobs (id, job_name, status, started_at, finished_at, error, summary)
            VALUES (?, ?, 'running', ?, NULL, NULL, '{}')
            """,
            [job_id, job_name, started_at],
        )
    job = {"id": job_id, "job_name": job_name, "status": "running", "started_at": started_at, "created": True, "summary": {}}
    _write_runtime_job(db_path, job)
    return job


def execute_refresh_job(
    job_id: str,
    job_name: str,
    db_path: Any,
    config_path: str | None = "config.yaml",
    *,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    if job_name not in ALLOWLIST:
        allowed = ", ".join(sorted(ALLOWLIST))
        raise ValueError(f"refresh job is not allowlisted: {job_name}. Allowed jobs: {allowed}")
    _write_runtime_job(db_path, {"id": job_id, "job_name": job_name, "status": "running", "summary": {}})
    try:
        summary = ALLOWLIST[job_name](config_path)
    except Exception as exc:
        error = f"{exc}\n{traceback.format_exc()}"
        with db(db_path, read_only=False) as con:
            _update_refresh_job_failed(con, job_id, error, {"error": str(exc)})
            row = _refresh_job_row(con, job_id)
        if row:
            _write_runtime_job(db_path, row)
        _remove_runtime_job(db_path, job_id)
        if raise_on_error:
            raise
        return {"id": job_id, "job_name": job_name, "status": "failed", "error": str(exc)}

    failure = summary_failure_message(summary)
    if failure:
        with db(db_path, read_only=False) as con:
            _update_refresh_job_failed(con, job_id, failure, summary)
            row = _refresh_job_row(con, job_id)
        if row:
            _write_runtime_job(db_path, row)
        _remove_runtime_job(db_path, job_id)
        return {"id": job_id, "job_name": job_name, "status": "failed", "error": failure, "summary": summary}

    with db(db_path, read_only=False) as con:
        _update_refresh_job_succeeded(con, job_id, summary)
        row = _refresh_job_row(con, job_id)
    if row:
        _write_runtime_job(db_path, row)
    _remove_runtime_job(db_path, job_id)
    return {"id": job_id, "job_name": job_name, "status": "succeeded", "summary": summary}


def finish_refresh_job_failed(job_id: str, job_name: str, db_path: Any, error: str) -> dict[str, Any]:
    with db(db_path, read_only=False) as con:
        _update_refresh_job_failed(con, job_id, error)
        row = _refresh_job_row(con, job_id)
    if row:
        _write_runtime_job(db_path, row)
    _remove_runtime_job(db_path, job_id)
    return {"id": job_id, "job_name": job_name, "status": "failed", "error": error}


def execute_refresh_job_subprocess(
    job_id: str,
    job_name: str,
    db_path: Any,
    config_path: str | None = "config.yaml",
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "investment_panel.core.refresh_jobs",
        job_name,
        "--job-id",
        job_id,
        "--db-path",
        str(db_path),
        "--config",
        config_path or "config.yaml",
    ]
    timeout_seconds = _job_timeout_seconds(job_name)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return finish_refresh_job_failed(
            job_id,
            job_name,
            db_path,
            f"refresh subprocess timed out after {timeout_seconds}s: {' '.join(command)}",
        )
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        detail = stderr or stdout or f"refresh subprocess exited with code {completed.returncode}"
        return finish_refresh_job_failed(
            job_id,
            job_name,
            db_path,
            f"refresh subprocess exited with code {completed.returncode}: {detail[-2000:]}",
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"id": job_id, "job_name": job_name, "status": "succeeded", "summary": {"stdout": stdout[-2000:]}}


def run_refresh_job(job_name: str, db_path: Any, config_path: str | None = "config.yaml") -> dict[str, Any]:
    job = start_refresh_job(job_name, db_path)
    if not job.get("created"):
        return job
    return execute_refresh_job(job["id"], job_name, db_path, config_path)


def parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def summary_failure_message(summary: Any) -> str | None:
    if not isinstance(summary, dict):
        return None
    if summary.get("ok") is not False and summary.get("status") != "failed":
        return None
    error = summary.get("error")
    if isinstance(error, str) and error:
        return error
    source_errors = summary.get("source_errors")
    if isinstance(source_errors, list):
        failed_sources = [
            str(item.get("name") or "").strip()
            for item in source_errors
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        if failed_sources:
            return f"Refresh failed for sources: {', '.join(failed_sources[:3])}"
    failed_step = summary.get("failedStep")
    if isinstance(failed_step, str) and failed_step:
        return f"Refresh failed at {failed_step}"
    return "Refresh failed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_name")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--job-id")
    args = parser.parse_args(argv)

    if args.job_id:
        result = execute_refresh_job(args.job_id, args.job_name, args.db_path, args.config, raise_on_error=False)
    else:
        result = run_refresh_job(args.job_name, args.db_path, args.config)
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
