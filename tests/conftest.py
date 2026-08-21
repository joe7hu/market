"""Pytest session setup: a fast default inner loop plus an opt-in slow tier.

Two things make the suite painful to iterate on:

1. A couple of end-to-end integration tests (building candidates from a local
   Arco fixture, the free-source round-trip) do ~90s of real computation each.
   They are marked ``@pytest.mark.slow`` and skipped by default. Run the full
   suite with ``uv run pytest --run-slow``.

So ``uv run pytest`` is the fast loop (~40s); ``uv run pytest --run-slow`` is the
complete run for CI / pre-push.
"""

from __future__ import annotations

import pytest

from investment_panel.database.authority import close_cached_runtimes
from investment_panel.database.migrations import upgrade_database


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="run tests marked @pytest.mark.slow (heavy end-to-end integration)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "slow: heavy end-to-end integration test; skipped unless --run-slow is passed",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-slow"):
        return
    skip_slow = pytest.mark.skip(reason="slow integration test; pass --run-slow to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


@pytest.fixture(autouse=True)
def _close_postgresql_runtime_pools() -> None:
    yield
    close_cached_runtimes()


@pytest.fixture
def postgres_dsn(postgresql) -> str:
    info = postgresql.info
    credentials = info.user if not info.password else f"{info.user}:{info.password}"
    return f"postgresql://{credentials}@{info.host}:{info.port}/{info.dbname}"


@pytest.fixture
def migrated_postgres_dsn(postgres_dsn: str) -> str:
    upgrade_database(postgres_dsn)
    return postgres_dsn
