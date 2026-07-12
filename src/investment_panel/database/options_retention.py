"""Point-in-time reject retention for option-radar counterfactual learning."""

from __future__ import annotations

from typing import Any


def retain_reject_sample(connection: Any, run_id: Any) -> None:
    """Keep every one-blocker near miss and a stable 5% sample of other rejects."""
    sampled = "cardinality(decision.blockers) = 1 OR " \
        "mod(('x' || substr(md5(decision.decision_key), 1, 8))::bit(32)::bigint, 20) = 0"
    connection.execute(
        f"""
        INSERT INTO analysis.reject_summary
            (run_id, strategy_revision_id, instrument_id, gate_code, reject_count, sampled_decision_keys)
        SELECT decision.run_id, decision.strategy_revision_id, decision.instrument_id, blocker, count(*),
               COALESCE(array_agg(decision.decision_key ORDER BY decision.decision_key)
                   FILTER (WHERE {sampled}), '{{}}')
        FROM analysis.decision decision CROSS JOIN unnest(decision.blockers) blocker
        WHERE decision.run_id = %s AND decision.state = 'REJECTED'
        GROUP BY decision.run_id, decision.strategy_revision_id, decision.instrument_id, blocker
        """, [run_id],
    )
    connection.execute(
        f"""DELETE FROM analysis.option_decision option_decision USING analysis.decision decision
            WHERE option_decision.decision_id = decision.id AND decision.run_id = %s
              AND decision.state = 'REJECTED' AND NOT ({sampled})""", [run_id],
    )
    connection.execute(
        f"""DELETE FROM analysis.decision decision
            WHERE run_id = %s AND state = 'REJECTED' AND NOT ({sampled})""", [run_id],
    )
