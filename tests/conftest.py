"""Pytest session setup: a fast default inner loop plus an opt-in slow tier.

Two things make the suite painful to iterate on:

1. The yfinance options-chain throttle
   (``free_sources.YFINANCE_OPTION_THROTTLE_SECONDS = 0.4``) runs as real
   ``time.sleep`` per symbol/expiry. It protects the live rate limit but has no
   place slowing tests, so we zero it for every test.

2. A couple of end-to-end integration tests (building candidates from a local
   Arco fixture, the free-source round-trip) do ~90s of real computation each.
   They are marked ``@pytest.mark.slow`` and skipped by default. Run the full
   suite with ``uv run pytest --run-slow``.

So ``uv run pytest`` is the fast loop (~40s); ``uv run pytest --run-slow`` is the
complete run for CI / pre-push.
"""

from __future__ import annotations

import pytest


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
def _zero_yfinance_option_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        import investment_panel.core.free_sources as free_sources_core
    except Exception:  # pragma: no cover - core import optional in some envs
        return
    monkeypatch.setattr(free_sources_core.yfinance_sources, "YFINANCE_OPTION_THROTTLE_SECONDS", 0, raising=False)
