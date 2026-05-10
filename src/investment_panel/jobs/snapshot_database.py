"""Copy the local DuckDB database to the NAS source archive."""

from __future__ import annotations

import argparse
import json

from investment_panel.core.config import load_config
from investment_panel.core.status import snapshot_duckdb, write_source_status


def run(config_path: str | None = None) -> dict[str, str | None]:
    config = load_config(config_path)
    snapshot_path = snapshot_duckdb(config)
    status_path = write_source_status(
        config,
        "mini-market-db-snapshot",
        {
            "source": "market-mini",
            "snapshotPath": str(snapshot_path) if snapshot_path else None,
            "database": str(config.database.duckdb_path),
        },
    )
    return {
        "database": str(config.database.duckdb_path),
        "snapshot_path": str(snapshot_path) if snapshot_path else None,
        "status_path": str(status_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2))


if __name__ == "__main__":
    main()
