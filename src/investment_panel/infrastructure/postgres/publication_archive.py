"""Archive publication rows before retention, without putting the NAS on reads.

Each object is a self-describing original database row, not a projection. PostgreSQL-rendered row JSON is retained as text to avoid numeric precision
loss in a JSON decoder. Shared payloads retain their original hashes and are archived only once. Metadata and
payload rows are exported separately, keeping memory bounded by one row rather
than an entire multi-symbol publication. A failed export/verification aborts the
caller's deletion transaction; files already written are safe to reuse.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService


class PublicationArchive:
    def __init__(self, runtime: Any, archive_root: Path) -> None:
        self.service = StorageArchiveService(runtime, archive_root)

    def rows(self, connection: Any, relation: str, predicate: str, parameters: list[Any]) -> int:
        # All SQL fragments are internal constants, never CLI input.
        if relation not in {
            "app.publication", "app.publication_bundle", "app.publication_bundle_item",
            "app.publication_payload", "app.publication_item", "analysis.run",
        }:
            raise ValueError("publication archive relation is not allowed")
        count = 0
        with connection.cursor(name=f"archive_{uuid4().hex}") as cursor:
            cursor.execute(f"SELECT to_jsonb(source)::text AS row_json FROM {relation} source WHERE {predicate}", parameters)
            for record in cursor:
                row_json = str(record["row_json"])
                row = json.loads(row_json)
                artifact = self.service._write_json_gzip(
                    "publications", {"relation": relation, "row_json": row_json},
                    source_relation=relation, row_count=1,
                    metadata={"archive_contract": "publication-row.v2",
                              "source_key": str(row.get("id") or row.get("content_hash") or ""),
                              "archive_phase": "before_retention"},
                )
                checked = self.service.verify(manifest_id=int(artifact["manifest_id"]))
                if checked["verified"] != 1:
                    raise ValueError("publication archive did not verify; source rows retained")
                count += 1
        return count

    def publications(self, connection: Any, candidates: list[Any]) -> list[Any]:
        """Lock publisher scopes, recheck status, and archive exact candidates."""
        scopes = connection.execute(
            "SELECT DISTINCT scope FROM app.publication WHERE id = ANY(%s) ORDER BY scope", [candidates],
        ).fetchall()
        # Cooperate with AnalysisRepository.publish so reactivation cannot race
        # an age-based selection. All scopes use a deterministic lock order.
        for scope in scopes:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"publication:{scope['scope']}"])
        rows = connection.execute(
            """SELECT id, bundle_id, analysis_run_id FROM app.publication publication
               WHERE id = ANY(%s) AND status = 'superseded'
                 AND NOT EXISTS (SELECT 1 FROM analysis.ticker_decision decision
                                 WHERE decision.market_state_publication_id = publication.id)
               ORDER BY id FOR UPDATE""", [candidates],
        ).fetchall()
        ids = [row["id"] for row in rows]
        if not ids:
            return []
        bundle_ids = [row["bundle_id"] for row in rows if row["bundle_id"] is not None]
        self.rows(connection, "analysis.run", "source.id = ANY(%s)", [[row["analysis_run_id"] for row in rows]])
        self.rows(connection, "app.publication", "source.id = ANY(%s)", [ids])
        self.rows(connection, "app.publication_bundle", "source.id = ANY(%s)", [bundle_ids])
        self.rows(connection, "app.publication_bundle_item", "source.bundle_id = ANY(%s)", [bundle_ids])
        self.rows(connection, "app.publication_item", "source.publication_id = ANY(%s)", [ids])
        self.rows(connection, "app.publication_payload", """source.content_hash IN (
            SELECT item.content_hash FROM app.publication_bundle_item item WHERE item.bundle_id = ANY(%s)
        )""", [bundle_ids])
        return ids
