"""Filesystem-only archive integrity helpers, independent of database services."""

from __future__ import annotations

from hashlib import sha256
import gzip
import os
from pathlib import Path
from typing import Any

MAX_CHUNK_BYTES = 64 * 1024**2


def archive_file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sync_archive_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_copy_file(path: Path, expected_sha256: str, *, row_count: int, metadata: dict[str, Any]) -> tuple[bool, str]:
    """Verify with bounded memory; no JSON materialization or local temp file."""
    try:
        if archive_file_hash(path) != expected_sha256:
            return False, "sha256_mismatch"
        digest = sha256()
        rows = size = 0
        with gzip.open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(block)
                if size > MAX_CHUNK_BYTES:
                    return False, "chunk_exceeds_restore_budget"
                digest.update(block)
                # COPY text escapes embedded newlines, unlike CSV.
                rows += block.count(b"\n")
        if digest.hexdigest() != metadata.get("content_sha256"):
            return False, "content_sha256_mismatch"
        if rows != row_count or size != metadata.get("uncompressed_bytes"):
            return False, "copy_size_or_row_count_mismatch"
        if not metadata.get("columns"):
            return False, "copy_schema_missing"
        return True, "copy_bytes_verified"
    except (OSError, EOFError) as exc:
        return False, f"copy_archive_unreadable:{type(exc).__name__}"
