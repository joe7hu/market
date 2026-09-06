"""Choose a writable provider-payload archive without blocking ingestion."""

from __future__ import annotations

import gzip
from hashlib import sha256
import os
from pathlib import Path
import tempfile

from investment_panel.core.config import AppConfig


def provider_archive_path(config: AppConfig, *parts: str) -> Path:
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


def write_provider_payload(config: AppConfig, raw: bytes, *, compressed: bool = True) -> Path:
    """Reuse immutable payload bytes across retries and ingestion runs."""
    data = gzip.compress(raw, mtime=0) if compressed else raw
    digest = sha256(data).hexdigest()
    suffix = ".json.gz" if compressed else ".json"
    target = provider_archive_path(config, "blobs", digest[:2], digest + suffix)
    if target.exists():
        if sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"provider archive digest mismatch: {target}")
        return target
    fd, temporary = tempfile.mkstemp(prefix=".archive-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target
