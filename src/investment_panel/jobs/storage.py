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


def run(command: str, *, config_path: str | None = None, batch_size: int = 500,
        manifest_id: int | None = None, destination: str | None = None, phase: str | None = None) -> dict[str, Any]:
    service = _service(config_path)
    if command == "plan":
        return service.plan()
    if command == "archive":
        if phase != "fundamental-history":
            raise ValueError("archive currently supports only phase=fundamental-history; no other phase may write without its validation contract")
        return service.archive_fundamental_history(batch_size=batch_size)
    if command == "verify":
        return service.verify(manifest_id=manifest_id)
    if command == "restore":
        if manifest_id is None or not destination:
            raise ValueError("restore requires --manifest-id and --destination staging file")
        return service.restore_to_file(manifest_id, Path(destination))
    if command == "compact":
        if phase not in {"fundamental-history", "price-confirmations", "publications", "options", "derived"}:
            raise ValueError("compact requires a named phase")
        raise ValueError("compaction is blocked until its named preflight records a verified backup, staging estimate, and restore proof")
    raise ValueError(f"unknown storage command: {command}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verified, non-destructive Market archive operations")
    parser.add_argument("command", choices=("plan", "archive", "verify", "compact", "restore"))
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--phase", choices=sorted(ARCHIVE_KINDS | {"price-confirmations"}))
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--manifest-id", type=int)
    parser.add_argument("--destination")
    args = parser.parse_args()
    print(json.dumps(run(args.command, config_path=args.config, phase=args.phase, batch_size=args.batch_size,
                        manifest_id=args.manifest_id, destination=args.destination), default=str, indent=2))


if __name__ == "__main__":
    main()
