"""Add immutable Phase 4 allocation, scenario, execution, and attribution artifacts."""

from __future__ import annotations

from alembic import op


revision = "20260902_0071"
down_revision = "20260902_0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE analysis.portfolio_allocation_snapshot (
            allocation_id TEXT PRIMARY KEY,
            as_of TIMESTAMPTZ NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('available', 'cash_only', 'unavailable')),
            cash_hurdle DOUBLE PRECISION NOT NULL CHECK (cash_hurdle < 'Infinity'::double precision AND cash_hurdle > '-Infinity'::double precision AND cash_hurdle >= 0),
            forecast_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            action_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            strategy_registry_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            input_hash CHAR(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CHECK (as_of = input_cutoff),
            CHECK (jsonb_typeof(forecast_ids) = 'array'),
            CHECK (jsonb_typeof(action_ids) = 'array'),
            CHECK (jsonb_typeof(strategy_registry_ids) = 'array')
        );

        CREATE TABLE analysis.portfolio_allocation_item (
            allocation_item_id TEXT PRIMARY KEY,
            allocation_id TEXT NOT NULL REFERENCES analysis.portfolio_allocation_snapshot(allocation_id),
            ticker TEXT NOT NULL,
            strategy_forecast_id TEXT REFERENCES analysis.strategy_forecast(id),
            action_id TEXT,
            hypothesis_id UUID REFERENCES analysis.hypothesis(id),
            disposition TEXT NOT NULL CHECK (disposition IN ('selected', 'ranked_out', 'rejected')),
            target_weight DOUBLE PRECISION NOT NULL CHECK (target_weight < 'Infinity'::double precision AND target_weight > '-Infinity'::double precision AND target_weight >= 0 AND target_weight <= 1),
            marginal_book_utility DOUBLE PRECISION NOT NULL CHECK (marginal_book_utility < 'Infinity'::double precision AND marginal_book_utility > '-Infinity'::double precision),
            trace JSONB NOT NULL,
            blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
            funding_source TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CHECK (jsonb_typeof(trace) = 'object'),
            CHECK (jsonb_typeof(blockers) = 'array'),
            CHECK (disposition <> 'selected' OR (ticker = 'CASH' AND target_weight > 0 AND marginal_book_utility >= 0) OR (target_weight > 0 AND marginal_book_utility > 0)),
            CHECK (ticker <> '')
        );
        CREATE INDEX ix_portfolio_allocation_item_snapshot
            ON analysis.portfolio_allocation_item (allocation_id, disposition, target_weight DESC);

        CREATE TABLE analysis.probabilistic_portfolio_scenario_artifact (
            scenario_artifact_id TEXT PRIMARY KEY,
            allocation_id TEXT NOT NULL REFERENCES analysis.portfolio_allocation_snapshot(allocation_id),
            model_version TEXT NOT NULL,
            probability_semantics TEXT NOT NULL,
            scenarios JSONB NOT NULL,
            tail_dependence JSONB NOT NULL,
            simultaneous_unwind JSONB NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            input_hash CHAR(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CHECK (jsonb_typeof(scenarios) = 'array' AND jsonb_array_length(scenarios) > 0),
            CHECK (jsonb_typeof(tail_dependence) = 'object'),
            CHECK (jsonb_typeof(simultaneous_unwind) = 'object')
        );

        CREATE TABLE analysis.execution_model_snapshot (
            execution_model_snapshot_id TEXT PRIMARY KEY,
            allocation_id TEXT REFERENCES analysis.portfolio_allocation_snapshot(allocation_id),
            model_version TEXT NOT NULL,
            calibration_status TEXT NOT NULL CHECK (calibration_status IN ('calibrated', 'calibration_pending', 'unavailable')),
            sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
            fill_probability DOUBLE PRECISION CHECK (fill_probability IS NULL OR (fill_probability < 'Infinity'::double precision AND fill_probability > '-Infinity'::double precision AND fill_probability BETWEEN 0 AND 1)),
            spread_bps DOUBLE PRECISION CHECK (spread_bps IS NULL OR (spread_bps < 'Infinity'::double precision AND spread_bps > '-Infinity'::double precision AND spread_bps >= 0)),
            latency_ms DOUBLE PRECISION CHECK (latency_ms IS NULL OR (latency_ms < 'Infinity'::double precision AND latency_ms > '-Infinity'::double precision AND latency_ms >= 0)),
            impact_bps DOUBLE PRECISION CHECK (impact_bps IS NULL OR (impact_bps < 'Infinity'::double precision AND impact_bps > '-Infinity'::double precision AND impact_bps >= 0)),
            input_cutoff TIMESTAMPTZ NOT NULL,
            input_hash CHAR(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CHECK (calibration_status <> 'calibrated' OR sample_count > 0)
        );

        CREATE TABLE app.paper_execution_observation (
            paper_execution_observation_id TEXT PRIMARY KEY,
            allocation_item_id TEXT REFERENCES analysis.portfolio_allocation_item(allocation_item_id),
            paper_order_id UUID REFERENCES app.paper_order(id),
            execution_mode TEXT NOT NULL DEFAULT 'paper' CHECK (execution_mode = 'paper'),
            paper_only BOOLEAN NOT NULL DEFAULT true CHECK (paper_only),
            status TEXT NOT NULL CHECK (status IN ('planned', 'submitted', 'partial', 'filled', 'exited', 'cancelled', 'unavailable')),
            requested_quantity DOUBLE PRECISION NOT NULL CHECK (requested_quantity < 'Infinity'::double precision AND requested_quantity > '-Infinity'::double precision AND requested_quantity >= 0),
            filled_quantity DOUBLE PRECISION NOT NULL CHECK (filled_quantity < 'Infinity'::double precision AND filled_quantity > '-Infinity'::double precision AND filled_quantity >= 0 AND filled_quantity <= requested_quantity),
            requested_price DOUBLE PRECISION CHECK (requested_price IS NULL OR (requested_price < 'Infinity'::double precision AND requested_price > '-Infinity'::double precision AND requested_price > 0)),
            fill_price DOUBLE PRECISION CHECK (fill_price IS NULL OR (fill_price < 'Infinity'::double precision AND fill_price > '-Infinity'::double precision AND fill_price > 0)),
            spread_bps DOUBLE PRECISION CHECK (spread_bps IS NULL OR (spread_bps < 'Infinity'::double precision AND spread_bps > '-Infinity'::double precision AND spread_bps >= 0)),
            latency_ms DOUBLE PRECISION CHECK (latency_ms IS NULL OR (latency_ms < 'Infinity'::double precision AND latency_ms > '-Infinity'::double precision AND latency_ms >= 0)),
            impact_bps DOUBLE PRECISION CHECK (impact_bps IS NULL OR (impact_bps < 'Infinity'::double precision AND impact_bps > '-Infinity'::double precision AND impact_bps >= 0)),
            observed_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CHECK (status NOT IN ('filled', 'exited') OR filled_quantity > 0),
            CHECK (fill_price IS NOT NULL OR filled_quantity = 0)
        );

        CREATE TABLE analysis.book_attribution (
            book_attribution_id TEXT PRIMARY KEY,
            allocation_id TEXT NOT NULL REFERENCES analysis.portfolio_allocation_snapshot(allocation_id),
            allocation_item_id TEXT NOT NULL REFERENCES analysis.portfolio_allocation_item(allocation_item_id),
            strategy_forecast_id TEXT REFERENCES analysis.strategy_forecast(id),
            hypothesis_id UUID REFERENCES analysis.hypothesis(id),
            paper_execution_observation_id TEXT REFERENCES app.paper_execution_observation(paper_execution_observation_id),
            pnl_status TEXT NOT NULL CHECK (pnl_status IN ('pending_fill', 'realized', 'unavailable')),
            realized_pnl DOUBLE PRECISION CHECK (realized_pnl IS NULL OR (realized_pnl < 'Infinity'::double precision AND realized_pnl > '-Infinity'::double precision)),
            attribution JSONB NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CHECK (jsonb_typeof(attribution) = 'object'),
            CHECK (pnl_status <> 'realized' OR realized_pnl IS NOT NULL)
        );

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_lineage()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_cutoff TIMESTAMPTZ; expected_allocation TEXT;
                expected_forecast TEXT; expected_hypothesis UUID;
        BEGIN
            IF TG_TABLE_NAME = 'probabilistic_portfolio_scenario_artifact' THEN
                SELECT input_cutoff INTO expected_cutoff
                FROM analysis.portfolio_allocation_snapshot
                WHERE allocation_id = NEW.allocation_id;
                IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN
                    RAISE EXCEPTION 'Phase 4 scenario input cutoff does not match allocation lineage';
                END IF;
            ELSIF TG_TABLE_NAME = 'execution_model_snapshot' AND NEW.allocation_id IS NOT NULL THEN
                SELECT input_cutoff INTO expected_cutoff
                FROM analysis.portfolio_allocation_snapshot
                WHERE allocation_id = NEW.allocation_id;
                IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN
                    RAISE EXCEPTION 'Phase 4 execution input cutoff does not match allocation lineage';
                END IF;
            ELSIF TG_TABLE_NAME = 'book_attribution' THEN
                SELECT allocation_id, strategy_forecast_id, hypothesis_id INTO expected_allocation,
                    expected_forecast, expected_hypothesis
                FROM analysis.portfolio_allocation_item
                WHERE allocation_item_id = NEW.allocation_item_id;
                IF expected_allocation IS NULL OR NEW.allocation_id IS DISTINCT FROM expected_allocation THEN
                    RAISE EXCEPTION 'Phase 4 attribution item does not match allocation lineage';
                END IF;
                IF NEW.strategy_forecast_id IS DISTINCT FROM expected_forecast
                   OR NEW.hypothesis_id IS DISTINCT FROM expected_hypothesis THEN
                    RAISE EXCEPTION 'Phase 4 attribution forecast or hypothesis does not match item lineage';
                END IF;
                SELECT input_cutoff INTO expected_cutoff
                FROM analysis.portfolio_allocation_snapshot
                WHERE allocation_id = NEW.allocation_id;
                IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN
                    RAISE EXCEPTION 'Phase 4 attribution input cutoff does not match allocation lineage';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER portfolio_scenario_lineage
            BEFORE INSERT ON analysis.probabilistic_portfolio_scenario_artifact
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();
        CREATE TRIGGER execution_model_lineage
            BEFORE INSERT ON analysis.execution_model_snapshot
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();
        CREATE TRIGGER book_attribution_lineage
            BEFORE INSERT ON analysis.book_attribution
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();

        CREATE OR REPLACE FUNCTION analysis.reject_phase4_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Phase 4 artifacts are immutable';
        END;
        $$;
        CREATE TRIGGER portfolio_allocation_snapshot_immutable
            BEFORE UPDATE OR DELETE ON analysis.portfolio_allocation_snapshot
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();
        CREATE TRIGGER portfolio_allocation_item_immutable
            BEFORE UPDATE OR DELETE ON analysis.portfolio_allocation_item
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();
        CREATE TRIGGER portfolio_scenario_artifact_immutable
            BEFORE UPDATE OR DELETE ON analysis.probabilistic_portfolio_scenario_artifact
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();
        CREATE TRIGGER execution_model_snapshot_immutable
            BEFORE UPDATE OR DELETE ON analysis.execution_model_snapshot
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();
        CREATE TRIGGER book_attribution_immutable
            BEFORE UPDATE OR DELETE ON analysis.book_attribution
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();
        CREATE TRIGGER paper_execution_observation_immutable
            BEFORE UPDATE OR DELETE ON app.paper_execution_observation
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

        GRANT SELECT, INSERT ON analysis.portfolio_allocation_snapshot,
            analysis.portfolio_allocation_item,
            analysis.probabilistic_portfolio_scenario_artifact,
            analysis.execution_model_snapshot,
            analysis.book_attribution TO market_app;
        GRANT SELECT, INSERT ON app.paper_execution_observation TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT, INSERT ON analysis.portfolio_allocation_snapshot,
            analysis.portfolio_allocation_item,
            analysis.probabilistic_portfolio_scenario_artifact,
            analysis.execution_model_snapshot,
            analysis.book_attribution FROM market_app;
        REVOKE SELECT, INSERT ON app.paper_execution_observation FROM market_app;
        DROP TRIGGER IF EXISTS paper_execution_observation_immutable ON app.paper_execution_observation;
        DROP TRIGGER IF EXISTS book_attribution_lineage ON analysis.book_attribution;
        DROP TRIGGER IF EXISTS execution_model_lineage ON analysis.execution_model_snapshot;
        DROP TRIGGER IF EXISTS portfolio_scenario_lineage ON analysis.probabilistic_portfolio_scenario_artifact;
        DROP TRIGGER IF EXISTS book_attribution_immutable ON analysis.book_attribution;
        DROP TRIGGER IF EXISTS execution_model_snapshot_immutable ON analysis.execution_model_snapshot;
        DROP TRIGGER IF EXISTS portfolio_scenario_artifact_immutable ON analysis.probabilistic_portfolio_scenario_artifact;
        DROP TRIGGER IF EXISTS portfolio_allocation_item_immutable ON analysis.portfolio_allocation_item;
        DROP TRIGGER IF EXISTS portfolio_allocation_snapshot_immutable ON analysis.portfolio_allocation_snapshot;
        DROP FUNCTION IF EXISTS analysis.reject_phase4_update();
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_lineage();
        DROP TABLE IF EXISTS analysis.book_attribution;
        DROP TABLE IF EXISTS app.paper_execution_observation;
        DROP TABLE IF EXISTS analysis.execution_model_snapshot;
        DROP TABLE IF EXISTS analysis.probabilistic_portfolio_scenario_artifact;
        DROP TABLE IF EXISTS analysis.portfolio_allocation_item;
        DROP TABLE IF EXISTS analysis.portfolio_allocation_snapshot;
        """
    )
