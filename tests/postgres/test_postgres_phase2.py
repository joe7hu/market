from __future__ import annotations

from datetime import UTC, datetime

import pytest

from investment_panel.core.phase2 import EventObservation
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.phase2 import Phase2Repository
from investment_panel.database.runtime import DatabaseRuntime


def test_phase2_event_fields_lineage_and_divergent_identity_are_authoritative(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    repository = Phase2Repository(runtime)
    now = datetime(2026, 9, 2, 14, tzinfo=UTC)
    event = EventObservation(
        observation_id="phase2-event-persistence-test",
        field_name="event.actual",
        dimension="event risk",
        asset_class="macro",
        source_id="trading_economics",
        source_version="test.v1",
        value=3.2,
        observed_at=now,
        available_at=now,
        release_at=now,
        actual=3.2,
        consensus=3.0,
        surprise=0.2,
        revision=-0.1,
        content_hash="a" * 64,
        )
    try:
        ingestion.register_source(
            "trading_economics", name="TE test seam", family="phase2", kind="test",
            operational_state="active", health_owner="update_phase2_sources", freshness_seconds=86400,
        )
        with ingestion.run("trading_economics", "phase2-test") as source_run:
            payload_id = ingestion.record_payload(
                source_run.id, "phase2-test/event.json", sha256="b" * 64,
                byte_count=1, schema_version="phase2-test.v1",
            )
            repository.record_observations((event,), ingest_run_id=str(source_run.id), payload_id=payload_id, parent_snapshot_id="snapshot-parent")
            source_run.finish("succeeded")
        with runtime.read() as connection:
            row = connection.execute(
                """SELECT actual, consensus, surprise, revision, ingest_run_id::text AS ingest_run_id,
                          content_hash, parent_snapshot_id
                   FROM raw.market_observation WHERE observation_id = %s""",
                [event.observation_id],
            ).fetchone()
        assert dict(row) == {
            "actual": 3.2, "consensus": 3.0, "surprise": 0.2, "revision": -0.1,
            "ingest_run_id": str(source_run.id), "content_hash": "a" * 64,
            "parent_snapshot_id": "snapshot-parent",
        }
        with pytest.raises(ValueError, match="identity conflicts"):
            repository.record_observations(
                (event.model_copy(update={"actual": 9.9}),),
                ingest_run_id=str(source_run.id),
                payload_id=payload_id,
                parent_snapshot_id="snapshot-parent",
            )
    finally:
        runtime.close()


def test_phase2_market_app_has_writers_but_artifacts_remain_immutable(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.read() as connection:
            privileges = connection.execute(
                """SELECT has_table_privilege('market_app', 'raw.market_observation', 'INSERT') AS raw_insert,
                          has_table_privilege('market_app', 'analysis.market_state_posterior', 'INSERT') AS posterior_insert,
                          has_table_privilege('market_app', 'ingest.source_lifecycle_history', 'INSERT') AS lifecycle_insert""",
            ).fetchone()
        assert privileges["raw_insert"] and privileges["posterior_insert"]
        assert not privileges["lifecycle_insert"]
    finally:
        runtime.close()
