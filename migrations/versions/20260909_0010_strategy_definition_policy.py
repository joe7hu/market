"""Persist definition restrictions and remove the runtime identity bypass."""

from alembic import op


revision = "20260909_0010"
down_revision = "20260909_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE analysis.strategy_revision
            ADD COLUMN definition_blockers jsonb NOT NULL DEFAULT '[]'::jsonb,
            ADD CONSTRAINT strategy_revision_definition_blockers_check
                CHECK (jsonb_typeof(definition_blockers) = 'array');

        -- Preserve incomplete historical rows as readable, non-executable
        -- records without leaving a half-populated identity pair.
        SELECT set_config('market.strategy_implementation_backfill', 'on', true);
        UPDATE analysis.strategy_revision
           SET implementation_id = NULL,
               implementation_version = NULL,
               p3_enabled = false
         WHERE (implementation_id IS NULL) <> (implementation_version IS NULL);

        ALTER TABLE analysis.strategy_revision
            ADD CONSTRAINT strategy_revision_implementation_pair_check
                CHECK (
                    (implementation_id IS NULL AND implementation_version IS NULL)
                    OR (implementation_id IS NOT NULL AND implementation_version IS NOT NULL
                        AND btrim(implementation_id) <> '' AND btrim(implementation_version) <> '')
                );

        -- 0009 derived some bindings from mechanism names. Keep rows readable,
        -- but disable only identities that are incomplete, unknown, or use a
        -- version that is no longer an executable implementation.
        UPDATE analysis.strategy_revision
           SET p3_enabled = false
         WHERE implementation_id IS NULL
            OR implementation_version IS NULL
            OR implementation_id NOT IN (
                'daily_trend_underreaction', 'daily_gap_regime',
                'daily_event_propagation', 'options_recovery',
                'crypto_funding_basis', 'volatility_aware_momentum'
            )
            OR (implementation_id = 'daily_trend_underreaction' AND implementation_version <> '2')
            OR (implementation_id = 'daily_gap_regime' AND implementation_version <> '1')
            OR (implementation_id = 'daily_event_propagation' AND implementation_version <> '1')
            OR (implementation_id = 'options_recovery' AND implementation_version <> '2')
            OR (implementation_id = 'crypto_funding_basis' AND implementation_version <> '1')
            OR (implementation_id = 'volatility_aware_momentum' AND implementation_version <> '1');

        ALTER TABLE analysis.strategy_revision
            ADD CONSTRAINT strategy_revision_executable_binding_check
                CHECK (
                    NOT p3_enabled
                    OR (implementation_id IS NOT NULL AND implementation_version IS NOT NULL)
                );

        CREATE OR REPLACE FUNCTION analysis.enforce_strategy_implementation_identity_immutable() RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.implementation_id IS DISTINCT FROM OLD.implementation_id
                   OR NEW.implementation_version IS DISTINCT FROM OLD.implementation_version THEN
                    RAISE EXCEPTION 'strategy implementation identity is immutable';
                END IF;
                RETURN NEW;
            END;
            $$;
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION analysis.enforce_strategy_implementation_identity_immutable() RETURNS trigger
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

        ALTER TABLE analysis.strategy_revision
            DROP CONSTRAINT strategy_revision_definition_blockers_check,
            DROP CONSTRAINT strategy_revision_implementation_pair_check,
            DROP CONSTRAINT strategy_revision_executable_binding_check,
            DROP COLUMN definition_blockers;
        """,
    )
