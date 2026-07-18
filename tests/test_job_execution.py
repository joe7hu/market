from __future__ import annotations

import os

from investment_panel.core.job_execution import RefreshProcessSpec, process_command, process_environment


def test_process_spec_keeps_database_credentials_in_environment() -> None:
    spec = RefreshProcessSpec(
        job_id="job-1",
        job_name="update_market_data",
        database_url="postgresql://market:secret@db.internal/market",
        config_path="config.yaml",
        database_reference="public-reference",
    )

    command = process_command(spec)
    environment = process_environment(spec, {"PYTHONPATH": "existing"})

    assert "secret" not in " ".join(command)
    assert environment["MARKET_DATABASE_URL"].endswith("@db.internal/market")
    assert environment["PYTHONPATH"].split(os.pathsep)[-1] == "existing"


def test_sync_and_async_paths_share_the_same_process_command() -> None:
    spec = RefreshProcessSpec("job-1", "update_market_data", "postgresql:///market", "config.yaml", "db")
    command = process_command(spec)
    assert command[:3] == [spec.python_executable, "-m", "investment_panel.core.refresh_jobs"]
    assert command[-2:] == ["--config", "config.yaml"]
