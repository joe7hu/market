import psycopg
import pytest

from investment_panel.infrastructure.postgres.migrations import HEAD_REVISION


@pytest.mark.parametrize("attempt", range(2))
def test_migrated_clones_are_fresh_and_raw_databases_stay_blank(
    migrated_postgres_dsn: str, postgres_dsn: str, attempt: int,
) -> None:
    with psycopg.connect(migrated_postgres_dsn) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION
        assert connection.execute("SELECT to_regclass('public.fixture_probe')").fetchone()[0] is None
        connection.execute("CREATE TABLE public.fixture_probe (value integer)")
        connection.execute("INSERT INTO public.fixture_probe VALUES (%s)", [attempt])
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT to_regclass('public.alembic_version')").fetchone()[0] is None
        assert connection.execute("SELECT to_regnamespace('analysis')").fetchone()[0] is None
