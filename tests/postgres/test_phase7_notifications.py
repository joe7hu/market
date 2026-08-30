from __future__ import annotations

from investment_panel.database.decision_inbox import DecisionInboxRepository
from investment_panel.database.runtime import DatabaseRuntime


def test_notifications_emit_only_meaningful_state_transitions(
    migrated_postgres_dsn: str,
) -> None:
    """BIG-A16: repeated observations use exact episode/revision/policy dedupe."""

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        repository = DecisionInboxRepository(runtime)
        first = repository.emit_governance_transition(
            episode_id="episode-phase7",
            decision_revision="revision-1",
            transition="newly_actionable",
            policy_version="policy-phase7",
            payload={"ticker": "PH7"},
        )
        repeated = repository.emit_governance_transition(
            episode_id="episode-phase7",
            decision_revision="revision-1",
            transition="newly_actionable",
            policy_version="policy-phase7",
            payload={"ticker": "PH7"},
        )
        changed = repository.emit_governance_transition(
            episode_id="episode-phase7",
            decision_revision="revision-2",
            transition="action_changed",
            policy_version="policy-phase7",
            payload={"ticker": "PH7"},
        )
        assert first["created"] is True
        assert repeated["created"] is False
        assert changed["created"] is True
        with runtime.read() as connection:
            count = connection.execute(
                "SELECT count(*) AS count FROM app.notification_outbox"
            ).fetchone()["count"]
        assert count == 2
    finally:
        runtime.close()
