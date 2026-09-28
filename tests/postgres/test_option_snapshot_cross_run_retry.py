"""Replay idempotence must preserve usable panels without backdating corrections."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.panel_option_queries import current_option_query
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


@pytest.fixture
def capture(application_postgres_dsn):
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    try:
        repository = IngestionRepository(runtime)
        repository.register_source("option-cross-run", name="Audit", family="broker", kind="option_chain")
        at = datetime.now(UTC) - timedelta(minutes=5)
        row = {"symbol": "QQQ", "expiration": (at + timedelta(days=30)).date().isoformat(),
               "strike": 500, "option_type": "call", "provider_symbol": "cross-run-contract",
               "bid": 1.125, "ask": 1.375, "style": "american", "settlement": "physical",
               "deliverable_key": "cross-run-standard", "standard_contract_verified": True,
               "provider_payload": {"instrument": {"id": "cross-run-contract"}, "revision": 1}}
        kwargs = {"source_id": "option-cross-run", "observed_at": at, "market_session": "regular",
                  "universe": "audit", "rows": [row]}
        yield runtime, repository, kwargs
    finally:
        runtime.close()


def _store(repository, kwargs):
    run_id = repository.start_run("option-cross-run", "option_quotes")
    repository.store_option_snapshot(run_id, **kwargs)
    return run_id


def _state(runtime):
    with runtime.read() as connection:
        snapshot = connection.execute("""SELECT id, xmin::text, ctid::text, ingest_run_id,
            completeness, contract_count FROM raw.option_snapshot""").fetchone()
        quotes = connection.execute("""SELECT id, xmin::text, ctid::text, observed_at,
            bid, provider_payload FROM raw.option_quote ORDER BY id""").fetchall()
        panel = connection.execute(current_option_query("options_chain")).fetchall()
    return snapshot, quotes, panel


@pytest.mark.parametrize("status", ["succeeded", "partial"])
def test_identical_retry_keeps_usable_option_capture_and_panel(capture, status):
    runtime, repository, kwargs = capture
    original = _store(repository, kwargs)
    repository.finish_run(original, status)
    before = _state(runtime)
    assert len(before[2]) == 1
    retry = _store(repository, kwargs)
    assert _state(runtime) == before
    repository.finish_run(retry, "failed")
    assert _state(runtime) == before
    successful_retry = _store(repository, kwargs)
    repository.finish_run(successful_retry, "succeeded")
    assert _state(runtime) == before
    assert before[2][0]["source_version"] == str(original)


@pytest.mark.parametrize("status", ["failed", "skipped", "running"])
def test_identical_option_retry_repairs_unusable_provenance(capture, status):
    runtime, repository, kwargs = capture
    original = _store(repository, kwargs)
    if status != "running":
        repository.finish_run(original, status)
    before = _state(runtime)
    assert before[2] == []
    recovered = _store(repository, kwargs)
    repository.finish_run(recovered, "succeeded")
    after = _state(runtime)
    assert after[0]["id"] == before[0]["id"]
    assert after[0]["ingest_run_id"] == recovered
    assert after[1] == before[1]
    assert len(after[2]) == 1 and after[2][0]["source_version"] == str(recovered)
    again = _store(repository, kwargs)
    repository.finish_run(again, "succeeded")
    assert _state(runtime) == after


@pytest.mark.parametrize("change", ["bid", "provider_payload", "new_observation", "snapshot_metadata"])
def test_real_option_correction_acquires_new_run_not_old_availability(capture, change):
    runtime, repository, kwargs = capture
    original = _store(repository, kwargs)
    repository.finish_run(original, "succeeded")
    before = _state(runtime)
    if change == "bid":
        kwargs["rows"][0]["bid"] = 1.25
    elif change == "provider_payload":
        kwargs["rows"][0]["provider_payload"] = {"instrument": {"id": "cross-run-contract"}, "revision": 2}
    elif change == "new_observation":
        kwargs["rows"][0]["provider_observed_at"] = kwargs["observed_at"] + timedelta(seconds=1)
    else:
        kwargs["completeness"] = 0.95
    retry_kwargs = deepcopy(kwargs)
    corrected = _store(repository, kwargs)
    assert _state(runtime)[2] == []  # Do not backdate changed facts into the old good run.
    repository.finish_run(corrected, "succeeded")
    after = _state(runtime)
    assert after[0]["id"] == before[0]["id"]
    assert after[0]["ingest_run_id"] == corrected
    assert len(after[2]) == 1 and after[2][0]["source_version"] == str(corrected)
    assert after[2][0]["available_at"] > before[2][0]["available_at"]
    if change == "bid":
        assert float(after[2][0]["bid"]) == 1.25
    elif change == "provider_payload":
        assert after[1][0]["provider_payload"]["revision"] == 2
    elif change == "new_observation":
        assert len(after[1]) == 2
        assert after[2][0]["observed_at"] == kwargs["rows"][0]["provider_observed_at"]
    else:
        assert after[0]["completeness"] == 0.95
    again = _store(repository, retry_kwargs)
    repository.finish_run(again, "succeeded")
    assert _state(runtime) == after
