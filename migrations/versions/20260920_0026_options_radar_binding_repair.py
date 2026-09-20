"""Replace the legacy Options Radar binding with an immutable v3 successor."""

from alembic import op


revision = "20260920_0026"
down_revision = "20260919_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            legacy record;
            successor record;
        BEGIN
            FOR legacy IN
                SELECT id, parameters
                FROM analysis.strategy_revision
                WHERE authority_group = 'options-radar-core'
                  AND strategy_key = 'options-radar-core'
                  AND status = 'active'
                  AND (revision <> 4
                       OR (implementation_id, implementation_version)
                          IS DISTINCT FROM ('options_radar', 'option-professional-v3-ticket')
                       OR parameters->>'feature_version' IS DISTINCT FROM 'option-professional-v3-ticket'
                       OR jsonb_typeof(parameters->'contract_version') IS DISTINCT FROM 'number'
                       OR parameters->>'contract_version' IS DISTINCT FROM '3')
                FOR UPDATE
            LOOP
                SELECT id, status, parameters, implementation_id, implementation_version
                  INTO successor
                  FROM analysis.strategy_revision
                 WHERE strategy_key = 'options-radar-core' AND revision = 4
                 FOR UPDATE;

                IF successor.id IS NOT NULL
                   AND (successor.implementation_id IS DISTINCT FROM 'options_radar'
                        OR successor.implementation_version IS DISTINCT FROM 'option-professional-v3-ticket'
                        OR successor.parameters->>'feature_version' IS DISTINCT FROM 'option-professional-v3-ticket'
                        OR jsonb_typeof(successor.parameters->'contract_version') IS DISTINCT FROM 'number'
                        OR successor.parameters->>'contract_version' IS DISTINCT FROM '3') THEN
                    RAISE EXCEPTION 'options radar revision 4 has an incompatible immutable binding';
                END IF;
                UPDATE analysis.strategy_revision
                   SET status = 'superseded'
                 WHERE supersedes_id = legacy.id
                   AND status IN ('candidate', 'testing', 'approved');
                UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = legacy.id;
                UPDATE app.publication
                   SET status = 'superseded', superseded_at = COALESCE(superseded_at, now())
                 WHERE (scope IN ('options-radar', format('options-paper-incumbent:%s', legacy.id))
                        OR scope IN (
                            SELECT format('options-paper-experiment:%s', child.id)
                            FROM analysis.strategy_revision child
                            WHERE child.supersedes_id = legacy.id AND child.status = 'superseded'
                        ))
                   AND status = 'published';
                DELETE FROM app.current_publication_item
                 WHERE scope IN ('options-radar', format('options-paper-incumbent:%s', legacy.id))
                    OR scope IN (
                        SELECT format('options-paper-experiment:%s', child.id)
                        FROM analysis.strategy_revision child
                        WHERE child.supersedes_id = legacy.id AND child.status = 'superseded'
                    );
                UPDATE analysis.shadow_trade shadow
                   SET status = 'unfilled', pending_entry_reason = 'candidate_authority_changed'
                  FROM analysis.decision decision
                 WHERE shadow.decision_id = decision.id AND shadow.status = 'pending'
                   AND shadow.source_kind = 'options_paper_experiment'
                   AND (decision.strategy_revision_id = legacy.id
                        OR decision.strategy_revision_id IN (
                            SELECT child.id FROM analysis.strategy_revision child
                            WHERE child.supersedes_id = legacy.id AND child.status = 'superseded'
                        ));

                IF successor.id IS NOT NULL THEN
                    UPDATE analysis.strategy_revision
                       SET status = 'active',
                           promoted_at = COALESCE(promoted_at, now())
                     WHERE id = successor.id
                       AND NOT EXISTS (
                           SELECT 1 FROM analysis.strategy_revision
                           WHERE authority_group = 'options-radar-core' AND status = 'active'
                             AND implementation_id = 'options_radar'
                             AND implementation_version = 'option-professional-v3-ticket'
                       );
                ELSIF NOT EXISTS (
                    SELECT 1 FROM analysis.strategy_revision
                    WHERE authority_group = 'options-radar-core' AND status = 'active'
                      AND implementation_id = 'options_radar'
                      AND implementation_version = 'option-professional-v3-ticket'
                ) THEN
                    INSERT INTO analysis.strategy_revision
                        (strategy_key, revision, name, status, parameters, authority_group,
                         implementation_id, implementation_version, promoted_at)
                    VALUES (
                        'options-radar-core', 4, 'Professional options radar', 'active',
                        jsonb_set(
                            jsonb_set(legacy.parameters, '{feature_version}',
                                      '"option-professional-v3-ticket"'::jsonb, true),
                            '{contract_version}', '3'::jsonb, true
                        ),
                        'options-radar-core', 'options_radar', 'option-professional-v3-ticket', now()
                    );
                END IF;
            END LOOP;
        END $$;
        """,
    )
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")


def downgrade() -> None:
    # The superseded legacy lineage remains historical evidence.
    pass
