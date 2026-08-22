"""Command line entrypoint for verified Market storage operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.storage_archive import ARCHIVE_KINDS, StorageArchiveService


def _service(config_path: str | None) -> StorageArchiveService:
    config = load_config(config_path)
    return StorageArchiveService(runtime_for_config(config), Path(config.nas.storage_archive_dir))


def run(
    command: str,
    *,
    config_path: str | None = None,
    batch_size: int = 500,
    manifest_id: int | None = None,
    destination: str | None = None,
    phase: str | None = None,
    state: str = "plan",
    backup_token: str | None = None,
    execute: bool = False,
    expire: bool = False,
) -> dict[str, Any]:
    service = _service(config_path)
    if command == "plan":
        return service.plan()
    if command == "archive":
        if phase == "fundamental-history":
            return service.archive_fundamental_history(batch_size=batch_size)
        if phase == "options":
            if expire:
                return service.expire_option_archives(execute=execute)
            return service.archive_options(execute=execute, backup_token=backup_token)
        raise ValueError("archive requires phase=fundamental-history or phase=options")
    if command == "verify":
        return service.verify(manifest_id=manifest_id)
    if command == "restore":
        if manifest_id is None or not destination:
            raise ValueError("restore requires --manifest-id and --destination staging file")
        return service.restore_to_file(manifest_id, Path(destination))
    if command == "compact":
        if phase != "price-confirmations":
            raise ValueError("compact requires a named phase")
        return service.compact_price_confirmations(
            state=state, batch_size=batch_size, backup_token=backup_token, dry_run=not execute
        )
    raise ValueError(f"unknown storage command: {command}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verified, resumable Market storage operations")
    parser.add_argument("command", choices=("plan", "archive", "verify", "compact", "restore"))
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--phase", choices=sorted(ARCHIVE_KINDS | {"price-confirmations"}))
    parser.add_argument("--state", choices=("plan", "backfill", "verify", "cutover"), default="plan")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--manifest-id", type=int)
    parser.add_argument("--destination")
    parser.add_argument("--backup-token", help="SHA-256 of a verified NAS PostgreSQL backup")
    parser.add_argument("--execute", action="store_true", help="enable a destructive detach/cutover; otherwise dry-run")
    parser.add_argument("--expire", action="store_true", help="report or remove option archive objects past 730 days")
    args = parser.parse_args()
    print(json.dumps(run(
        args.command, config_path=args.config, phase=args.phase, batch_size=args.batch_size,
        manifest_id=args.manifest_id, destination=args.destination, state=args.state,
        backup_token=args.backup_token, execute=args.execute,
        expire=args.expire,
    ), default=str, indent=2))


if __name__ == "__main__":
    main()
