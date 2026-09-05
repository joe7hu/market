"""Accept reconciled manual account lineage in Phase 4 guards."""

from __future__ import annotations

from alembic import op


revision = "20260905_0093"
down_revision = "20260905_0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION analysis.phase4_account_authority_exists(
          authority_id TEXT, cutoff TIMESTAMPTZ
        ) RETURNS BOOLEAN LANGUAGE plpgsql STABLE AS $$
        BEGIN
          IF authority_id ~ '^broker-account:[0-9]+$' THEN
            RETURN EXISTS (
              SELECT 1 FROM raw.broker_account_snapshot account
               WHERE ('broker-account:' || account.id::TEXT) = authority_id
                 AND account.observed_at <= cutoff
            );
          ELSIF authority_id ~ '^manual-account:[0-9]+$' THEN
            RETURN EXISTS (
              SELECT 1 FROM app.manual_account_snapshot account
               WHERE ('manual-account:' || account.id::TEXT) = authority_id
                 AND account.reconciliation_state = 'reconciled'
                 AND account.effective_at <= cutoff
            );
          END IF;
          RETURN FALSE;
        END;
        $$;

        CREATE OR REPLACE FUNCTION analysis.phase4_funding_source_capacity(
          source_key TEXT, authority_id TEXT, cutoff TIMESTAMPTZ
        ) RETURNS DOUBLE PRECISION LANGUAGE plpgsql STABLE AS $$
        DECLARE capacity DOUBLE PRECISION;
        BEGIN
          IF NOT analysis.phase4_account_authority_exists(authority_id, cutoff) THEN
            RETURN NULL;
          END IF;
          IF source_key = 'CASH:' || authority_id THEN
            IF authority_id ~ '^broker-account:[0-9]+$' THEN
              SELECT account.cash_balance::DOUBLE PRECISION INTO capacity
                FROM raw.broker_account_snapshot account
               WHERE ('broker-account:' || account.id::TEXT) = authority_id
                 AND account.observed_at <= cutoff;
            ELSE
              SELECT account.cash_balance::DOUBLE PRECISION INTO capacity
                FROM app.manual_account_snapshot account
               WHERE ('manual-account:' || account.id::TEXT) = authority_id
                 AND account.reconciliation_state = 'reconciled'
                 AND account.effective_at <= cutoff;
            END IF;
            RETURN capacity;
          END IF;
          IF source_key ~ '^TRIM:broker-position:[0-9]+$' THEN
            SELECT abs(position.market_value)::DOUBLE PRECISION INTO capacity
              FROM raw.broker_position_snapshot position
              JOIN raw.broker_account_snapshot account
                ON account.id = position.account_snapshot_id
             WHERE position.id = split_part(source_key, ':', 3)::BIGINT
               AND ('broker-account:' || account.id::TEXT) = authority_id
               AND account.observed_at <= cutoff
               AND position.quantity > 0 AND position.market_value IS NOT NULL;
            RETURN capacity;
          END IF;
          IF source_key ~ '^TRIM:manual-position:[0-9]+$' THEN
            SELECT position.quantity::DOUBLE PRECISION * quote.price::DOUBLE PRECISION INTO capacity
              FROM app.portfolio_position position
              LEFT JOIN LATERAL (
                SELECT price FROM raw.current_price_at(cutoff, ARRAY[position.instrument_id]::BIGINT[])
                LIMIT 1
              ) quote ON TRUE
             WHERE position.instrument_id = split_part(source_key, ':', 3)::BIGINT
               AND position.quantity > 0;
            RETURN capacity;
          END IF;
          RETURN NULL;
        END;
        $$;

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_funding_conservation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          allocation_cutoff TIMESTAMPTZ;
          authority_id TEXT;
          account_nav DOUBLE PRECISION;
          source_key TEXT;
          claimed DOUBLE PRECISION;
          available DOUBLE PRECISION;
          source_capacity DOUBLE PRECISION;
        BEGIN
          SELECT snapshot.input_cutoff, snapshot.metadata->>'authority_snapshot_id'
            INTO allocation_cutoff, authority_id
            FROM analysis.portfolio_allocation_snapshot snapshot
           WHERE snapshot.allocation_id = NEW.allocation_id;
          IF allocation_cutoff IS NULL OR NOT analysis.phase4_account_authority_exists(authority_id, allocation_cutoff) THEN
            RAISE EXCEPTION 'Phase 4 funding conservation has no reconciled account authority';
          END IF;
          IF authority_id ~ '^broker-account:[0-9]+$' THEN
            SELECT net_liquidation::DOUBLE PRECISION INTO account_nav
              FROM raw.broker_account_snapshot
             WHERE ('broker-account:' || id::TEXT) = authority_id AND observed_at <= allocation_cutoff;
          ELSE
            SELECT net_liquidation::DOUBLE PRECISION INTO account_nav
              FROM app.manual_account_snapshot
             WHERE ('manual-account:' || id::TEXT) = authority_id
               AND reconciliation_state = 'reconciled' AND effective_at <= allocation_cutoff;
          END IF;
          IF account_nav IS NULL OR account_nav <= 0 THEN
            RAISE EXCEPTION 'Phase 4 funding conservation has incomplete account evidence';
          END IF;
          FOR source_key, claimed IN
            SELECT source.key, sum((source.value #>> '{}')::DOUBLE PRECISION)
              FROM analysis.portfolio_allocation_item item
              CROSS JOIN LATERAL jsonb_each(item.funding_sources) source
             WHERE item.allocation_id = NEW.allocation_id
               AND item.disposition = 'selected' AND item.ticker <> 'CASH'
               AND item.target_weight > item.current_weight
             GROUP BY source.key
          LOOP
            source_capacity := analysis.phase4_funding_source_capacity(source_key, authority_id, allocation_cutoff);
            IF source_capacity IS NULL OR claimed > source_capacity + 0.000000001 THEN
              RAISE EXCEPTION 'Phase 4 funding source is unavailable or over-allocated: %', source_key;
            END IF;
            IF source_key LIKE 'TRIM:%' THEN
              SELECT coalesce(sum(
                       least(greatest(item.current_weight - item.target_weight, 0) * account_nav,
                             source_capacity)
                     ), 0)
                INTO available
                FROM analysis.portfolio_allocation_item item
               WHERE item.allocation_id = NEW.allocation_id
                 AND item.disposition IN ('selected', 'rollback')
                 AND item.target_weight < item.current_weight
                 AND item.trace->>'trim_position_id' = replace(source_key, 'TRIM:', '');
              IF claimed > least(source_capacity, available) + 0.000000001 THEN
                RAISE EXCEPTION 'Phase 4 trim funding exceeds released source %', source_key;
              END IF;
            END IF;
          END LOOP;
          RETURN NULL;
        END;
        $$;

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_review_snapshot_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF coalesce(NEW.metadata->>'authority', '') <> 'postgresql'
             OR NEW.metadata->>'authority_snapshot_id' !~ '^(broker-account|manual-account):[0-9]+$'
             OR jsonb_typeof(NEW.metadata->'source_hashes') IS DISTINCT FROM 'array'
             OR (NEW.status <> 'cash_only' AND jsonb_array_length(NEW.metadata->'source_hashes') = 0)
             OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'source_hashes') hash
                        WHERE hash !~ '^[0-9a-f]{64}$' OR hash = repeat('0', 64))
             OR NOT analysis.phase4_account_authority_exists(
                  NEW.metadata->>'authority_snapshot_id', NEW.input_cutoff)
          THEN
            RAISE EXCEPTION 'Phase 4 allocation requires PostgreSQL authority evidence';
          END IF;
          NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
            'allocation_id', NEW.allocation_id, 'as_of', analysis.phase4_canonical_timestamp(NEW.as_of),
            'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'status', NEW.status,
            'cash_hurdle', NEW.cash_hurdle, 'forecast_ids', NEW.forecast_ids, 'action_ids', NEW.action_ids,
            'strategy_registry_ids', NEW.strategy_registry_ids, 'metadata', NEW.metadata));
          RETURN NEW;
        END;
        $$;
        """
    )
    _replace_guard("enforce_phase4_authority_lineage", """
                   OR NOT EXISTS (
                       SELECT 1 FROM raw.broker_account_snapshot account
                        WHERE ('broker-account:' || account.id::text) = allocation_meta->>'authority_snapshot_id'
                          AND account.observed_at <= allocation_cutoff
                   )""", """
                   OR NOT analysis.phase4_account_authority_exists(
                       allocation_meta->>'authority_snapshot_id', allocation_cutoff
                   )""")
    _replace_guard("enforce_phase4_allocation_item_funding_lineage", """
               OR NOT EXISTS (
                 SELECT 1 FROM raw.broker_account_snapshot account
                  WHERE ('broker-account:' || account.id::TEXT) = allocation_meta->>'authority_snapshot_id'
                    AND account.observed_at <= allocation_cutoff
               )""", """
               OR NOT analysis.phase4_account_authority_exists(
                 allocation_meta->>'authority_snapshot_id', allocation_cutoff
               )""")
    _replace_guard("enforce_phase4_review_item_guard", """
            IF source_key !~ '^(CASH:broker-account:[0-9]+|TRIM:broker-position:[0-9]+)$'""", """
            IF source_key !~ '^(CASH:(broker-account|manual-account):[0-9]+|TRIM:(broker-position|manual-position):[0-9]+)$'""")
    _replace_guard("enforce_phase4_review_item_guard", """
            IF source_key LIKE 'CASH:%' THEN
              IF NOT EXISTS (
                SELECT 1
                  FROM raw.broker_account_snapshot account
                 WHERE account.id = split_part(source_key, ':', 3)::BIGINT
                   AND account.observed_at <= expected_cutoff
                   AND ('broker-account:' || account.id::TEXT) =
                       (SELECT metadata->>'authority_snapshot_id'
                          FROM analysis.portfolio_allocation_snapshot
                         WHERE allocation_id = NEW.allocation_id)
              ) THEN
                RAISE EXCEPTION 'Phase 4 cash funding source is not the allocation authority account: %', source_key;
              END IF;
            ELSIF NOT EXISTS (
              SELECT 1
                FROM raw.broker_position_snapshot position
                JOIN raw.broker_account_snapshot account
                  ON account.id = position.account_snapshot_id
               WHERE position.id = split_part(source_key, ':', 3)::BIGINT
                 AND position.quantity > 0
                 AND position.market_value IS NOT NULL
                 AND account.observed_at <= expected_cutoff
                 AND ('broker-account:' || account.id::TEXT) =
                     (SELECT metadata->>'authority_snapshot_id'
                        FROM analysis.portfolio_allocation_snapshot
                       WHERE allocation_id = NEW.allocation_id)
            ) THEN
              RAISE EXCEPTION 'Phase 4 trim funding source is not a persisted position: %', source_key;
            END IF;""", """
            IF analysis.phase4_funding_source_capacity(
                 source_key,
                 (SELECT metadata->>'authority_snapshot_id'
                    FROM analysis.portfolio_allocation_snapshot
                   WHERE allocation_id = NEW.allocation_id),
                 expected_cutoff
               ) IS NULL
               OR analysis.phase4_funding_source_capacity(
                 source_key,
                 (SELECT metadata->>'authority_snapshot_id'
                    FROM analysis.portfolio_allocation_snapshot
                   WHERE allocation_id = NEW.allocation_id),
                 expected_cutoff
               ) < source_amount THEN
              RAISE EXCEPTION 'Phase 4 funding source is unavailable or insufficient: %', source_key;
            END IF;""")
    _replace_guard("enforce_phase4_lineage", """
                    ELSIF NEW.funding_source LIKE 'CASH:broker-account:%' AND NOT EXISTS (
                        SELECT 1 FROM raw.broker_account_snapshot account
                        WHERE account.id = split_part(NEW.funding_source, ':', 4)::BIGINT
                          AND account.cash_balance >= NEW.funding_amount
                          AND account.observed_at <= (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
                    ) THEN""", """
                    ELSIF NEW.funding_source LIKE ANY (ARRAY['CASH:broker-account:%', 'CASH:manual-account:%'])
                      AND (analysis.phase4_funding_source_capacity(
                             NEW.funding_source,
                             (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id),
                             (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
                           ) IS NULL
                           OR analysis.phase4_funding_source_capacity(
                             NEW.funding_source,
                             (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id),
                             (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
                           ) < NEW.funding_amount) THEN""")
    _replace_guard("enforce_phase4_lineage", """
                    ELSIF NEW.funding_source LIKE 'TRIM:broker-position:%' AND NOT EXISTS (
                        SELECT 1 FROM raw.broker_position_snapshot position
                        WHERE position.id = split_part(NEW.funding_source, ':', 3)::BIGINT
                          AND position.quantity > 0
                          AND abs(coalesce(position.market_value, 0)) >= NEW.funding_amount
                    ) THEN""", """
                    ELSIF NEW.funding_source LIKE ANY (ARRAY['TRIM:broker-position:%', 'TRIM:manual-position:%'])
                      AND (analysis.phase4_funding_source_capacity(
                             NEW.funding_source,
                             (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id),
                             (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
                           ) IS NULL
                           OR analysis.phase4_funding_source_capacity(
                             NEW.funding_source,
                             (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id),
                             (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
                           ) < NEW.funding_amount) THEN""")
    _replace_guard("enforce_phase4_lineage", """
                    ELSIF NEW.funding_source NOT LIKE 'CASH:broker-account:%'
                          AND NEW.funding_source NOT LIKE 'TRIM:broker-position:%' THEN""", """
                    ELSIF NOT (NEW.funding_source LIKE ANY (ARRAY[
                          'CASH:broker-account:%', 'CASH:manual-account:%',
                          'TRIM:broker-position:%', 'TRIM:manual-position:%'])) THEN""")


def _replace_guard(function_name: str, old: str, new: str) -> None:
    sql_old = old.replace("'", "''")
    sql_new = new.replace("'", "''")
    sql_function = function_name.replace("'", "''")
    op.execute(
        f"""
        DO $do$
        DECLARE body TEXT; original TEXT;
        BEGIN
          SELECT pg_get_functiondef(oid) INTO body
            FROM pg_proc
           WHERE pronamespace = 'analysis'::regnamespace
             AND proname = '{sql_function}' AND prokind = 'f';
          IF body IS NULL THEN
            RAISE EXCEPTION 'Phase 4 guard function is missing: {sql_function}';
          END IF;
          original := body;
          body := replace(body, '{sql_old}', '{sql_new}');
          IF body = original THEN
            RAISE EXCEPTION 'Phase 4 guard replacement did not match: {sql_function}';
          END IF;
          EXECUTE body;
        END
        $do$;
        """,
    )


def downgrade() -> None:
    # The helper functions and guard predicates are backward-compatible with
    # 0092. Keep them on rollback so the database cannot lose a safety check.
    pass
