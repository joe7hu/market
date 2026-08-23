from __future__ import annotations

from datetime import UTC, datetime, timedelta

from investment_panel.core.decision.ticker import build_ticker_decision
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.ticker_decisions import TickerDecisionRepository
from investment_panel.database.ticker_execution import TickerPaperExecutionRepository
from conftest import typed_config


def test_stock_paper_entry_uses_shared_ticker_loss_budget_and_is_idempotent(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('ACME', 'Acme', 'equity') RETURNING id"
            ).fetchone()
        decision = build_ticker_decision(
            "ACME",
            {
                "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
                "portfolio_summary": [{"net_liquidation": 100_000, "available_at": "2026-08-22T13:55:00Z"}],
                "decision_queue": [{
                    "symbol": "ACME", "stance": "BULLISH", "action": "BUY",
                    "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                    "conviction_tier": "STANDARD",
                    "available_at": "2026-08-22T13:55:00Z",
                }],
            },
            as_of=datetime(2026, 8, 22, 14, tzinfo=UTC),
        )
        config = typed_config(
            migrated_postgres_dsn,
            raw={"analysis": {"options_decision_system": {
                "mode": "paper",
                "ticker_paper_actions_enabled": True,
                "stock_paper_actions_enabled": True,
            }}},
        )
        repository = TickerPaperExecutionRepository(runtime, config)
        result = repository.stage(
            ticker="ACME",
            decision=decision,
            expression_kind="STOCK",
            idempotency_key="acme-entry-1",
            quantity=50,
            limit_price=100,
        )
        replay = repository.stage(
            ticker="ACME",
            decision=decision,
            expression_kind="STOCK",
            idempotency_key="acme-entry-1",
            quantity=50,
            limit_price=100,
        )

        assert result["status"] == "staged"
        assert result["paper_only"] is True
        assert result["live_order_submission"] is False
        assert replay["idempotent_replay"] is True
        with runtime.read() as connection:
            row = connection.execute(
                "SELECT lane, expression_kind, planned_loss, paper_only FROM app.paper_order WHERE idempotency_key = 'acme-entry-1'"
            ).fetchone()
        assert row["lane"] == "ticker"
        assert row["expression_kind"] == "STOCK"
        assert float(row["planned_loss"]) == 500
        assert row["paper_only"] is True
    finally:
        runtime.close()


