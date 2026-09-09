"""Wait on one existing PostgreSQL job; print state changes, never start/cancel it."""

import argparse
import json
import math
import time
from uuid import UUID

from investment_panel.settings import load_config
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("seconds must be finite and greater than zero")
    return seconds


def wait_for_job(runtime, job_id: UUID, *, timeout: float, interval: float) -> int:
    deadline = time.monotonic() + positive_seconds(str(timeout))
    interval = positive_seconds(str(interval))
    previous = None
    while True:
        with runtime.read() as connection:
            row = connection.execute(
                "SELECT status, error FROM ops.job_run WHERE id = %s", [job_id],
            ).fetchone()
        state = {"job_id": str(job_id), "status": row["status"] if row else "missing"}
        if row and row["error"]:
            state["error"] = str(row["error"])[:300]
        if state != previous:
            print(json.dumps(state), flush=True)
            previous = state
        if state["status"] != "running":
            return 0 if state["status"] == "succeeded" else 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(json.dumps({"job_id": str(job_id), "status": "wait_timeout", "job_status": "running"}), flush=True)
            return 2
        # The read transaction is closed before sleeping; no lock/pool lease is held.
        time.sleep(min(interval, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", type=UUID)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--timeout", type=positive_seconds, default=1800.0)
    parser.add_argument("--interval", type=positive_seconds, default=5.0)
    args = parser.parse_args()
    runtime = None
    try:
        runtime = DatabaseRuntime(load_config(args.config).database.url)
        runtime.open()
        return wait_for_job(runtime, args.job_id, timeout=args.timeout, interval=args.interval)
    except Exception as exc:
        # Configuration/driver errors can contain credentials; expose only the type.
        print(json.dumps({"job_id": str(args.job_id), "status": "wait_error", "error_type": type(exc).__name__}), flush=True)
        return 1
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
