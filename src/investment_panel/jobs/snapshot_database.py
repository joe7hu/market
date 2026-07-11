"""Create and verify a PostgreSQL custom-format backup on the NAS."""

from __future__ import annotations

import argparse
import json

from investment_panel.core.config import load_config
from investment_panel.core.status import write_source_status
from investment_panel.database.backup import create_verified_backup


def run(config_path: str | None = None) -> dict[str, object]:
    config = load_config(config_path)
    backup = create_verified_backup(config.database.url, config.nas.postgres_backup_dir)
    status_path = write_source_status(
        config,
        "mini-market-db-snapshot",
        {
            "source": "market-mini",
            "database": "postgresql",
            "backup": backup,
        },
    )
    return {"database": "postgresql", "status_path": str(status_path), **backup}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2))


if __name__ == "__main__":
    main()