def test_ticker_paper_lifecycle_supports_partial_fill_and_invalidation_exit(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('LIFE', 'Lifecycle', 'equity') RETURNING id"
            ).fetchone()
            source_id = "ticker-paper-lifecycle"
            connection.execute(
                "INSERT INTO ingest.source (id, name, family, kind) VALUES (%s, %s, %s, %s)",
                [source_id, "Ticker paper lifecycle", "test", "quote"],
            )
            run_id = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES (%s, 'quotes', %s, %s, 'succeeded')
                RETURNING id
                """,
                [source_id, datetime(2026, 8, 22, 14, tzinfo=UTC), datetime(2026, 8, 22, 14, tzinfo=UTC)],
            ).fetchone()["id"]
            quote_id = connection.execute(
                """
                INSERT INTO raw.quote (
                    instrument_id, source_id, ingest_run_id, observed_at, available_at, price
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [
                    instrument["id"], source_id, run_id,
                    datetime(2026, 8, 22, 14, tzinfo=UTC),
                    datetime(2026, 8, 22, 14, tzinfo=UTC), 99.0,
                ],
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id) VALUES (%s, %s, %s)",
                [quote_id, datetime(2026, 8, 22, 14, tzinfo=UTC), run_id],
            )
        decision = build_ticker_decision(
            "LIFE",
            {
                "quotes": [{"symbol": "LIFE", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
                "portfolio_summary": [{"net_liquidation": 100_000, "available_at": "2026-08-22T13:55:00Z"}],
                "decision_queue": [{
                    "symbol": "LIFE", "stance": "BULLISH", "action": "BUY",
                    "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                    "conviction_tier": "STANDARD",
                    "available_at": "2026-08-22T13:55:00Z",
                }],
            },
            as_of=datetime(2026, 8, 22, 14, tzinfo=UTC),
        )
        config = typed_config(
            migrated_postgres_dsn,
            raw={"analysis": {"options_decision_system": {
                "mode": "paper",
                "ticker_paper_actions_enabled": True,
                "stock_paper_actions_enabled": True,
            }}},
        )
        repository = TickerPaperExecutionRepository(runtime, config)
        staged = repository.stage(
            ticker="LIFE", decision=decision, expression_kind="STOCK",
            idempotency_key="life-entry-1", quantity=4, limit_price=100,
        )
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE app.paper_order SET policy_result = policy_result || %s::jsonb WHERE id = %s::uuid",
                ['{"available_quantity": 2, "fee_per_unit": 0.01}', staged["paper_order_id"]],
            )
        partial = repository.process(now=datetime(2026, 8, 22, 14, 5, tzinfo=UTC))
        assert partial["managed"][0]["status"] == "partial"
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE app.paper_order SET policy_result = policy_result || %s::jsonb WHERE id = %s::uuid",
                ['{"available_quantity": 10}', staged["paper_order_id"]],
            )
        filled = repository.process(now=datetime(2026, 8, 22, 14, 6, tzinfo=UTC))
        assert filled["managed"][0]["event_status"] == "entered"

        with runtime.transaction() as connection:
            run_id = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES (%s, 'quotes', %s, %s, 'succeeded')
                RETURNING id
                """,
                [source_id, datetime(2026, 8, 22, 15, tzinfo=UTC), datetime(2026, 8, 22, 15, tzinfo=UTC)],
            ).fetchone()["id"]
            quote_id = connection.execute(
                """
                INSERT INTO raw.quote (
                    instrument_id, source_id, ingest_run_id, observed_at, available_at, price
                ) SELECT id, %s, %s, %s, %s, %s
                FROM catalog.instrument WHERE symbol = 'LIFE'
                RETURNING id
                """,
                [source_id, run_id, datetime(2026, 8, 22, 15, tzinfo=UTC), datetime(2026, 8, 22, 15, tzinfo=UTC), 89.0],
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id) VALUES (%s, %s, %s)",
                [quote_id, datetime(2026, 8, 22, 15, tzinfo=UTC), run_id],
            )
        exited = repository.process(now=datetime(2026, 8, 22, 15, 5, tzinfo=UTC))
        assert exited["managed"][0]["reason"] == "invalidation"
        with runtime.read() as connection:
            status = connection.execute(
                "SELECT status, filled_quantity, exited_quantity, fees FROM app.paper_order WHERE id = %s::uuid",
                [staged["paper_order_id"]],
            ).fetchone()
        assert status["status"] == "exited"
        assert float(status["filled_quantity"]) == 4
        assert float(status["exited_quantity"]) == 4
        assert float(status["fees"]) > 0
    finally:
        runtime.close()


def test_multi_leg_option_paper_lifecycle_uses_shared_quote_and_risk_ledger(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        source_id = "ticker-paper-options"
        observed = datetime(2026, 8, 22, 14, tzinfo=UTC)
        later_quote = datetime(2026, 8, 22, 14, 10, tzinfo=UTC)
        exit_quote = datetime(2026, 8, 22, 15, tzinfo=UTC)
        with runtime.transaction() as connection:
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('SPRD', 'Spread', 'equity') RETURNING id"
            ).fetchone()
            connection.execute(
                "INSERT INTO ingest.source (id, name, family, kind) VALUES (%s, %s, %s, %s)",
                [source_id, "Ticker option paper", "test", "options"],
            )
            contracts = [
                connection.execute(
                    """
                    INSERT INTO catalog.option_contract
                        (underlying_instrument_id, expiration, strike, option_type, deliverable_key)
                    VALUES (%s, '2026-09-18', %s, 'call', %s)
                    RETURNING id
                    """,
                    [instrument["id"], strike, f"sprd-{strike}"],
                ).fetchone()["id"]
                for strike in (100, 105)
            ]

            def add_option_snapshot(
                at: datetime,
                prices: tuple[tuple[float, float], tuple[float, float]],
                sizes: tuple[int, int],
            ) -> None:
                run_id = connection.execute(
                    """
                    INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                    VALUES (%s, 'options', %s, %s, 'succeeded')
                    RETURNING id
                    """,
                    [source_id, at, at],
                ).fetchone()["id"]
                snapshot_id = connection.execute(
                    """
                    INSERT INTO raw.option_snapshot
                        (source_id, ingest_run_id, observed_at, trading_date, market_session,
                         universe, completeness, contract_count, capture_state)
                    VALUES (%s, %s, %s, %s, 'regular', 'SPRD', 1, 2, 'complete')
                    RETURNING id
                    """,
                    [source_id, run_id, at, at.date()],
                ).fetchone()["id"]
                for contract_id, (bid, ask) in zip(contracts, prices):
                    connection.execute(
                        """
                        INSERT INTO raw.option_quote
                            (observed_at, snapshot_id, contract_id, underlying_price,
                             bid, ask, bid_size, ask_size, mid, volume, open_interest, available_at)
                        VALUES (%s, %s, %s, 100, %s, %s, %s, %s, (%s + %s) / 2, 20, 100, %s)
                        """,
                        [at, snapshot_id, contract_id, bid, ask, sizes[0], sizes[1], bid, ask, at],
                    )

            add_option_snapshot(observed, ((3.0, 3.2), (1.0, 1.1)), (1, 1))
            add_option_snapshot(later_quote, ((3.1, 3.3), (1.0, 1.1)), (2, 2))
            add_option_snapshot(exit_quote, ((2.4, 2.6), (0.8, 1.0)), (2, 2))

        legs = [
            {
                "contract_id": contracts[0], "option_type": "call", "side": "long",
                "strike": 100, "bid": 3.0, "ask": 3.2, "bid_size": 1, "ask_size": 1,
                "quote_time": observed, "expiration": "2026-09-18",
            },
            {
                "contract_id": contracts[1], "option_type": "call", "side": "short",
                "strike": 105, "bid": 1.0, "ask": 1.1, "bid_size": 1, "ask_size": 1,
                "quote_time": observed, "expiration": "2026-09-18",
            },
        ]
        decision = build_ticker_decision(
            "SPRD",
            {
                "quotes": [{"symbol": "SPRD", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
                "portfolio_summary": [{"symbol": "SPRD", "net_liquidation": 100_000, "available_at": "2026-08-22T13:55:00Z"}],
                "decision_queue": [{
                    "symbol": "SPRD", "stance": "BULLISH", "action": "BUY",
                    "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                    "conviction_tier": "STANDARD",
                    "available_at": "2026-08-22T13:55:00Z",
                }],
                "options_payoff_scenarios": [{
                    "symbol": "SPRD", "structure": "debit_spread", "entry_price": 2.2,
                    "max_loss": 220, "lower_confidence_expectancy": 0.8,
                    "liquidity_score": 0.9, "fill_probability": 0.9,
                    "expiration": "2026-09-18", "legs": legs,
                    "available_at": "2026-08-22T13:55:00Z",
                }],
            },
            as_of=observed,
        )
        config = typed_config(
            migrated_postgres_dsn,
            raw={"analysis": {"options_decision_system": {
                "mode": "paper",
                "ticker_paper_actions_enabled": True,
                "stock_paper_actions_enabled": True,
                "options_paper_actions_enabled": True,
            }}},
        )
        repository = TickerPaperExecutionRepository(runtime, config)
        staged = repository.stage(
            ticker="SPRD", decision=decision, expression_kind="DEBIT_SPREAD",
            idempotency_key="sprd-spread-1", quantity=2, limit_price=2.3,
        )
        with runtime.read() as connection:
            assert connection.execute(
                "SELECT count(*) FROM app.paper_order_leg WHERE paper_order_id = %s::uuid",
                [staged["paper_order_id"]],
            ).fetchone()["count"] == 2

        partial = repository.process(now=datetime(2026, 8, 22, 14, 5, tzinfo=UTC))
        assert partial["managed"][0]["status"] == "partial"
        filled = repository.process(now=datetime(2026, 8, 22, 14, 11, tzinfo=UTC))
        assert filled["managed"][0]["event_status"] == "entered"

        with runtime.transaction() as connection:
            run_id = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES (%s, 'quotes', %s, %s, 'succeeded')
                RETURNING id
                """,
                [source_id, exit_quote, exit_quote],
            ).fetchone()["id"]
            quote_id = connection.execute(
                """
                INSERT INTO raw.quote
                    (instrument_id, source_id, ingest_run_id, observed_at, available_at, price)
                VALUES (%s, %s, %s, %s, %s, 89)
                RETURNING id
                """,
                [instrument["id"], source_id, run_id, exit_quote, exit_quote],
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id) VALUES (%s, %s, %s)",
                [quote_id, exit_quote, run_id],
            )

        exited = repository.process(now=datetime(2026, 8, 22, 15, 5, tzinfo=UTC))
        assert exited["managed"][0]["reason"] == "invalidation"
        with runtime.read() as connection:
            row = connection.execute(
                """
                SELECT status, filled_quantity, exited_quantity, fees, exit_price, paper_only
                FROM app.paper_order WHERE id = %s::uuid
                """,
                [staged["paper_order_id"]],
            ).fetchone()
        assert row["status"] == "exited"
        assert float(row["filled_quantity"]) == 2
        assert float(row["exited_quantity"]) == 2
        assert float(row["fees"]) > 0
        assert float(row["exit_price"]) == 1.4
        assert row["paper_only"] is True
    finally:
        runtime.close()


