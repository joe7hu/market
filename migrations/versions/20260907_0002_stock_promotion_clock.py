"""Separate stock learning inputs from the later paper promotion decision."""

import re

from alembic import op


revision = "20260907_0002"
down_revision = "20260906_0001"
branch_labels = None
depends_on = None

_FUNCTIONS = (
    "enforce_research_gate_promotion_clock",
    "enforce_research_revision_promotion",
    "enforce_research_revision_promotion_hardened",
)
_DECLARATION = "promotion_decision_cutoff TIMESTAMPTZ; "
_DECISION_CLOCK = """
            IF NEW.status = 'active' THEN
            SELECT (promotion.metrics->>'promotion_cutoff')::timestamptz
              INTO promotion_decision_cutoff
            FROM analysis.strategy_evaluation promotion
            WHERE NEW.strategy_key = 'ticker-stock-alpha'
              AND NEW.parameters->>'model_version' = 'ticker-stock-alpha.v3'
              AND NEW.parameters->>'target_version' = 'stock-counterfactual-20-session-net.v1'
              AND promotion.strategy_revision_id = NEW.id
              AND promotion.evaluation_type = 'paper_advisory_promotion'
              AND promotion.verdict = 'pass'
              AND promotion.artifact_id = NEW.artifact_id
              AND promotion.artifact_hash = NEW.artifact_hash
              AND promotion.input_hash = NEW.parameters->>'input_hash'
              AND promotion.metrics->>'authorization_mode' = 'PAPER'
              AND promotion.metrics->>'artifact_hash' = NEW.artifact_hash
              AND promotion.metrics->>'input_hash' = NEW.parameters->>'input_hash'
              AND promotion.evaluated_at <= clock_timestamp()
              AND promotion.available_at <= clock_timestamp()
            ORDER BY promotion.evaluated_at DESC, promotion.id DESC LIMIT 1;
            IF promotion_decision_cutoff > clock_timestamp() THEN
                RAISE EXCEPTION 'paper promotion cutoff cannot be future-dated';
            END IF;
            END IF;
"""


def upgrade() -> None:
    connection = op.get_bind().connection.driver_connection
    for name in _FUNCTIONS:
        definition = connection.execute(
            "SELECT pg_get_functiondef(%s::regprocedure)", [f"analysis.{name}()"],
        ).fetchone()[0]
        if "promotion_decision_cutoff" in definition:
            raise RuntimeError("stock promotion clock migration requires the original trigger")
        if "DECLARE " in definition:
            definition = definition.replace("DECLARE ", "DECLARE " + _DECLARATION, 1)
        else:
            definition = definition.replace("BEGIN", "DECLARE " + _DECLARATION + "\n        BEGIN", 1)
        definition = definition.replace("BEGIN", "BEGIN\n" + _DECISION_CLOCK, 1)
        # Keep every input/as_of equality and evidence check. Only clocks for
        # publication and completed evaluation use the later PAPER decision.
        definition, count = re.subn(
            r"([<>]=?) (trial\.input_cutoff|trial_cutoff)",
            r"\1 COALESCE(promotion_decision_cutoff, \2)", definition,
        )
        if count < 2:
            raise RuntimeError("stock promotion clock trigger shape changed")
        connection.execute(definition)


def downgrade() -> None:
    connection = op.get_bind().connection.driver_connection
    for name in _FUNCTIONS:
        definition = connection.execute(
            "SELECT pg_get_functiondef(%s::regprocedure)", [f"analysis.{name}()"],
        ).fetchone()[0]
        definition = definition.replace("BEGIN\n" + _DECISION_CLOCK, "BEGIN", 1)
        if name == "enforce_research_revision_promotion_hardened":
            definition = definition.replace(_DECLARATION, "", 1)
        else:
            definition = definition.replace("DECLARE " + _DECLARATION + "\n        ", "", 1)
        definition = re.sub(r"COALESCE\(promotion_decision_cutoff, (trial\.input_cutoff|trial_cutoff)\)", r"\1", definition)
        connection.execute(definition)
