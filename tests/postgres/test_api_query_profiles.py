"""Bounded user reads avoid JIT compilation without weakening timeouts."""
from contextlib import contextmanager

import pytest

from investment_panel.infrastructure.postgres.runtime import API_PROFILE, JOB_PROFILE, DatabaseRuntime
from investment_panel.infrastructure.postgres.panel_models import TODAY_AUTHORITY_PROFILE
from investment_panel.infrastructure.postgres.experiment_events import experiment_history, experiment_progress
from investment_panel.infrastructure.postgres.paper_workbench import PaperWorkbenchRepository


@pytest.mark.parametrize("context", ["read", "snapshot", "transaction"])
def test_api_jit_policy_and_timeouts_do_not_leak_to_worker_transactions(migrated_postgres_dsn, context):
    runtime = DatabaseRuntime(migrated_postgres_dsn, max_size=1)
    runtime.open()
    try:
        with runtime.pool.connection() as connection:
            connection.execute("SET jit = on")
        with getattr(runtime, context)() as connection:
            assert connection.execute("SHOW jit").fetchone()["jit"] == "off"
            assert connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "3s"
            assert connection.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "2s"
        with runtime.read(JOB_PROFILE) as connection:
            assert connection.execute("SHOW jit").fetchone()["jit"] == "on"
            assert connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "15min"
        with runtime.snapshot(TODAY_AUTHORITY_PROFILE) as connection:
            assert connection.execute("SHOW jit").fetchone()["jit"] == "off"
            assert connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "10s"
        with pytest.raises(RuntimeError, match="rollback"):
            with runtime.transaction() as connection:
                assert connection.execute("SHOW jit").fetchone()["jit"] == "off"
                raise RuntimeError("rollback")
        with runtime.pool.connection() as connection:
            assert connection.execute("SHOW jit").fetchone()["jit"] == "on"
    finally:
        runtime.close()


def test_experiment_http_and_health_reads_use_api_not_worker_budget(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    profiles = []

    class BoundedRuntime:
        @contextmanager
        def snapshot(self, profile=API_PROFILE):
            profiles.append(profile)
            with runtime.snapshot(profile) as connection:
                yield connection
    bounded = BoundedRuntime()
    try:
        assert experiment_history(bounded, observation_id="00000000-0000-0000-0000-000000000000") is None
        assert experiment_progress(bounded)["active"] == 0
        assert PaperWorkbenchRepository(bounded).observations()["total"] == 0
        assert len(profiles) == 3 and all(profile == API_PROFILE for profile in profiles)
    finally:
        runtime.close()