def test_ticker_publisher_persists_immutable_revision_and_pit_manifest(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    from investment_panel.jobs import ticker_decisions

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('PITX', 'Point In Time', 'equity')"
            )
        config = typed_config(migrated_postgres_dsn)
        monkeypatch.setattr(ticker_decisions, "load_config", lambda _path: config)
        observed = datetime(2026, 8, 22, 14, tzinfo=UTC)
        result = ticker_decisions.publish(
            "config.yaml", symbols=["PITX"], as_of=observed,
        )
        replay = ticker_decisions.publish(
            "config.yaml", symbols=["PITX"], as_of=observed,
        )

        assert result["published_count"] == 1, result
        assert replay["published_count"] == 0
        assert replay["skipped_count"] == 1
        decision = TickerDecisionRepository(runtime).latest("PITX")
        assert decision is not None
        assert decision.as_of == observed
        assert len(decision.input_manifest.input_hash) == 64
        with runtime.read() as connection:
            manifest = connection.execute(
                "SELECT count(*) FROM analysis.ticker_input_manifest manifest "
                "JOIN analysis.ticker_decision decision ON decision.id = manifest.ticker_decision_id"
            ).fetchone()["count"]
            outcomes = connection.execute(
                "SELECT count(*) FROM analysis.ticker_outcome outcome "
                "JOIN analysis.ticker_decision decision ON decision.id = outcome.ticker_decision_id"
            ).fetchone()["count"]
            benchmark = connection.execute(
                """
                SELECT benchmark_key, member_count, membership_hash,
                       exact_membership, coverage
                FROM analysis.ticker_benchmark_snapshot
                WHERE benchmark_key = 'market-equity-etf' AND as_of = %s
                """,
                [observed],
            ).fetchone()
        assert manifest >= 1
        assert outcomes == 6
        assert benchmark["benchmark_key"] == "market-equity-etf"
        assert benchmark["member_count"] == len(benchmark["exact_membership"])
        assert "PITX" in benchmark["exact_membership"]
        assert len(benchmark["membership_hash"]) == 64
        assert benchmark["coverage"]["options_availability_affects_breadth"] is False
    finally:
        runtime.close()


