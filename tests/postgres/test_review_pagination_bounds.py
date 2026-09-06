"""Live review pages use stable keys and never copy the collection."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import psycopg
import pytest

from conftest import typed_config
from investment_panel.database import panel_pagination


def test_review_pages_above_50k_use_stable_keys_and_live_values(
    migrated_postgres_dsn, application_postgres_dsn, monkeypatch,
):
    with psycopg.connect(migrated_postgres_dsn) as connection:
        connection.execute(
            "CREATE TABLE public.review_source "
            "(id uuid PRIMARY KEY, created_at timestamptz NOT NULL, value text)"
        )
        connection.execute(
            "INSERT INTO public.review_source SELECT lpad(to_hex(n), 32, '0')::uuid, "
            "now() - interval '1 day', 'original' FROM generate_series(1, 50003) n"
        )
        connection.execute("GRANT SELECT ON public.review_source TO market_app")
        connection.execute("ANALYZE public.review_source")
    monkeypatch.setitem(panel_pagination.QUERY_POLICIES, "candidate_event_mark", SimpleNamespace(
        query="SELECT decision.id, decision.value FROM public.review_source decision "
              "ORDER BY decision.created_at DESC", custom_loader=None,
    ))
    config = typed_config(application_postgres_dsn)
    now = datetime.now(UTC)
    first, count, cursor = panel_pagination.load_postgres_table_page(
        config, "candidate_event_mark", limit=25, snapshot_at=now,
    )
    assert count == 50003
    assert [row["id"].int for row in first] == list(range(50003, 49978, -1))
    assert all(set(row) == {"id", "value"} for row in first)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        connection.execute("UPDATE public.review_source SET value = 'changed' WHERE id = %s", [UUID(int=49978)])
        connection.execute(
            "INSERT INTO public.review_source VALUES (%s, now() + interval '1 day', 'future')",
            [UUID(int=49979 + 100000)],
        )
    second, second_count, _ = panel_pagination.load_postgres_table_page(
        config, "candidate_event_mark", limit=25, snapshot_at=now, after=cursor,
    )
    assert second_count == count
    assert [row["id"].int for row in second] == list(range(49978, 49953, -1))
    assert second[0]["value"] == "changed"
    last, last_count, next_cursor = panel_pagination.load_postgres_table_page(
        config, "candidate_event_mark", limit=25, snapshot_at=now,
        after=("candidate_event_mark", str(UUID(int=4))),
    )
    assert last_count == count and next_cursor is None
    assert [row["id"].int for row in last] == [3, 2, 1]
    with psycopg.connect(migrated_postgres_dsn) as connection:
        plan = connection.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON) SELECT * FROM public.review_source "
            "WHERE created_at <= %s AND id < %s ORDER BY id DESC LIMIT 26",
            [now, UUID(int=49979)],
        ).fetchone()[0][0]["Plan"]
        assert plan["Actual Rows"] == 26 and "Index" in str(plan)
    for after in [("candidate_event_mark", "invalid"), ("different_collection", str(UUID(int=4)))]:
        with pytest.raises(ValueError, match="invalid review cursor"):
            panel_pagination.load_postgres_table_page(
                config, "candidate_event_mark", limit=25, snapshot_at=now, after=after,
            )
    with pytest.raises(ValueError, match="expired"):
        panel_pagination.load_postgres_table_page(
            config, "candidate_event_mark", limit=25,
            snapshot_at=now - timedelta(minutes=31), after=cursor,
        )


@pytest.mark.parametrize("collection", [*panel_pagination.REVIEW_PAGE_KEYS, "strategy_cohort_result"])
def test_review_collection_queries_keep_the_application_role_contract(application_postgres_dsn, collection):
    rows, count, cursor = panel_pagination.load_postgres_table_page(
        typed_config(application_postgres_dsn), collection, limit=25, snapshot_at=datetime.now(UTC),
    )
    assert rows == [] and count == 0 and cursor is None
