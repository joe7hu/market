"""Choose a writable provider-payload archive without blocking ingestion."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any


def provider_archive_path(config: Any, *parts: str) -> Path:
    preferred = Path(config.nas.market_dir) / "provider-payloads"
    fallback = Path(config.report_dir).parent / "provider-payloads"
    for root in dict.fromkeys((preferred, fallback)):
        if root == preferred and not preferred.parent.exists():
            continue
        if _root_is_writable(root):
            path = root.joinpath(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
    raise PermissionError(f"No writable provider-payload archive: {preferred} or {fallback}")


def _root_is_writable(root: Path) -> bool:
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".market-write-check-", dir=root):
            pass
    except OSError:
        return False
    return True
