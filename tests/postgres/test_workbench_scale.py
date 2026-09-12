"""Opt-in representative scale check; all rows live in a disposable test database."""
import json
import platform
from time import perf_counter

import pytest

from investment_panel.infrastructure.postgres.paper_workbench import PaperWorkbenchRepository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


@pytest.mark.slow
def test_workbench_ten_thousand_trades_million_marks(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            connection.execute("SET LOCAL statement_timeout = '180s'")
            connection.execute("INSERT INTO catalog.instrument (symbol, name, asset_class) SELECT 'WBENCH' || n, 'Workbench scale fixture', 'equity' FROM generate_series(1, 100) n")
            connection.execute("INSERT INTO ingest.source (id, name, family, kind, operational_state, health_owner, freshness_seconds) VALUES ('workbench-scale', 'Deterministic scale fixture', 'test', 'quote', 'active', 'test', 3600)")
            run = connection.execute("INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status) VALUES ('workbench-scale', 'quotes', now() - interval '4 hours', now() - interval '1 minute', 'succeeded') RETURNING id").fetchone()["id"]
            connection.execute("""INSERT INTO raw.quote (instrument_id, source_id, ingest_run_id, observed_at, price, available_at)
                SELECT instrument.id, 'workbench-scale', %s, now() - n * interval '1 second' - interval '2 minutes', 12, now() - interval '1 minute'
                FROM catalog.instrument instrument CROSS JOIN generate_series(1, 10000) n WHERE instrument.symbol LIKE 'WBENCH%%'""", [run])
            connection.execute("INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id) SELECT id, available_at, %s FROM raw.quote WHERE source_id = 'workbench-scale'", [run])
            connection.execute("""INSERT INTO app.paper_order (instrument_id, side, quantity, limit_price, status, paper_only, structure)
                SELECT instrument.id, 'buy', 1, 10, 'entered', true, 'equity' FROM catalog.instrument instrument CROSS JOIN generate_series(1, 100) n WHERE instrument.symbol LIKE 'WBENCH%'""")
            connection.execute("""INSERT INTO app.trade_journal (instrument_id, action, quantity, price, rationale, details)
                SELECT instrument_id, 'paper_entry', 1, 10, 'deterministic_options_paper_execution', jsonb_build_object('paper_order_id', id, 'fees', 0.5) FROM app.paper_order""")
            connection.execute("ANALYZE app.paper_order")
            connection.execute("ANALYZE app.trade_journal")
            connection.execute("ANALYZE raw.quote")
        repository = PaperWorkbenchRepository(runtime)
        first = repository.trades()
        identity = first["rows"][0]["paper_order_id"]
        timings = {}
        for name, call in (("summary", repository.performance), ("detail", lambda: repository.trade(identity)), ("page", repository.trades)):
            call()
            started = perf_counter()
            result = call()
            timings[name] = round(perf_counter() - started, 4)
            if name == "summary":
                assert result["counts"]["total_orders"] == 10000
                assert result["counts"]["filled_orders"] == 10000
                assert result["unrealized_pnl"] == 15000
        print("WORKBENCH_BENCHMARK " + json.dumps({"platform": platform.platform(), "trades": 10000, "marks": 1000000, "warm_seconds": timings}))
    finally:
        runtime.close()
