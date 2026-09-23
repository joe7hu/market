"""Command line entrypoint for verified Market storage operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from investment_panel.settings import load_config
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.infrastructure.postgres.decision_storage import compact_context_batch
from investment_panel.infrastructure.postgres.manifest_archive import ManifestArchive
from investment_panel.infrastructure.postgres.retention import RetentionRepository
from investment_panel.infrastructure.postgres.storage_archive import ARCHIVE_KINDS, StorageArchiveService


def _service(config_path: str | None) -> StorageArchiveService:
    config = load_config(config_path)
    return StorageArchiveService(runtime_for_config(config), Path(config.nas.storage_archive_dir))


def run(
    command: str,
    *,
    config_path: str | None = None,
    batch_size: int | None = None,
    max_batches: int = 1,
    manifest_id: int | None = None,
    destination: str | None = None,
    phase: str | None = None,
    state: str = "plan",
    backup_token: str | None = None,
    execute: bool = False,
    expire: bool = False,
) -> dict[str, Any]:
    batch_size = batch_size if batch_size is not None else (25 if phase == "decision-context" else 10 if phase == "publications" else 500)
    service = _service(config_path)
    if command == "plan":
        return service.plan()
    if command in {"archive", "compact"} and phase == "decision-manifests":
        return ManifestArchive(service).run(state=state, batch_size=batch_size,
            max_batches=max_batches, execute=execute, backup_token=backup_token)
    if command == "compact" and phase == "decision-context":
        if state not in {"plan", "backfill"}:
            raise ValueError("decision-context state must be plan or backfill")
        return compact_context_batch(service.runtime, batch_size=batch_size,
                                     execute=execute and state == "backfill")
    if command in {"archive", "compact"} and phase == "publications":
        if state == "cutover":
            if execute:
                service._require_verified_backup(backup_token)
            return RetentionRepository(service.runtime, archive_root=service.archive_root).prune_publications(
                batch_size=batch_size, dry_run=not execute)
        if state not in {"plan", "backfill"}:
            raise ValueError("publications state must be plan, backfill, or cutover; use verify for individual manifests")
        return RetentionRepository(service.runtime, archive_root=service.archive_root).archive_publications(
            batch_size=batch_size, execute=execute and state == "backfill")
    if command == "archive":
        if phase == "fundamental-history":
            return service.archive_fundamental_history(batch_size=batch_size)
        if phase == "options":
            if expire:
                return service.expire_option_archives(execute=execute)
            return service.archive_options(execute=execute and state == "cutover",
                                           export=execute and state == "backfill", backup_token=backup_token)
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
    parser.add_argument("--phase", choices=sorted(ARCHIVE_KINDS | {"price-confirmations", "decision-context"}))
    parser.add_argument("--state", choices=("plan", "backfill", "verify", "cutover"), default="plan")
    parser.add_argument("--batch-size", type=int, help="phase-specific bounded batch size")
    parser.add_argument("--max-batches", type=int, default=1, help="bounded decision-manifest export batches per invocation")
    parser.add_argument("--manifest-id", type=int)
    parser.add_argument("--destination")
    parser.add_argument("--backup-token", help="SHA-256 of a verified NAS PostgreSQL backup")
    parser.add_argument("--execute", action="store_true", help="enable writes for backfill or explicitly selected cutover; otherwise plan only")
    parser.add_argument("--expire", action="store_true", help="inventory option archives past 730 days; age-only deletion is disabled")
    args = parser.parse_args()
    print(json.dumps(run(
        args.command, config_path=args.config, phase=args.phase, batch_size=args.batch_size,
        max_batches=args.max_batches,
        manifest_id=args.manifest_id, destination=args.destination, state=args.state,
        backup_token=args.backup_token, execute=args.execute,
        expire=args.expire,
    ), default=str, indent=2))


if __name__ == "__main__":
    main()
