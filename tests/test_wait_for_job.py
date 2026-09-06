from contextlib import contextmanager
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts import wait_for_job as waiter


@pytest.mark.parametrize("terminal,expected", [("succeeded", 0), ("failed", 1), ("partial", 1), (None, 1), ("running", 2)])
def test_job_waiter_preserves_result_and_releases_transactions(terminal, expected, monkeypatch, capsys):
    clock = [0.0]
    active = [False]
    states = iter(["running", "running", terminal])
    job_id = UUID(int=1)

    class Runtime:
        @contextmanager
        def read(self):
            active[0] = True
            try:
                yield self
            finally:
                active[0] = False

        def execute(self, sql, parameters):
            assert sql.startswith("SELECT") and parameters == [job_id]
            status = next(states)
            row = {"status": status, "error": None} if status else None
            return SimpleNamespace(fetchone=lambda: row)

    def sleep(seconds):
        assert not active[0]
        clock[0] += seconds

    monkeypatch.setattr(waiter, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    assert waiter.wait_for_job(Runtime(), job_id, timeout=2, interval=1) == expected
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(output) == 2  # Repeated running state produces no duplicate output.
    assert output[-1]["status"] == ("wait_timeout" if expected == 2 else terminal or "missing")


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_waiter_rejects_unbounded_or_invalid_time(value):
    with pytest.raises(waiter.argparse.ArgumentTypeError):
        waiter.positive_seconds(value)
