"""Health failures: concurrent marks, incomplete quote batches, legacy bindings."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from time import sleep
from uuid import uuid4

import psycopg
import pytest

from investment_panel.core.options_recovery_registry import strategies
from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.instruments import reconcile_instrument
from investment_panel.infrastructure.postgres.options_history import OptionHistoryRepository
from investment_panel.infrastructure.postgres.options_publication import OPTION_SUBSET_KEYS
from investment_panel.infrastructure.postgres import options
from investment_panel.infrastructure.postgres.options_recovery_execution import RecoveryExecutionRepository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.jobs import paper_quotes
from investment_panel.settings import load_config


def test_concurrent_paper_marks_preserve_confirmed_quote(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("health-quotes", name="Health quotes", family="market", kind="quote", operational_state="active", health_owner="refresh_assessment_inputs", freshness_seconds=60)
        run = ingestion.start_run("health-quotes", "quotes")
        ingestion.store_quotes(run, "health-quotes", [{"symbol": "HEALTHMARK", "price": 123, "observed_at": datetime.now(UTC)}])
        ingestion.finish_run(run, "succeeded")
        with runtime.transaction() as connection:
            instrument = reconcile_instrument(connection, "HEALTHMARK")
            assert connection.execute("SELECT price FROM analysis.paper_current_mark_projection WHERE instrument_id = %s", [instrument]).fetchone()["price"] == 123
            connection.execute("DELETE FROM analysis.paper_current_mark_projection WHERE instrument_id = %s", [instrument])
        with psycopg.connect(migrated_postgres_dsn) as first, psycopg.connect(migrated_postgres_dsn) as observer:
            first.execute("SELECT analysis.refresh_paper_current_marks(%s)", [[instrument]])
            def refresh():
                with psycopg.connect(migrated_postgres_dsn, application_name="health-mark-contender") as second:
                    second.execute("SELECT analysis.refresh_paper_current_marks(%s)", [[instrument]])
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(refresh)
                # Wait for the actual database lock, not an arbitrary delay.
                for _ in range(200):
                    blocked = observer.execute("SELECT 1 FROM pg_stat_activity WHERE application_name = 'health-mark-contender' AND wait_event_type = 'Lock'").fetchone()
                    observer.commit()
                    if blocked:
                        break
                    sleep(0.01)
                assert blocked
                new_run = first.execute("INSERT INTO ingest.run (source_id, capability, status, started_at) VALUES ('health-quotes', 'quotes', 'running', clock_timestamp()) RETURNING id").fetchone()[0]
                fact, available = first.execute("INSERT INTO raw.quote (instrument_id, source_id, ingest_run_id, observed_at, price) VALUES (%s, 'health-quotes', %s, clock_timestamp(), 234) RETURNING id, available_at", [instrument, new_run]).fetchone()
                first.execute("INSERT INTO raw.quote_fact_availability (fact_id, fact_available_at, ingest_run_id) VALUES (%s, %s, %s)", [fact, available, new_run])
                first.execute("UPDATE ingest.run SET status = 'succeeded', finished_at = clock_timestamp() WHERE id = %s", [new_run])
                first.commit()
                future.result(timeout=5)
        with runtime.read() as connection:
            mark = connection.execute("SELECT price FROM analysis.paper_current_mark_projection WHERE instrument_id = %s", [instrument]).fetchone()
        assert mark["price"] == 234
    finally:
        runtime.close()


def test_active_native_quote_batch_covers_more_than_twenty_symbols(monkeypatch):
    config = load_config(None)
    contracts = [{"symbol": f"SYM{i}", "contract_id": i + 1, "provider_instrument_id": str(uuid4())} for i in range(44)]
    monkeypatch.setattr(paper_quotes, "load_config", lambda _: config)
    monkeypatch.setattr(paper_quotes, "is_market_open", lambda _: True)
    monkeypatch.setattr(paper_quotes, "active_paper_contracts", lambda *_: contracts)
    monkeypatch.setattr(paper_quotes, "runtime_for_config", lambda _: None)
    monkeypatch.setattr(paper_quotes, "OptionHistoryPolicyRepository", lambda _: SimpleNamespace(
        acquire_provider_lease=lambda **_: SimpleNamespace(id=1), release_provider_lease=lambda _: None))
    selected = []
    def collect(_provider, _symbols, **kwargs):
        selected.extend(kwargs["required_contracts"])
        return {"symbols_attempted": [row["symbol"] for row in selected]}
    monkeypatch.setattr(paper_quotes, "collect_robinhood_option_chains", collect)
    monkeypatch.setattr(paper_quotes, "persist_collected_option_chains", lambda *_args, **_: {"matched_contract_ids": [row["contract_id"] for row in selected]})
    result = paper_quotes.run()
    assert result["status"] == "ok"
    assert result["contracts_captured"] == result["contracts_required"] == 44


def test_option_mark_refresh_joins_the_snapshot_writer_lock(migrated_postgres_dsn):
    with psycopg.connect(migrated_postgres_dsn) as writer, psycopg.connect(migrated_postgres_dsn) as observer:
        writer.execute("SELECT pg_advisory_xact_lock(hashtextextended('raw.option_quote.partition', 0))")
        def refresh():
            with psycopg.connect(migrated_postgres_dsn, application_name="health-option-mark-contender") as contender:
                contender.execute("SET LOCAL statement_timeout = '5s'")
                contender.execute("SELECT analysis.refresh_paper_option_marks('{}'::bigint[])")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(refresh)
            blocked = None
            try:
                for _ in range(200):
                    blocked = observer.execute("SELECT 1 FROM pg_stat_activity WHERE application_name = 'health-option-mark-contender' AND wait_event_type = 'Lock'").fetchone()
                    observer.commit()
                    if blocked or future.done():
                        break
                    sleep(0.01)
            finally:
                writer.commit()
            future.result(timeout=5)
            assert blocked


def test_terminal_run_takes_writer_lock_before_current_marks(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("lock-order", name="Lock order", family="market", kind="quote", operational_state="active", health_owner="refresh_assessment_inputs", freshness_seconds=60)
        for price in (100, 101):
            run = ingestion.start_run("lock-order", "quotes")
            ingestion.store_quotes(run, "lock-order", [{"symbol": "LOCKORDER", "price": price, "observed_at": datetime.now(UTC)}])
            if price == 100:
                ingestion.finish_run(run, "succeeded")
        with runtime.read() as connection:
            instrument = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = 'LOCKORDER'").fetchone()["id"]
        with psycopg.connect(migrated_postgres_dsn) as writer, psycopg.connect(migrated_postgres_dsn) as observer:
            writer.execute("SELECT pg_advisory_xact_lock(hashtextextended('raw.option_quote.partition', 0))")
            def finish():
                with psycopg.connect(migrated_postgres_dsn, application_name="health-run-contender") as contender:
                    contender.execute("SET LOCAL statement_timeout = '5s'")
                    contender.execute("UPDATE ingest.run SET status = 'succeeded', finished_at = clock_timestamp() WHERE id = %s", [run])
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(finish)
                try:
                    for _ in range(200):
                        blocked = observer.execute("SELECT 1 FROM pg_stat_activity WHERE application_name = 'health-run-contender' AND wait_event_type = 'Lock'").fetchone()
                        observer.commit()
                        if blocked:
                            break
                        sleep(0.01)
                    assert blocked
                    observer.execute("SELECT instrument_id FROM analysis.paper_current_mark_projection WHERE instrument_id = %s FOR UPDATE NOWAIT", [instrument])
                    observer.commit()
                finally:
                    writer.commit()
                future.result(timeout=5)
    finally:
        runtime.close()


@pytest.mark.parametrize("terminal", ["fail", "defer", "stale"])
def test_capture_terminal_takes_writer_lock_before_snapshot(migrated_postgres_dsn, terminal):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("capture-lock", name="Capture lock", family="market", kind="option_chain", operational_state="active", health_owner="refresh_option_history", freshness_seconds=60)
        run = ingestion.start_run("capture-lock", "options")
        repository = OptionHistoryRepository(runtime)
        slot = datetime.now(UTC)
        snapshot = repository.claim_slot(source_id="capture-lock", symbol="CAPTURELOCK", slot_at=slot, run_id=run)
        assert snapshot
        def finish():
            if terminal == "stale":
                repository.defer_stale_running_captures(source_id="capture-lock", collection_profile="history_full", stale_after=timedelta(0), now=slot + timedelta(minutes=10))
            else:
                arguments = dict(source_id="capture-lock", symbol="CAPTURELOCK", slot_at=slot, run_id=run)
                if terminal == "fail":
                    repository.fail_capture(**arguments, error="test failure")
                else:
                    repository.defer_capture(**arguments, reason="test defer")
        with psycopg.connect(migrated_postgres_dsn) as writer, psycopg.connect(migrated_postgres_dsn) as observer:
            writer.execute("SELECT pg_advisory_xact_lock(hashtextextended('raw.option_quote.partition', 0))")
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(finish)
                try:
                    for _ in range(100):
                        blocked = observer.execute("SELECT 1 FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND wait_event = 'advisory'").fetchone()
                        observer.commit()
                        if blocked or future.done():
                            break
                        sleep(0.01)
                    assert blocked
                    observer.execute("SELECT id FROM raw.option_snapshot WHERE id = %s FOR UPDATE NOWAIT", [snapshot])
                    observer.commit()
                finally:
                    writer.commit()
                future.result(timeout=5)
    finally:
        runtime.close()


def test_active_contract_lookup_does_not_read_its_full_quote_history(migrated_postgres_dsn, monkeypatch):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("bounded-quotes", name="Bounded quotes", family="market", kind="option_chain", operational_state="active", health_owner="refresh_paper_quotes", freshness_seconds=60)
        now = datetime.now(UTC)
        with ingestion.run("bounded-quotes", "option_quotes") as run:
            capture = ingestion.store_option_snapshot(run.id, source_id="bounded-quotes", observed_at=now,
                market_session="regular", universe="test", rows=[{"symbol": "BOUNDED", "expiration": (now.date() + timedelta(days=30)).isoformat(), "strike": 100, "option_type": "call", "bid": 1, "ask": 1.1}])
        with runtime.transaction() as connection:
            contract = connection.execute("SELECT id, underlying_instrument_id FROM catalog.option_contract WHERE strike = 100").fetchone()
            order = connection.execute("INSERT INTO app.paper_order (instrument_id, side, quantity, status, lane, paper_only) VALUES (%s, 'buy', 1, 'staged', 'radar', true) RETURNING id", [contract["underlying_instrument_id"]]).fetchone()["id"]
            connection.execute("INSERT INTO app.paper_order_leg (paper_order_id, leg_index, contract_id, option_type, side, strike, bid, ask, bid_size, ask_size, quote_time) VALUES (%s, 0, %s, 'call', 'buy', 100, 1, 1.1, 10, 10, %s)", [order, contract["id"], now])
            connection.execute("INSERT INTO raw.option_quote (observed_at, available_at, snapshot_id, contract_id, mid) SELECT %s::timestamptz - n * interval '1 second', %s, %s, %s, 1.05 FROM generate_series(1, 1000) n", [now, now, capture["snapshot_id"], contract["id"]])
            connection.execute("ANALYZE raw.option_quote")
        queries = []
        @contextmanager
        def read():
            with runtime.read() as connection:
                connection.execute("SET LOCAL enable_seqscan = off")
                def execute(query, parameters):
                    queries.append(connection.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + query, parameters).fetchone()["QUERY PLAN"][0]["Plan"])
                    return connection.execute(query, parameters)
                yield SimpleNamespace(execute=execute)
        monkeypatch.setattr(options, "runtime_for_config", lambda _: SimpleNamespace(read=read))
        rows = options.active_paper_contracts(load_config(None), "bounded-quotes")
        assert [row["contract_id"] for row in rows] == [contract["id"]]
        def quote_reads(plan):
            count = plan.get("Actual Rows", 0) * plan.get("Actual Loops", 0) if plan.get("Relation Name", "").startswith("option_quote") else 0
            return count + sum(quote_reads(child) for child in plan.get("Plans", []))
        assert quote_reads(queries[0]) < 16
    finally:
        runtime.close()


def test_price_job_waits_for_a_normal_serial_writer(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("writer-wait", name="Writer wait", family="market", kind="quote", operational_state="active", health_owner="refresh_paper_quotes", freshness_seconds=60)
        run = ingestion.start_run("writer-wait", "quotes")
        with psycopg.connect(migrated_postgres_dsn) as writer, psycopg.connect(migrated_postgres_dsn) as observer:
            writer.execute("SELECT pg_advisory_xact_lock(hashtextextended('raw.option_quote.partition', 0))")
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(ingestion.store_quotes, run, "writer-wait", [{"symbol": "WAITQUOTE", "price": 456, "observed_at": datetime.now(UTC)}])
                try:
                    for _ in range(100):
                        blocked = observer.execute("SELECT 1 FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND wait_event = 'advisory'").fetchone()
                        observer.commit()
                        if blocked:
                            break
                        sleep(0.01)
                    assert blocked
                    # A valid writer can hold the shared lock beyond the old 2s budget.
                    sleep(2.25)
                finally:
                    writer.commit()
                assert future.result(timeout=5) == 1
        ingestion.finish_run(run, "succeeded")
        with runtime.read() as connection:
            assert connection.execute("SELECT price FROM analysis.paper_current_mark_projection mark JOIN catalog.instrument instrument ON instrument.id = mark.instrument_id WHERE instrument.symbol = 'WAITQUOTE'").fetchone()["price"] == 456
    finally:
        runtime.close()


def test_unrelated_publication_reads_do_not_run_option_projections(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        analysis = AnalysisRepository(runtime)
        cutoff = datetime.now(UTC)
        run = analysis.start_run("health-today", input_cutoff=cutoff, code_version="health-test", inputs={})
        brief = {"brief_date": cutoff.date().isoformat(), "summary": "test brief"}
        analysis.publish(run, "today", {"preopen_daily_brief": [brief]})
        option_run = analysis.start_run("options-radar", input_cutoff=cutoff, code_version="health-test", inputs={})
        candidate = {key: None for keys in OPTION_SUBSET_KEYS.values() for key in keys}
        candidate.update(contract_id="health-option", ticker="HEALTH", candidate_event_id=str(uuid4()))
        option_publication = analysis.publish(option_run, "options-radar", {"candidate_event": [candidate],
            **{model: [{key: candidate[key] for key in keys}] for model, keys in OPTION_SUBSET_KEYS.items()}})
        with runtime.transaction() as connection:
            assert connection.execute("SELECT bundle.projection_version FROM app.publication publication JOIN app.publication_bundle bundle ON bundle.id = publication.bundle_id WHERE publication.id = %s", [option_publication]).fetchone()["projection_version"] == "option-subsets-v1"
            # Any call for an unrelated model is the regression, regardless of data size.
            definition = connection.execute("SELECT pg_get_functiondef('app.option_bundle_projection(uuid)'::regprocedure) AS definition").fetchone()["definition"]
            header = definition[:definition.index("LANGUAGE sql")]
            connection.execute(header + "LANGUAGE plpgsql STABLE STRICT SET search_path = pg_catalog AS $$ BEGIN RAISE EXCEPTION 'unrelated option projection'; END $$;")
        for relation in ("app.publication_content_item", "app.current_publication_item_read"):
            with runtime.read() as connection:
                rows = connection.execute(f"SELECT payload FROM {relation} WHERE model_name = 'preopen_daily_brief'").fetchall()
                assert [row["payload"] for row in rows] == [brief]
        publication = analysis.publication_at_or_before("today", cutoff=datetime.now(UTC))
        assert publication is not None
    finally:
        runtime.close()


def test_recovery_creates_new_binding_without_rewriting_legacy(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        analysis = AnalysisRepository(runtime)
        legacy = {strategy.key: analysis.register_strategy(strategy.key, 1, name=strategy.name,
            parameters=dict(strategy.parameters), implementation_id="unavailable", implementation_version="1") for strategy in strategies()}
        repository = RecoveryExecutionRepository(runtime)
        current = repository._ensure_strategies()
        assert current == repository._ensure_strategies()
        assert all(current[key] != legacy[key] for key in current)
        with runtime.read() as connection:
            rows = connection.execute("SELECT id, revision, implementation_id, implementation_version FROM analysis.strategy_revision WHERE id = ANY(%s)", [list(legacy.values()) + list(current.values())]).fetchall()
        assert all(row["implementation_id"] == "unavailable" for row in rows if row["id"] in legacy.values())
        assert all(row["revision"] == 2 and row["implementation_id"] == "options_recovery" and row["implementation_version"] == "2" for row in rows if row["id"] in current.values())
    finally:
        runtime.close()
