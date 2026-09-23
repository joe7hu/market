"""Verified PostgreSQL custom-format backup creation."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from psycopg.conninfo import conninfo_to_dict, make_conninfo


_REQUIRED_SCHEMAS = {"catalog", "ingest", "raw", "analysis", "app", "ops"}


def create_verified_backup(
    database_url: str,
    destination_dir: str | Path,
    *,
    postgres_bin_dir: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    reference = now or datetime.now(UTC)
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = reference.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    dump_path = destination / f"market-{stamp}.dump"
    binary_dir = Path(postgres_bin_dir) if postgres_bin_dir else None
    pg_dump = str(binary_dir / "pg_dump") if binary_dir else shutil.which("pg_dump")
    pg_restore = str(binary_dir / "pg_restore") if binary_dir else shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        raise FileNotFoundError("pg_dump and pg_restore must be installed or postgres_bin_dir must be set")
    safe_database_url, dump_environment = _credential_safe_connection(database_url)
    subprocess.run(
        [pg_dump, "--format=custom", "--compress=9", "--file", str(dump_path), safe_database_url],
        check=True,
        capture_output=True,
        text=True,
        env=dump_environment,
    )
    return _verify_backup(dump_path, created_at=reference, pg_restore=pg_restore, remove_invalid=True)


def verify_existing_backup(
    dump_path: str | Path,
    *,
    created_at: datetime | None = None,
    postgres_bin_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Create a receipt for an existing custom dump without re-dumping PostgreSQL."""
    path = Path(dump_path)
    binary_dir = Path(postgres_bin_dir) if postgres_bin_dir else None
    pg_restore = str(binary_dir / "pg_restore") if binary_dir else shutil.which("pg_restore")
    if not pg_restore:
        raise FileNotFoundError("pg_restore must be installed or postgres_bin_dir must be set")
    return _verify_backup(
        path,
        created_at=created_at or datetime.fromtimestamp(path.stat().st_mtime, UTC),
        pg_restore=pg_restore,
        remove_invalid=False,
    )


def _verify_backup(
    dump_path: Path,
    *,
    created_at: datetime,
    pg_restore: str,
    remove_invalid: bool,
) -> dict[str, Any]:
    manifest_path = dump_path.with_suffix(".json")
    if manifest_path.exists():
        raise FileExistsError(f"refusing to replace existing backup receipt: {manifest_path}")
    listing = subprocess.run(
        [pg_restore, "--list", str(dump_path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    missing = sorted(schema for schema in _REQUIRED_SCHEMAS if f"SCHEMA - {schema}" not in listing)
    if missing:
        if remove_invalid:
            dump_path.unlink(missing_ok=True)
        raise RuntimeError(f"backup verification missing schemas: {', '.join(missing)}")
    digest = hashlib.sha256()
    with dump_path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    manifest = {
        "status": "verified",
        "created_at": created_at.isoformat(),
        "dump_path": str(dump_path),
        "byte_count": dump_path.stat().st_size,
        "sha256": digest.hexdigest(),
        "format": "postgresql-custom",
        "schemas": sorted(_REQUIRED_SCHEMAS),
    }
    descriptor, temporary = tempfile.mkstemp(prefix=f".{manifest_path.name}.", suffix=".tmp", dir=manifest_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, manifest_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {**manifest, "manifest_path": str(manifest_path)}


def _credential_safe_connection(database_url: str) -> tuple[str, dict[str, str] | None]:
    libpq_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    parameters = conninfo_to_dict(libpq_url)
    password = parameters.pop("password", None)
    if password is None:
        return libpq_url, None
    environment = dict(os.environ)
    environment["PGPASSWORD"] = password
    return make_conninfo(**parameters), environment


credential_safe_connection = _credential_safe_connection
