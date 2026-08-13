"""Resumable bounded backfill for point-in-time price availability facts."""

from __future__ import annotations

import argparse
import json
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.jobs import JobRepository
from investment_panel.database.price_confirmation_retention import PriceConfirmationRetentionRepository


JOB_NAME = "price_fact_availability_backfill"
STATE_PREFIX = "price_fact_availability_backfill/"


def run(
    config_path: str | None = "config.yaml",
    *,
    table: str = "quote",
    fact_batch_size: int = 1_000,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Project one checkpointed batch. Re-run until ``complete`` is true."""

    if table not in {"quote", "price_bar"}:
        raise ValueError("table must be quote or price_bar")
    runtime = runtime_for_config(load_config(config_path))
    jobs = JobRepository(runtime)
    job = jobs.start(f"{JOB_NAME}:{table}")
    if not job["created"]:
        return {"status": "skipped", "reason": "already_running", "job_id": job["id"], "table": table}
    try:
        cursor = _cursor(runtime, table)
        result = PriceConfirmationRetentionRepository(runtime).project_availability_batch(
            table=table,
            after_fact_id=int(cursor.get("after_fact_id") or 0),
            after_available_at=cursor.get("after_available_at"),
            fact_batch_size=fact_batch_size,
            dry_run=dry_run,
        )
        complete = int(result["fact_versions"]) < fact_batch_size
        next_cursor = {
            "after_fact_id": result["next_after_fact_id"] if result["next_after_fact_id"] is not None else cursor.get("after_fact_id", 0),
            "after_available_at": _iso(result["next_after_available_at"]) if result["next_after_available_at"] is not None else cursor.get("after_available_at"),
        }
        if not dry_run and result["next_after_fact_id"] is not None:
            _store_cursor(runtime, table, next_cursor)
        payload = {
            "status": "ok",
            "table": table,
            "dry_run": dry_run,
            "complete": complete,
            "cursor": next_cursor,
            **result,
        }
        jobs.finish(job["id"], "succeeded" if complete else "partial", summary=payload)
        return payload
    except Exception as exc:
        jobs.finish(job["id"], "failed", error=f"{type(exc).__name__}: {exc}")
        raise


def _cursor(runtime: Any, table: str) -> dict[str, Any]:
    with runtime.read() as connection:
        row = connection.execute("SELECT value FROM app.setting WHERE key = %s", [STATE_PREFIX + table]).fetchone()
    return dict(row["value"] or {}) if row else {"after_fact_id": 0, "after_available_at": None}


def _store_cursor(runtime: Any, table: str, cursor: dict[str, Any]) -> None:
    with runtime.transaction() as connection:
        connection.execute(
            """
            INSERT INTO app.setting (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """,
            [STATE_PREFIX + table, Jsonb(cursor)],
        )


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--table", choices=("quote", "price_bar"), default="quote")
    parser.add_argument("--batch-size", type=int, default=1_000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.config, table=args.table, fact_batch_size=args.batch_size, dry_run=args.dry_run), default=str))


if __name__ == "__main__":
    main()
