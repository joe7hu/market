"""Verified PostgreSQL custom-format backup creation."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from psycopg.conninfo import conninfo_to_dict, make_conninfo


def create_verified_backup(
    database_url: str,
    destination_dir: str | Path,
    *,
    postgres_bin_dir: str | Path = "/opt/homebrew/opt/postgresql@18/bin",
    now: datetime | None = None,
) -> dict[str, Any]:
    reference = now or datetime.now(UTC)
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = reference.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    dump_path = destination / f"market-{stamp}.dump"
    manifest_path = destination / f"market-{stamp}.json"
    binary_dir = Path(postgres_bin_dir)
    safe_database_url, dump_environment = _credential_safe_connection(database_url)
    subprocess.run(
        [str(binary_dir / "pg_dump"), "--format=custom", "--compress=9", "--file", str(dump_path), safe_database_url],
        check=True,
        capture_output=True,
        text=True,
        env=dump_environment,
    )
    listing = subprocess.run(
        [str(binary_dir / "pg_restore"), "--list", str(dump_path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    required_schemas = {"catalog", "ingest", "raw", "analysis", "app", "ops"}
    missing = sorted(schema for schema in required_schemas if f"SCHEMA - {schema}" not in listing)
    if missing:
        dump_path.unlink(missing_ok=True)
        raise RuntimeError(f"backup verification missing schemas: {', '.join(missing)}")
    digest = hashlib.sha256()
    with dump_path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    manifest = {
        "status": "verified",
        "created_at": reference.isoformat(),
        "dump_path": str(dump_path),
        "byte_count": dump_path.stat().st_size,
        "sha256": digest.hexdigest(),
        "format": "postgresql-custom",
        "schemas": sorted(required_schemas),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
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
