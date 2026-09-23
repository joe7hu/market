"""Create and verify a PostgreSQL custom-format backup on the NAS."""

from __future__ import annotations

import argparse
import json

from investment_panel.settings import load_config
from investment_panel.core.status import write_source_status
from investment_panel.infrastructure.postgres.backup import create_verified_backup, verify_existing_backup


def run(config_path: str | None = None, *, verify_existing: str | None = None) -> dict[str, object]:
    config = load_config(config_path)
    backup = (
        verify_existing_backup(verify_existing)
        if verify_existing
        else create_verified_backup(config.database.url, config.nas.postgres_backup_dir)
    )
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
    parser.add_argument("--verify-existing", help="validate an existing custom dump and write its receipt without re-dumping")
    args = parser.parse_args()
    print(json.dumps(run(args.config, verify_existing=args.verify_existing), indent=2))


if __name__ == "__main__":
    main()
