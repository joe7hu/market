"""Storage-health application owner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from investment_panel.core.config import AppConfig
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.storage_archive import StorageArchiveService


def storage_health(config: AppConfig) -> dict[str, Any]:
    return StorageArchiveService(runtime_for_config(config), config.nas.storage_archive_dir).health()


__all__ = ["storage_health"]
