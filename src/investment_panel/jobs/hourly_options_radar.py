"""Hourly deterministic options-radar refresh without agent workers."""

from __future__ import annotations

import argparse
import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from investment_panel.core.config import load_config
from investment_panel.core.status import write_source_status
from investment_panel.jobs import refresh_options_radar

LOCK_PATH = Path("/tmp/market-hourly-options-radar.lock")
APP_STATUS_URL = "http://127.0.0.1:8000/api/status"


def run(config_path: str | None = None, symbols: list[str] | None = None, *, lock_path: Path = LOCK_PATH) -> dict[str, Any]:
    config = load_config(config_path)
    if app_is_serving_database(config.database.duckdb_path):
        result = {
            "database": str(config.database.duckdb_path),
            "cadence": "hourly_deterministic",
            "status": "skipped_app_active",
            "agent_workers": "daily_premarket_only",
            "source_refresh": "skipped_hourly_to_keep_app_responsive",
            "app_status_url": APP_STATUS_URL,
        }
        status_path = write_hourly_status(config, result)
        return {**result, "status_path": str(status_path)}

    with hourly_lock(lock_path) as acquired:
        if not acquired:
            result = {
                "database": str(config.database.duckdb_path),
                "cadence": "hourly_deterministic",
                "status": "skipped_running",
                "agent_workers": "daily_premarket_only",
                "lock_path": str(lock_path),
            }
            status_path = write_hourly_status(config, result)
            return {**result, "status_path": str(status_path)}

        radar = refresh_options_radar.run_signal_only(config_path, symbols=symbols)
        result = {
            "database": str(config.database.duckdb_path),
            "cadence": "hourly_deterministic",
            "status": "succeeded",
            "agent_workers": "daily_premarket_only",
            "source_refresh": "skipped_hourly_to_avoid_app_db_lock",
            "lock_path": str(lock_path),
            "options_radar": radar,
        }
        status_path = write_hourly_status(config, result)
        return {**result, "status_path": str(status_path)}


def app_is_serving_database(db_path: Any, status_url: str = APP_STATUS_URL) -> bool:
    try:
        with urlopen(status_url, timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except TimeoutError:
        return True
    except (OSError, URLError):
        return False
    except json.JSONDecodeError:
        return True
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    config = metadata.get("config") if isinstance(metadata, dict) else None
    database = config.get("database") if isinstance(config, dict) else None
    active_path = database.get("duckdb_path") if isinstance(database, dict) else None
    return bool(payload.get("ready")) and Path(str(active_path or "")).resolve() == Path(db_path).resolve()


@contextmanager
def hourly_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        handle.write("locked\n")
        handle.flush()
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_hourly_status(config: Any, result: dict[str, Any]) -> Path:
    return write_source_status(
        config,
        "mini-market-hourly-options-radar",
        {
            "source": "market-mini",
            "job": "hourly_options_radar",
            "origin": "autonomous_collector",
            **result,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", action="append", dest="symbols", default=None)
    args = parser.parse_args()
    print(json.dumps(run(args.config, symbols=args.symbols), indent=2, default=str))


if __name__ == "__main__":
    main()
