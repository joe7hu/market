from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_duckdb_is_not_a_production_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert all(not dependency.startswith("duckdb") for dependency in project["dependencies"])
    assert any(dependency.startswith("duckdb") for dependency in project["optional-dependencies"]["legacy-import"])


def test_fastapi_import_does_not_load_duckdb() -> None:
    script = """
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'duckdb' or name.startswith('duckdb.'):
        raise ModuleNotFoundError('DuckDB is excluded from the production runtime')
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
import app.main
from investment_panel.jobs import full_market_refresh, hourly_options_radar, premarket_options_intelligence
assert 'duckdb' not in __import__('sys').modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