def test_ticker_outcome_refresh_persists_costs_and_learning_metadata(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ticker = "OUTC"
        as_of = datetime(2026, 7, 1, 21, tzinfo=UTC)
        available_at = datetime(2026, 7, 1, 20, 30, tzinfo=UTC)
        reference = datetime(2026, 8, 20, 21, tzinfo=UTC)
        ingestion = IngestionRepository(runtime)
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity')",
                [ticker, "Outcome Test"],
            )
        source_id = "ticker-outcome-bars"
        ingestion.register_source(source_id, name="Ticker outcome bars", family="test", kind="daily_bars")
        run_id = ingestion.start_run(source_id, "price_bars", started_at=available_at)
        rows = [{"symbol": ticker, "date": as_of.date().isoformat(), "close": 100}]
        cursor = as_of.date() + timedelta(days=1)
        while len(rows) < 30:
            if cursor.weekday() < 5:
                rows.append({"symbol": ticker, "date": cursor.isoformat(), "close": 100 + len(rows)})
            cursor += timedelta(days=1)
        ingestion.store_price_bars(run_id, source_id, rows, asset_classes={ticker: "equity"})
        ingestion.finish_run(run_id, "succeeded")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [available_at, available_at, run_id],
            )
            connection.execute(
                "UPDATE raw.price_bar SET available_at = %s WHERE ingest_run_id = %s",
                [available_at, run_id],
            )
            connection.execute(
                "UPDATE raw.price_bar_confirmation SET fact_available_at = %s WHERE ingest_run_id = %s",
                [available_at, run_id],
            )
            connection.execute(
                "UPDATE raw.price_bar_fact_availability SET fact_available_at = %s WHERE ingest_run_id = %s",
                [available_at, run_id],
            )

        decision = build_ticker_decision(
            ticker,
            {
                "quotes": [{"symbol": ticker, "price": 100, "available_at": available_at, "confirmed": True}],
                "portfolio_summary": [{"net_liquidation": 100_000, "available_at": available_at}],
                "decision_queue": [{
                    "symbol": ticker, "stance": "BULLISH", "action": "BUY",
                    "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                    "conviction_tier": "STANDARD", "available_at": available_at,
                }],
            },
            as_of=as_of,
        )
        repository = TickerDecisionRepository(runtime)
        published = repository.publish(decision)
        outcome_result = repository.refresh_outcomes(now=reference)

        assert outcome_result["resolved"] == 3
        with runtime.read() as connection:
            row = connection.execute(
                """
                SELECT state, stock_counterfactual_return, metadata
                FROM analysis.ticker_outcome
                WHERE ticker_decision_id = %s::uuid
                  AND horizon = 'TACTICAL' AND horizon_sessions = 20
                """,
                [published["ticker_decision_id"]],
            ).fetchone()
        assert row["state"] == "resolved"
        assert row["stock_counterfactual_return"] is not None
        assert row["metadata"]["cost_adjusted_stock_counterfactual_return"] is not None
        assert row["metadata"]["sample"] in {"historical", "forward", "canary"}
        assert row["metadata"]["purge_embargo_verified"] is True
        assert row["metadata"]["multiple_trial_correction"] == "single-policy-no-trial-selection-v1"
        assert row["metadata"]["expression_marks"]["STOCK"]["evidence_state"] == "ESTIMATED"
    finally:
        runtime.close()
