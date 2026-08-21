from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_production_dependencies_are_postgresql_only() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = [*project["dependencies"], *project["optional-dependencies"]["test"]]
    assert all("postgres" in dependency or not dependency.startswith("duck") for dependency in dependencies)


def test_fastapi_import_does_not_load_retired_storage() -> None:
    script = """
import app.main
from investment_panel.jobs import full_market_refresh, hourly_options_radar, premarket_options_intelligence
from investment_panel.jobs import update_arco_sources, update_broker_sources, update_content_sources
from investment_panel.jobs import update_disclosure_sources, update_market_data, update_market_events
assert app.main.app is not None
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
