from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from investment_panel.core.config import ArcoConfig
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.jobs import update_arco_sources


def test_arco_refresh_records_source_files_and_compact_symbol_evidence(
    migrated_postgres_dsn: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw_dir = tmp_path / "arco"
    raw_dir.mkdir()
    (raw_dir / "signals.json").write_text(
        json.dumps({
            "topics": [],
            "subtopics": [{
                "id": "signal-1", "subtopic": "$NVDA AI infrastructure demand",
                "summary": "Capacity remains constrained", "score": 0.8,
            }],
        }),
        encoding="utf-8",
    )
    (raw_dir / "beliefs.json").write_text(json.dumps({"beliefs": []}), encoding="utf-8")
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    with runtime.transaction() as connection:
        connection.execute(
            "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('NVDA', 'NVIDIA', 'equity')"
        )
    config = SimpleNamespace(
        database=SimpleNamespace(url=migrated_postgres_dsn),
        arco=ArcoConfig(raw_dir=raw_dir),
    )
    monkeypatch.setattr(update_arco_sources, "load_config", lambda _path=None: config)
    monkeypatch.setattr(update_arco_sources, "runtime_for_config", lambda _config: runtime)
    try:
        first = update_arco_sources.run()
        second = update_arco_sources.run()
        assert first["items"] == 1
        assert second["items"] == 1
        with runtime.read() as connection:
            counts = connection.execute(
                """
                SELECT (SELECT count(*) FROM raw.content_item WHERE source_id = 'arco') AS items,
                       (SELECT count(*) FROM raw.content_item_instrument) AS links,
                       (SELECT count(*) FROM ingest.payload) AS payloads
                """
            ).fetchone()
            row = connection.execute(
                "SELECT title, summary, metadata FROM raw.content_item WHERE source_id = 'arco'"
            ).fetchone()
        assert (counts["items"], counts["links"], counts["payloads"]) == (1, 1, 2)
        assert row["title"] == "$NVDA AI infrastructure demand"
        assert "Capacity remains constrained" in row["summary"]
        assert row["metadata"]["raw_payload_location"] == "ingest.payload"
    finally:
        runtime.close()
