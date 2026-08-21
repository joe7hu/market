"""Storage-health application owner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from investment_panel.database.authority import runtime_for_config
from investment_panel.database.storage_archive import StorageArchiveService


def storage_health(config: dict[str, Any]) -> dict[str, Any]:
    nas = config.get("nas") or {}
    archive_dir = nas.get("storage_archive_dir") or "/Volumes/agent/data-sources/market-mini/storage-archive/v1"
    return StorageArchiveService(runtime_for_config(config), Path(archive_dir)).health()


__all__ = ["storage_health"]
