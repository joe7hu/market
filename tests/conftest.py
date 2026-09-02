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

from copy import deepcopy
import os
from pathlib import Path
import tempfile

import pytest

# CI provisions the same explicit app-login contract that production uses.
# These are test credentials only; the migration still requires deployment
# configuration rather than falling back to its migration owner.
os.environ.setdefault("MARKET_APP_LOGIN_ROLE", "market_app")
os.environ.setdefault("MARKET_APP_DATABASE_PASSWORD", "phase1-test-market-app-password")

from investment_panel.core.config import AppConfig, load_config
from investment_panel.database.configuration import DatabaseConfig
from dataclasses import replace
from investment_panel.database.authority import close_cached_runtimes
from investment_panel.database.migrations import upgrade_database


def typed_config(
    dsn: str = "postgresql:///market",
    *,
    raw: dict[str, object] | None = None,
    status_dir: Path | None = None,
) -> AppConfig:
    """Build the typed application config used by direct owner tests."""

    values = deepcopy(raw or {})
    database = values.setdefault("database", {})
    if isinstance(database, dict):
        database["url"] = dsn
    with tempfile.TemporaryDirectory(prefix="market-test-config-") as directory:
        path = Path(directory) / "config.yaml"
        import yaml

        path.write_text(yaml.safe_dump(values), encoding="utf-8")
        config = load_config(path)
    config = replace(config, database=DatabaseConfig(url=dsn))
    if status_dir is not None:
        config = replace(config, nas=replace(config.nas, status_dir=status_dir))
    return config


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
