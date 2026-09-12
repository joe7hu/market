"""Maintain a compact PostgreSQL-owned projection for paper-book reads."""

from alembic import op


revision = "20260912_0022"
down_revision = "20260912_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE analysis.paper_current_mark_projection (
            instrument_id bigint PRIMARY KEY
                REFERENCES catalog.instrument(id) ON DELETE CASCADE,
            price double precision NOT NULL,
            currency text,
            source_id text NOT NULL,
            observed_at timestamptz NOT NULL,
            available_at timestamptz NOT NULL,
            valuation_status text NOT NULL,
            source_kind text NOT NULL,
            projected_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );

        CREATE INDEX ix_paper_current_mark_projection_source
            ON analysis.paper_current_mark_projection (source_id, projected_at DESC);

        CREATE TABLE analysis.paper_trade_projection (
            paper_order_id uuid PRIMARY KEY
                REFERENCES app.paper_order(id) ON DELETE CASCADE,
            entry_quantity numeric(24,8) NOT NULL DEFAULT 0,
            exit_quantity numeric(24,8) NOT NULL DEFAULT 0,
            entry_units numeric(30,8) NOT NULL DEFAULT 0,
            exit_units numeric(30,8) NOT NULL DEFAULT 0,
            actual_fees numeric(30,8) NOT NULL DEFAULT 0,
            entry_fees numeric(30,8) NOT NULL DEFAULT 0,
            exit_fees numeric(30,8) NOT NULL DEFAULT 0,
            missing_fees bigint NOT NULL DEFAULT 0,
            invalid_fills bigint NOT NULL DEFAULT 0,
            fill_multipliers_verified boolean,
            latest_fill_at timestamptz,
            journal_ids text[] NOT NULL DEFAULT ARRAY[]::text[],
            fill_rows jsonb NOT NULL DEFAULT '[]'::jsonb,
            refreshed_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );

        CREATE INDEX ix_paper_trade_projection_latest_fill
            ON analysis.paper_trade_projection (latest_fill_at DESC, paper_order_id);

        CREATE FUNCTION analysis.refresh_paper_current_marks(p_instrument_ids bigint[])
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis', 'raw', 'catalog', 'ingest'
        AS $$
        DECLARE mark_as_of timestamptz := clock_timestamp();
        BEGIN
            DELETE FROM analysis.paper_current_mark_projection projection
            WHERE projection.instrument_id = ANY(COALESCE(p_instrument_ids, ARRAY[]::bigint[]));

            INSERT INTO analysis.paper_current_mark_projection (
                instrument_id,
                price,
                currency,
                source_id,
                observed_at,
                available_at,
                valuation_status,
                source_kind,
                projected_at
            )
            SELECT priced.instrument_id,
                   priced.price,
                   priced.currency,
                   priced.source_id,
                   priced.observed_at,
                   priced.available_at,
                   priced.valuation_status,
                   priced.source_kind,
                   mark_as_of
            FROM raw.current_price_at(mark_as_of, p_instrument_ids) priced;
        END;
        $$;

        CREATE FUNCTION analysis.refresh_paper_trade_projections(p_order_ids uuid[])
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'app', 'analysis'
        AS $$
        BEGIN
            DELETE FROM analysis.paper_trade_projection projection
            WHERE projection.paper_order_id = ANY(COALESCE(p_order_ids, ARRAY[]::uuid[]));

            INSERT INTO analysis.paper_trade_projection (
                paper_order_id,
                entry_quantity,
                exit_quantity,
                entry_units,
                exit_units,
                actual_fees,
                entry_fees,
                exit_fees,
                missing_fees,
                invalid_fills,
                fill_multipliers_verified,
                latest_fill_at,
                journal_ids,
                fill_rows,
                refreshed_at
            )
            SELECT paper.id,
                   coalesce(sum(journal.quantity) FILTER (WHERE journal.action = 'paper_entry'), 0),
                   coalesce(sum(journal.quantity) FILTER (WHERE journal.action <> 'paper_entry'), 0),
                   coalesce(sum(journal.quantity * journal.price) FILTER (WHERE journal.action = 'paper_entry'), 0),
                   coalesce(sum(journal.quantity * journal.price) FILTER (WHERE journal.action <> 'paper_entry'), 0),
                   coalesce(sum((journal.details->>'fees')::numeric)
                       FILTER (WHERE journal.details->>'fees' ~ '^[0-9]+([.][0-9]+)?$'), 0),
                   coalesce(sum((journal.details->>'fees')::numeric)
                       FILTER (WHERE journal.action = 'paper_entry'
                              AND journal.details->>'fees' ~ '^[0-9]+([.][0-9]+)?$'), 0),
                   coalesce(sum((journal.details->>'fees')::numeric)
                       FILTER (WHERE journal.action <> 'paper_entry'
                              AND journal.details->>'fees' ~ '^[0-9]+([.][0-9]+)?$'), 0),
                   count(*) FILTER (WHERE NOT coalesce(
                       journal.details->>'fees' ~ '^[0-9]+([.][0-9]+)?$', false)),
                   count(*) FILTER (WHERE NOT coalesce(
                       journal.quantity > 0
                       AND journal.quantity < 'Infinity'::numeric
                       AND journal.price >= 0
                       AND journal.price < 'Infinity'::numeric
                       AND (journal.action <> 'paper_entry' OR journal.price > 0), false)),
                   CASE WHEN count(journal.id) = 0 THEN NULL ELSE bool_and(coalesce(
                       paper.contract_multiplier > 0
                       AND paper.contract_multiplier < 'Infinity'::numeric
                       AND CASE WHEN journal.action = 'paper_entry' THEN
                           jsonb_typeof(journal.details->'contract_multiplier') = 'number'
                           AND journal.details->'contract_multiplier' = to_jsonb(paper.contract_multiplier)
                       ELSE
                           jsonb_typeof(journal.details->'entry_contract_multiplier') = 'number'
                           AND jsonb_typeof(journal.details->'exit_contract_multiplier') = 'number'
                           AND journal.details->'entry_contract_multiplier' = to_jsonb(paper.contract_multiplier)
                           AND journal.details->'exit_contract_multiplier' = to_jsonb(paper.contract_multiplier)
                       END, false)) END,
                   max(journal.created_at),
                   coalesce(array_agg(journal.id::text ORDER BY journal.created_at, journal.id)
                       FILTER (WHERE journal.id IS NOT NULL), ARRAY[]::text[]),
                   coalesce(jsonb_agg(jsonb_build_object(
                       'id', journal.id::text,
                       'action', journal.action,
                       'quantity', journal.quantity,
                       'price', journal.price,
                       'created_at', journal.created_at,
                       'fees', journal.details->'fees',
                       'contract_multiplier', journal.details->'contract_multiplier',
                       'entry_contract_multiplier', journal.details->'entry_contract_multiplier',
                       'exit_contract_multiplier', journal.details->'exit_contract_multiplier'
                   ) ORDER BY journal.created_at, journal.id)
                   FILTER (WHERE journal.id IS NOT NULL), '[]'::jsonb),
                   clock_timestamp()
            FROM app.paper_order paper
            LEFT JOIN app.trade_journal journal
              ON journal.details->>'paper_order_id' = paper.id::text
             AND journal.decision_id IS NOT DISTINCT FROM paper.decision_id
             AND journal.instrument_id = paper.instrument_id
             AND journal.rationale = 'deterministic_options_paper_execution'
             AND (journal.action = 'paper_entry'
                  OR journal.action = 'paper_exit'
                  OR journal.action LIKE 'paper_exit:%%')
            WHERE paper.id = ANY(COALESCE(p_order_ids, ARRAY[]::uuid[]))
            GROUP BY paper.id;
        END;
        $$;

        CREATE FUNCTION analysis.refresh_paper_order_projection_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'app', 'analysis'
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                DELETE FROM analysis.paper_trade_projection projection
                WHERE projection.paper_order_id IN (SELECT old_rows.id FROM old_rows);
            ELSE
                PERFORM analysis.refresh_paper_trade_projections(
                    ARRAY(SELECT new_rows.id FROM new_rows)
                );
            END IF;
            RETURN NULL;
        END;
        $$;

        CREATE FUNCTION analysis.refresh_paper_quote_mark_projection_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis', 'raw', 'catalog', 'ingest'
        AS $$
        BEGIN
            PERFORM analysis.refresh_paper_current_marks(
                ARRAY(
                    SELECT DISTINCT instrument_id
                    FROM (
                        SELECT quote.instrument_id
                        FROM raw.quote quote
                        JOIN new_rows changed
                          ON changed.fact_id = quote.id
                         AND changed.fact_available_at = quote.available_at
                        UNION ALL
                        SELECT quote.instrument_id
                        FROM raw.quote_history quote
                        JOIN new_rows changed
                          ON changed.fact_id = quote.id
                         AND changed.fact_available_at = quote.available_at
                    ) affected
                )
            );
            RETURN NULL;
        END;
        $$;

        CREATE FUNCTION analysis.refresh_paper_quote_mark_delete_projection_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis', 'raw', 'catalog', 'ingest'
        AS $$
        BEGIN
            PERFORM analysis.refresh_paper_current_marks(
                ARRAY(
                    SELECT DISTINCT instrument_id
                    FROM (
                        SELECT quote.instrument_id
                        FROM raw.quote quote
                        JOIN old_rows changed
                          ON changed.fact_id = quote.id
                         AND changed.fact_available_at = quote.available_at
                        UNION ALL
                        SELECT quote.instrument_id
                        FROM raw.quote_history quote
                        JOIN old_rows changed
                          ON changed.fact_id = quote.id
                         AND changed.fact_available_at = quote.available_at
                    ) affected
                )
            );
            RETURN NULL;
        END;
        $$;

        CREATE FUNCTION analysis.refresh_paper_bar_mark_projection_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis', 'raw', 'catalog', 'ingest'
        AS $$
        BEGIN
            PERFORM analysis.refresh_paper_current_marks(
                ARRAY(
                    SELECT DISTINCT instrument_id
                    FROM (
                        SELECT bar.instrument_id
                        FROM raw.price_bar bar
                        JOIN new_rows changed
                          ON changed.fact_id = bar.id
                         AND changed.fact_available_at = bar.available_at
                        UNION ALL
                        SELECT bar.instrument_id
                        FROM raw.price_bar_history bar
                        JOIN new_rows changed
                          ON changed.fact_id = bar.id
                         AND changed.fact_available_at = bar.available_at
                    ) affected
                )
            );
            RETURN NULL;
        END;
        $$;

        CREATE FUNCTION analysis.refresh_paper_bar_mark_delete_projection_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis', 'raw', 'catalog', 'ingest'
        AS $$
        BEGIN
            PERFORM analysis.refresh_paper_current_marks(
                ARRAY(
                    SELECT DISTINCT instrument_id
                    FROM (
                        SELECT bar.instrument_id
                        FROM raw.price_bar bar
                        JOIN old_rows changed
                          ON changed.fact_id = bar.id
                         AND changed.fact_available_at = bar.available_at
                        UNION ALL
                        SELECT bar.instrument_id
                        FROM raw.price_bar_history bar
                        JOIN old_rows changed
                          ON changed.fact_id = bar.id
                         AND changed.fact_available_at = bar.available_at
                    ) affected
                )
            );
            RETURN NULL;
        END;
        $$;

        CREATE FUNCTION analysis.refresh_paper_journal_insert_projection_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'app', 'analysis'
        AS $$
        BEGIN
            PERFORM analysis.refresh_paper_trade_projections(
                ARRAY(
                    SELECT DISTINCT CASE
                        WHEN NULLIF(row_data.details->>'paper_order_id', '') ~*
                            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        THEN NULLIF(row_data.details->>'paper_order_id', '')::uuid
                    END
                    FROM new_rows row_data
                    WHERE NULLIF(row_data.details->>'paper_order_id', '') IS NOT NULL
                )
            );
            RETURN NULL;
        END;
        $$;

        CREATE FUNCTION analysis.refresh_paper_journal_update_projection_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'app', 'analysis'
        AS $$
        BEGIN
            PERFORM analysis.refresh_paper_trade_projections(
                ARRAY(
                    SELECT DISTINCT CASE
                        WHEN NULLIF(row_data.paper_order_id, '') ~*
                            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        THEN NULLIF(row_data.paper_order_id, '')::uuid
                    END
                    FROM (
                        SELECT details->>'paper_order_id' AS paper_order_id FROM new_rows
                        UNION ALL
                        SELECT details->>'paper_order_id' AS paper_order_id FROM old_rows
                    ) row_data
                    WHERE NULLIF(row_data.paper_order_id, '') IS NOT NULL
                )
            );
            RETURN NULL;
        END;
        $$;

        CREATE FUNCTION analysis.refresh_paper_journal_delete_projection_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'app', 'analysis'
        AS $$
        BEGIN
            PERFORM analysis.refresh_paper_trade_projections(
                ARRAY(
                    SELECT DISTINCT CASE
                        WHEN NULLIF(row_data.details->>'paper_order_id', '') ~*
                            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        THEN NULLIF(row_data.details->>'paper_order_id', '')::uuid
                    END
                    FROM old_rows row_data
                    WHERE NULLIF(row_data.details->>'paper_order_id', '') IS NOT NULL
                )
            );
            RETURN NULL;
        END;
        $$;

        CREATE TRIGGER paper_trade_projection_insert
            AFTER INSERT ON app.paper_order
            REFERENCING NEW TABLE AS new_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION analysis.refresh_paper_order_projection_trigger();

        CREATE TRIGGER paper_trade_projection_update
            AFTER UPDATE ON app.paper_order
            REFERENCING NEW TABLE AS new_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION analysis.refresh_paper_order_projection_trigger();

        CREATE TRIGGER paper_trade_projection_delete
            AFTER DELETE ON app.paper_order
            REFERENCING OLD TABLE AS old_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION analysis.refresh_paper_order_projection_trigger();

        CREATE TRIGGER paper_trade_projection_journal_insert
            AFTER INSERT ON app.trade_journal
            REFERENCING NEW TABLE AS new_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION analysis.refresh_paper_journal_insert_projection_trigger();

        CREATE TRIGGER paper_trade_projection_journal_update
            AFTER UPDATE ON app.trade_journal
            REFERENCING NEW TABLE AS new_rows OLD TABLE AS old_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION analysis.refresh_paper_journal_update_projection_trigger();

        CREATE TRIGGER paper_trade_projection_journal_delete
            AFTER DELETE ON app.trade_journal
            REFERENCING OLD TABLE AS old_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION analysis.refresh_paper_journal_delete_projection_trigger();

        CREATE TRIGGER paper_mark_projection_quote_insert
            AFTER INSERT ON raw.quote_confirmation
            REFERENCING NEW TABLE AS new_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION analysis.refresh_paper_quote_mark_projection_trigger();

        CREATE TRIGGER paper_mark_projection_quote_update
            AFTER UPDATE ON raw.quote_confirmation
            REFERENCING NEW TABLE AS new_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION analysis.refresh_paper_quote_mark_projection_trigger();

        CREATE TRIGGER paper_mark_projection_quote_delete
            AFTER DELETE ON raw.quote_confirmation
            REFERENCING OLD TABLE AS old_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION analysis.refresh_paper_quote_mark_delete_projection_trigger();

        CREATE TRIGGER paper_mark_projection_bar_insert
            AFTER INSERT ON raw.price_bar_confirmation
            REFERENCING NEW TABLE AS new_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION analysis.refresh_paper_bar_mark_projection_trigger();

        CREATE TRIGGER paper_mark_projection_bar_update
            AFTER UPDATE ON raw.price_bar_confirmation
            REFERENCING NEW TABLE AS new_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION analysis.refresh_paper_bar_mark_projection_trigger();

        CREATE TRIGGER paper_mark_projection_bar_delete
            AFTER DELETE ON raw.price_bar_confirmation
            REFERENCING OLD TABLE AS old_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION analysis.refresh_paper_bar_mark_delete_projection_trigger();

        SELECT analysis.refresh_paper_trade_projections(
            ARRAY(SELECT id FROM app.paper_order)
        );

        SELECT analysis.refresh_paper_current_marks(
            ARRAY(SELECT id FROM catalog.instrument)
        );

        GRANT SELECT ON TABLE analysis.paper_current_mark_projection TO market_app;
        GRANT SELECT ON TABLE analysis.paper_trade_projection TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.refresh_paper_current_marks(bigint[]) TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.refresh_paper_trade_projections(uuid[]) TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.refresh_paper_order_projection_trigger() TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.refresh_paper_journal_insert_projection_trigger() TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.refresh_paper_journal_update_projection_trigger() TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.refresh_paper_journal_delete_projection_trigger() TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER paper_trade_projection_journal_delete ON app.trade_journal;
        DROP TRIGGER paper_trade_projection_journal_update ON app.trade_journal;
        DROP TRIGGER paper_trade_projection_journal_insert ON app.trade_journal;
        DROP TRIGGER paper_trade_projection_delete ON app.paper_order;
        DROP TRIGGER paper_trade_projection_update ON app.paper_order;
        DROP TRIGGER paper_trade_projection_insert ON app.paper_order;
        DROP TRIGGER paper_mark_projection_bar_delete ON raw.price_bar_confirmation;
        DROP TRIGGER paper_mark_projection_bar_update ON raw.price_bar_confirmation;
        DROP TRIGGER paper_mark_projection_bar_insert ON raw.price_bar_confirmation;
        DROP TRIGGER paper_mark_projection_quote_delete ON raw.quote_confirmation;
        DROP TRIGGER paper_mark_projection_quote_update ON raw.quote_confirmation;
        DROP TRIGGER paper_mark_projection_quote_insert ON raw.quote_confirmation;
        DROP FUNCTION analysis.refresh_paper_journal_delete_projection_trigger();
        DROP FUNCTION analysis.refresh_paper_journal_update_projection_trigger();
        DROP FUNCTION analysis.refresh_paper_journal_insert_projection_trigger();
        DROP FUNCTION analysis.refresh_paper_order_projection_trigger();
        DROP FUNCTION analysis.refresh_paper_bar_mark_delete_projection_trigger();
        DROP FUNCTION analysis.refresh_paper_bar_mark_projection_trigger();
        DROP FUNCTION analysis.refresh_paper_quote_mark_delete_projection_trigger();
        DROP FUNCTION analysis.refresh_paper_quote_mark_projection_trigger();
        DROP FUNCTION analysis.refresh_paper_current_marks(bigint[]);
        DROP FUNCTION analysis.refresh_paper_trade_projections(uuid[]);
        DROP INDEX analysis.ix_paper_trade_projection_latest_fill;
        DROP INDEX analysis.ix_paper_current_mark_projection_source;
        DROP TABLE analysis.paper_trade_projection;
        DROP TABLE analysis.paper_current_mark_projection;
        """
    )
