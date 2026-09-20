"""Supersede Options Radar candidates detached from their incumbent."""

from alembic import op


revision = "20260920_0027"
down_revision = "20260920_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE analysis.strategy_revision candidate
           SET status = 'superseded'
          FROM analysis.strategy_revision parent
         WHERE candidate.supersedes_id = parent.id
           AND candidate.authority_group = 'options-radar-core'
           AND candidate.status IN ('candidate', 'testing', 'approved')
           AND parent.authority_group = 'options-radar-core'
           AND parent.status = 'superseded'
        """,
    )
    op.execute(
        """
        UPDATE app.publication
           SET status = 'superseded', superseded_at = COALESCE(superseded_at, now())
         WHERE scope IN (
             SELECT format('options-paper-experiment:%s', candidate.id)
             FROM analysis.strategy_revision candidate
             JOIN analysis.strategy_revision parent ON parent.id = candidate.supersedes_id
             WHERE candidate.authority_group = 'options-radar-core'
               AND candidate.status = 'superseded'
               AND parent.authority_group = 'options-radar-core'
               AND parent.status = 'superseded'
         )
           AND status = 'published'
        """,
    )
    op.execute(
        """
        DELETE FROM app.current_publication_item
         WHERE scope IN (
             SELECT format('options-paper-experiment:%s', candidate.id)
             FROM analysis.strategy_revision candidate
             JOIN analysis.strategy_revision parent ON parent.id = candidate.supersedes_id
             WHERE candidate.authority_group = 'options-radar-core'
               AND candidate.status = 'superseded'
               AND parent.authority_group = 'options-radar-core'
               AND parent.status = 'superseded'
         )
        """,
    )
    op.execute(
        """
        UPDATE analysis.shadow_trade shadow
           SET status = 'unfilled', pending_entry_reason = 'candidate_authority_changed'
          FROM analysis.decision decision
         WHERE shadow.decision_id = decision.id
           AND shadow.status = 'pending'
           AND shadow.source_kind = 'options_paper_experiment'
           AND decision.strategy_revision_id IN (
               SELECT candidate.id
               FROM analysis.strategy_revision candidate
               JOIN analysis.strategy_revision parent ON parent.id = candidate.supersedes_id
               WHERE candidate.authority_group = 'options-radar-core'
                 AND candidate.status = 'superseded'
                 AND parent.authority_group = 'options-radar-core'
                 AND parent.status = 'superseded'
           )
        """,
    )


def downgrade() -> None:
    # Detached candidates cannot regain a superseded parent as authority.
    pass
