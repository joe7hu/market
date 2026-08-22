"""Add explicit source lifecycle and freshness contracts.

The backfill keeps every source and every payload.  It only classifies the
producer identity used by current health and readiness decisions.
"""

from __future__ import annotations

from alembic import op


revision = "20260821_0047"
down_revision = "20260821_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ingest.source
            ADD COLUMN IF NOT EXISTS operational_state TEXT NOT NULL DEFAULT 'archived',
            ADD COLUMN IF NOT EXISTS health_owner TEXT,
            ADD COLUMN IF NOT EXISTS freshness_seconds INTEGER;
        ALTER TABLE ops.job_run
            ADD COLUMN IF NOT EXISTS scheduled_due_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS dispatched_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS source_status TEXT,
            ADD COLUMN IF NOT EXISTS downstream_status TEXT;
        """
    )
    op.execute(
        """
        UPDATE ingest.source
        SET operational_state = 'archived', health_owner = NULL, freshness_seconds = NULL;

        UPDATE ingest.source
        SET operational_state = 'active', health_owner = 'update_market_data', freshness_seconds = 3600
        WHERE id = 'daily-market-prices';

        UPDATE ingest.source
        SET operational_state = 'active', health_owner = 'options_radar_hard_refresh', freshness_seconds = 259200
        WHERE id = 'robinhood';

        UPDATE ingest.source
        SET operational_state = 'active', health_owner = 'update_social_sources', freshness_seconds = 1800
        WHERE id = 'birdclaw_primary_tweets';

        UPDATE ingest.source
        SET operational_state = 'active', health_owner = 'update_arco_data', freshness_seconds = 14400
        WHERE id = 'arco';

        UPDATE ingest.source
        SET operational_state = 'active', health_owner = 'update_event_calendar', freshness_seconds = 86400
        WHERE id = 'official-event-calendar';

        UPDATE ingest.source
        SET operational_state = 'active', health_owner = 'external:mungermode', freshness_seconds = 86400
        WHERE id = 'mungermode-market-valuations';

        UPDATE ingest.source
        SET operational_state = 'active', health_owner = 'update_research_sources', freshness_seconds = 3600
        WHERE id LIKE 'news_%' OR id LIKE 'blog_%';

        UPDATE ingest.source
        SET operational_state = 'active', health_owner = 'update_disclosures', freshness_seconds = 86400
        WHERE id LIKE 'house_%' OR id LIKE 'sec_13f_%' OR id LIKE 'disclosure_csv_%';

        UPDATE ingest.source
        SET operational_state = 'standby', health_owner = 'update_ibkr_options', freshness_seconds = 3600
        WHERE id = 'ibkr';

        UPDATE ingest.source
        SET operational_state = 'standby', health_owner = 'update_broker_sources', freshness_seconds = 3600,
            enabled = false
        WHERE id = 'moomoo';
        """
    )
    op.execute(
        """
        ALTER TABLE ingest.source
            ADD CONSTRAINT ck_ingest_source_operational_state
            CHECK (operational_state IN ('active', 'standby', 'archived'));
        ALTER TABLE ingest.source
            ADD CONSTRAINT ck_ingest_source_freshness_seconds
            CHECK (freshness_seconds IS NULL OR freshness_seconds > 0);
        ALTER TABLE ingest.source
            ADD CONSTRAINT ck_ingest_source_active_health_contract
            CHECK (
                operational_state <> 'active'
                OR (health_owner IS NOT NULL AND freshness_seconds IS NOT NULL)
            );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_ingest_source_operational_health
            ON ingest.source (operational_state, enabled, health_owner);
        CREATE INDEX IF NOT EXISTS ix_ops_job_run_due
            ON ops.job_run (job_name, scheduled_due_at DESC);
        """
    )
    # The current-price selector is the shared quote authority for Today,
    # Watchlist, portfolio, and readiness reads. Keep historical facts in
    # place, but make the selector ignore disabled and archived identities.
    op.execute(
        """
        DO $$
        DECLARE
            function_definition TEXT;
        BEGIN
            SELECT pg_get_functiondef(
                'raw.current_price_for_instruments(timestamptz,bigint[])'::regprocedure
            ) INTO function_definition;
            function_definition := replace(
                function_definition,
                'JOIN ingest.source source ON source.id = quote.source_id',
                'JOIN ingest.source source ON source.id = quote.source_id '
                'AND source.enabled AND source.operational_state = ''active'''
            );
            function_definition := replace(
                function_definition,
                'JOIN ingest.source source ON source.id = bar.source_id',
                'JOIN ingest.source source ON source.id = bar.source_id '
                'AND source.enabled AND source.operational_state = ''active'''
            );
            EXECUTE function_definition;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ops_job_run_due")
    op.execute("DROP INDEX IF EXISTS ix_ingest_source_operational_health")
    op.execute("ALTER TABLE ingest.source DROP CONSTRAINT IF EXISTS ck_ingest_source_active_health_contract")
    op.execute("ALTER TABLE ingest.source DROP CONSTRAINT IF EXISTS ck_ingest_source_freshness_seconds")
    op.execute("ALTER TABLE ingest.source DROP CONSTRAINT IF EXISTS ck_ingest_source_operational_state")
    op.execute(
        """
        ALTER TABLE ops.job_run
            DROP COLUMN IF EXISTS downstream_status,
            DROP COLUMN IF EXISTS source_status,
            DROP COLUMN IF EXISTS dispatched_at,
            DROP COLUMN IF EXISTS scheduled_due_at;
        ALTER TABLE ingest.source
            DROP COLUMN IF EXISTS freshness_seconds,
            DROP COLUMN IF EXISTS health_owner,
            DROP COLUMN IF EXISTS operational_state;
        """
    )
