"""Bounded, self-describing row packs; original PostgreSQL JSON never reserializes.

Packs amortize NAS directory/manifest operations across hundreds of rows. They
are cold artifacts, not a second serving database. The caller owns source locks
and commits source mutation only after every pack verifies.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService

MAX_PACK_BYTES = 8 * 1024**2
MAX_PACK_ROWS = 500
RELATIONS = frozenset({
    "raw.option_quote", "analysis.option_relative_value", "analysis.run",
    "analysis.ticker_decision", "app.publication", "app.publication_bundle",
    "app.publication_bundle_item", "app.publication_payload", "app.publication_item",
})


class RowArchive:
    def __init__(self, service: StorageArchiveService, *, kind: str = "derived") -> None:
        self.service = service
        if kind not in {"derived", "publications"}:
            raise ValueError("unsupported row archive kind")
        self.kind = kind

    def write(self, connection: Any, relation: str, records: Iterable[Any]) -> list[int]:
        if relation not in RELATIONS:
            raise ValueError("row archive relation is not allowed")
        columns = [dict(row) for row in connection.execute("""
            SELECT attname AS name, format_type(atttypid, atttypmod) AS type
            FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped
            ORDER BY attnum
        """, [relation]).fetchall()]
        identity = connection.execute("SELECT current_database() AS database_name, oid AS database_oid FROM pg_database WHERE datname = current_database()").fetchone()
        ids: list[int] = []
        pack: list[dict[str, Any]] = []
        size = 2

        def flush() -> None:
            artifact = self.service._write_json_gzip(
                self.kind, pack, source_relation=relation, row_count=len(pack),
                metadata={"archive_contract": "postgres-row-pack.v1", "columns": columns,
                          "source_database": dict(identity)},
            )
            manifest_id = int(artifact["manifest_id"])
            if self.service.verify(manifest_id=manifest_id)["verified"] != 1:
                raise ValueError("row archive verification failed; source rows retained")
            ids.append(manifest_id)

        for record in records:
            raw = str(record["row_json"])
            if len(raw.encode("utf-8")) > MAX_PACK_BYTES:
                raise ValueError("individual source row exceeds the 8 MiB archive budget")
            entry = {"relation": relation, "row_json": raw, "columns": columns,
                     "source_database": dict(identity), "archive_contract": "postgres-row-pack.v1"}
            entry_size = len(json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
            if entry_size + 2 > MAX_PACK_BYTES:
                raise ValueError("individual source row exceeds the 8 MiB archive budget including schema")
            if pack and (size + entry_size + 1 > MAX_PACK_BYTES or len(pack) >= MAX_PACK_ROWS):
                flush()
                pack, size = [], 2
            pack.append(entry)
            size += entry_size + 1
        if pack:
            flush()
        return ids
