"""Persist the exact executable implementation for each strategy revision."""

from alembic import op


revision = "20260909_0009"
down_revision = "20260909_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE analysis.strategy_revision
            ADD COLUMN implementation_id text,
            ADD COLUMN implementation_version text;

        ALTER TABLE analysis.strategy_revision
            ADD CONSTRAINT strategy_revision_implementation_id_check
                CHECK (implementation_id IS NULL OR btrim(implementation_id) <> ''),
            ADD CONSTRAINT strategy_revision_implementation_version_check
                CHECK (implementation_version IS NULL OR btrim(implementation_version) <> '');

        CREATE FUNCTION analysis.enforce_strategy_implementation_identity_immutable() RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF current_setting('market.strategy_implementation_backfill', true) IS DISTINCT FROM 'on'
                   AND (NEW.implementation_id IS DISTINCT FROM OLD.implementation_id
                        OR NEW.implementation_version IS DISTINCT FROM OLD.implementation_version) THEN
                    RAISE EXCEPTION 'strategy implementation identity is immutable';
                END IF;
                RETURN NEW;
            END;
            $$;

        CREATE TRIGGER enforce_strategy_implementation_identity_immutable
            BEFORE UPDATE OF implementation_id, implementation_version
            ON analysis.strategy_revision
            FOR EACH ROW
            EXECUTE FUNCTION analysis.enforce_strategy_implementation_identity_immutable();

        SELECT set_config('market.strategy_implementation_backfill', 'on', true);
        UPDATE analysis.strategy_revision
           SET implementation_id = CASE mechanism_class
               WHEN 'trend_underreaction' THEN 'daily_trend_underreaction'
               WHEN 'gap_regime' THEN 'daily_gap_regime'
               WHEN 'event_propagation' THEN 'daily_event_propagation'
               WHEN 'options_recovery' THEN 'options_recovery'
               WHEN 'crypto_basis' THEN 'crypto_funding_basis'
               ELSE coalesce(nullif(mechanism_class, ''), 'unavailable')
           END,
               implementation_version = CASE mechanism_class
                   WHEN 'options_recovery' THEN '2'
                   ELSE '1'
               END
         WHERE implementation_id IS NULL;
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER enforce_strategy_implementation_identity_immutable
            ON analysis.strategy_revision;
        DROP FUNCTION analysis.enforce_strategy_implementation_identity_immutable();
        ALTER TABLE analysis.strategy_revision
            DROP CONSTRAINT strategy_revision_implementation_id_check,
            DROP CONSTRAINT strategy_revision_implementation_version_check,
            DROP COLUMN implementation_id,
            DROP COLUMN implementation_version;
        """,
    )
