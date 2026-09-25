"""PostgreSQL publication isolation; plan/evidence validators have separate tests.

A missing or invalid plan must neither suppress a complete independent plan nor
leak a partially built six-unit set into canonical authority.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from investment_panel.domain.decision import OutcomeAttribution, outcome_attribution_stable_key
from investment_panel.infrastructure.postgres import ticker_decisions
from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
from investment_panel.infrastructure.postgres.instruments import reconcile_instrument
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


@pytest.mark.parametrize('failure', ['missing_units', 'invalid_unit', 'plan_blocked'])
def test_complete_plan_publishes_without_incomplete_plan_or_partial_set(migrated_postgres_dsn, monkeypatch, failure):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    cutoff = datetime(2026, 8, 20, 14, tzinfo=UTC)
    observed = cutoff + timedelta(days=1)
    try:
        with runtime.transaction() as connection:
            instrument = reconcile_instrument(connection, 'PUB-RECOVERY')
            for name in ('good', 'bad'):
                decision = connection.execute("""
                    INSERT INTO analysis.ticker_decision (instrument_id, decision_revision,
                      contract_version, as_of, input_hash, code_version, experiment_id,
                      tactical, fundamental, capital_action, risk_policy, input_manifest)
                    VALUES (%s, %s, 'test', %s, %s, 'test', 'test', '{}', '{}', '{}', '{}', %s)
                    RETURNING id
                """, [instrument, name, cutoff, 'f' * 64, Jsonb({'trade_plan': {'id': name}})]).fetchone()['id']
                for horizon, periods in ticker_decisions.HORIZON_SESSIONS.items():
                    for sessions in periods:
                        if name == 'bad' and failure == 'missing_units' and sessions == 252:
                            continue
                        connection.execute("""
                            INSERT INTO analysis.ticker_outcome (ticker_decision_id, horizon,
                              horizon_sessions, state, measured_through, available_at, selected_expression)
                            VALUES (%s, %s, %s, 'observing', %s, %s, 'STOCK')
                        """, [decision, horizon.value, sessions, observed, observed])

        def authority(row):
            name = row['decision_revision']
            return SimpleNamespace(trade_plan_id=name), 'plan_unavailable' if name == 'bad' and failure == 'plan_blocked' else None

        def attribution(plan, outcome, *, evaluation_cutoff, paper_execution):
            if plan.trade_plan_id == 'bad' and failure == 'invalid_unit' and outcome['horizon_sessions'] == 252:
                return None
            evidence = {'kind': 'STOCK', 'source_id': 'fixture-confirmed-bar', 'observed_at': observed,
                        'available_at': observed, 'gross_return': .1, 'evidence_state': 'OBSERVED'}
            return OutcomeAttribution.model_validate({
                'stable_unit_key': outcome_attribution_stable_key(plan.trade_plan_id, outcome['horizon'], outcome['horizon_sessions']),
                'ticker': 'PUB-RECOVERY', 'trade_plan_id': plan.trade_plan_id,
                'trade_plan_publication_id': 'ranking', 'opportunity_episode_id': 'episode-' + plan.trade_plan_id,
                'decision_revision': plan.trade_plan_id, 'policy_version': 'policy',
                'selected_expression_kind': 'STOCK', 'selected_expression_identity': 'expression',
                'rank_id': 'rank', 'alpha_signal_id': 'signal', 'portfolio_impact_id': 'impact',
                'market_snapshot_id': 'snapshot', 'market_state_publication_id': 'market',
                'decision_cutoff': cutoff, 'evaluation_cutoff': evaluation_cutoff,
                'horizon': outcome['horizon'], 'horizon_sessions': outcome['horizon_sessions'],
                'state': 'OBSERVING', 'observed_through': observed, 'available_at': observed,
                'outcome_evidence': [evidence], 'selected_evidence': evidence,
                'counterfactuals': {'STOCK': evidence}, 'all_expression_counterfactuals': {'STOCK': evidence},
                'evidence_state': 'OBSERVED', 'sample_eligible': False, 'promotion_eligible': False,
            })
        monkeypatch.setattr(ticker_decisions, 'plan_authority', authority)
        monkeypatch.setattr(ticker_decisions, '_build_outcome_attribution', attribution)
        repository = ticker_decisions.TickerDecisionRepository(runtime)
        result = repository.publish_outcome_attributions(now=observed)
        assert result['status'] == 'partial', result
        assert result['published_count'] == 6
        assert result['excluded_plan_count'] == 1
        assert result['blockers']
        rows = AnalysisRepository(runtime).publication_rows('ticker-outcome-attribution', 'outcome_attribution')
        assert len(rows) == 6
        assert {row['trade_plan_id'] for row in rows} == {'good'}
        assert all(not row['promotion_eligible'] for row in rows)
        replay = repository.publish_outcome_attributions(now=observed)
        assert replay['attribution_publication_id'] == result['attribution_publication_id']
        with runtime.read() as connection:
            assert connection.execute('SELECT count(*) AS n FROM app.paper_order').fetchone()['n'] == 0
    finally:
        runtime.close()
