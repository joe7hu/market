"""Close privileged and protected-role paths for the research application login."""

from __future__ import annotations

import os
import re

from alembic import op
import sqlalchemy as sa


revision = "20260902_0067"
down_revision = "20260902_0066"
branch_labels = None
depends_on = None


_PROTECTED_ROLES = ("market_research_signer", "market_migrator")
_PROTECTED_FUNCTIONS = (
    "research_evaluator_signing_key",
    "research_evaluator_output_hash_v2",
    "research_evaluator_signature_payload",
    "enforce_research_evaluator_output",
    "enforce_research_evidence_manifest",
    "enforce_validation_dossier_seal",
    "enforce_research_trial_terminal_immutability",
    "enforce_research_result_actual_availability",
    "enforce_research_gate_actual_availability",
    "enforce_research_universe_actual_availability",
    "enforce_research_revision_promotion_hardened",
    "enforce_research_revision_promotion",
    "enforce_strategy_forecast_authority",
    "research_evidence_complete",
    "research_validation_evidence_complete",
)


def upgrade() -> None:
    app_login = os.environ.get("MARKET_APP_LOGIN_ROLE", "").strip()
    if not app_login or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", app_login):
        raise RuntimeError("MARKET_APP_LOGIN_ROLE must name the explicitly configured application login")

    bind = op.get_bind()
    configured = bind.execute(
        sa.text(
            """SELECT oid, rolcanlogin, rolsuper, rolbypassrls, rolinherit,
                      rolcreaterole, rolcreatedb, rolreplication
               FROM pg_roles WHERE rolname = :role"""
        ),
        {"role": app_login},
    ).mappings().one_or_none()
    if configured is None or not configured["rolcanlogin"]:
        raise RuntimeError("MARKET_APP_LOGIN_ROLE must identify an existing login role")
    if any(
        configured[field]
        for field in (
            "rolsuper", "rolbypassrls", "rolinherit", "rolcreaterole",
            "rolcreatedb", "rolreplication",
        )
    ):
        raise RuntimeError(
            "MARKET_APP_LOGIN_ROLE must identify a non-privileged NOINHERIT non-replicating login"
        )

    protected = bind.execute(
        sa.text(
            """SELECT count(*) AS role_count,
                      count(*) FILTER (
                          WHERE NOT rolcanlogin AND NOT rolsuper
                            AND NOT rolbypassrls AND NOT rolinherit
                            AND NOT rolcreaterole AND NOT rolcreatedb
                            AND NOT rolreplication
                      ) AS safe_count
               FROM pg_roles
               WHERE rolname IN ('market_research_signer', 'market_migrator')"""
        )
    ).mappings().one()
    if protected["role_count"] != len(_PROTECTED_ROLES) or protected["safe_count"] != len(_PROTECTED_ROLES):
        raise RuntimeError("protected evaluator and migration roles have unsafe attributes")

    membership = bind.execute(
        sa.text(
            """WITH RECURSIVE role_graph(member, granted_role) AS (
                   SELECT member, roleid FROM pg_auth_members
                   UNION
                   SELECT graph.member, membership.roleid
                   FROM role_graph graph
                   JOIN pg_auth_members membership ON membership.member = graph.granted_role
               )
               SELECT bool_or(protected.rolname IN ('market_research_signer', 'market_migrator'))
                          AS reaches_protected,
                      bool_or(protected.rolname <> 'market_app') AS reaches_unapproved
               FROM role_graph graph
               JOIN pg_roles protected ON protected.oid = graph.granted_role
               WHERE graph.member = :member"""
        ),
        {"member": configured["oid"]},
    ).mappings().one()
    direct_membership = bind.execute(
        sa.text(
            """SELECT EXISTS (
                   SELECT 1
                   FROM pg_auth_members
                   WHERE member = :member
                     AND roleid = (SELECT oid FROM pg_roles WHERE rolname = 'market_app')
               ) OR :member = (SELECT oid FROM pg_roles WHERE rolname = 'market_app')
               AS direct_market_app,
               NOT EXISTS (
                   SELECT 1
                   FROM pg_auth_members
                   WHERE member = (SELECT oid FROM pg_roles WHERE rolname = 'market_app')
               ) AS market_app_is_leaf"""
        ),
        {"member": configured["oid"]},
    ).mappings().one()
    if (
        membership["reaches_protected"]
        or membership["reaches_unapproved"]
        or not direct_membership["direct_market_app"]
        or not direct_membership["market_app_is_leaf"]
    ):
        raise RuntimeError("configured application login has an unsafe role membership path")

    market_app_membership = bind.execute(
        sa.text(
            """SELECT EXISTS (
                   SELECT 1 FROM pg_auth_members
                   WHERE member = (SELECT oid FROM pg_roles WHERE rolname = 'market_app')
                     AND roleid IN (
                         SELECT oid FROM pg_roles
                         WHERE rolname IN ('market_research_signer', 'market_migrator')
                     )
               ) AS app_reaches_protected"""
        )
    ).mappings().one()
    if market_app_membership["app_reaches_protected"]:
        raise RuntimeError("market_app reaches a protected evaluator role")

    function_names = ", ".join(
        "'" + name.replace("'", "''") + "'" for name in _PROTECTED_FUNCTIONS
    )
    function_owners = bind.execute(
        sa.text(
            f"""SELECT count(*) AS function_count
                FROM pg_proc procedure
                JOIN pg_namespace namespace ON namespace.oid = procedure.pronamespace
                WHERE namespace.nspname = 'analysis'
                  AND procedure.proname IN ({function_names})
                  AND procedure.proowner = (SELECT oid FROM pg_roles WHERE rolname = 'market_research_signer')"""
        )
    ).scalar_one()
    if function_owners != len(_PROTECTED_FUNCTIONS):
        raise RuntimeError("protected evaluator functions are not owned by market_research_signer")

    objects = bind.execute(
        sa.text(
            """SELECT count(*) AS protected_object_count
               FROM pg_class
               WHERE oid IN ('analysis.research_evaluator_signing_secret'::regclass,
                             'analysis.research_evaluator_output'::regclass)
                 AND relowner = (SELECT oid FROM pg_roles WHERE rolname = 'market_research_signer')"""
        )
    ).scalar_one()
    if objects != 2:
        raise RuntimeError("protected evaluator tables are not owned by market_research_signer")

    op.execute(
        """
        REVOKE market_research_signer, market_migrator FROM market_app;
        GRANT USAGE ON SCHEMA app, raw, ingest TO market_app;
        GRANT SELECT ON ALL TABLES IN SCHEMA catalog TO market_app;
        GRANT SELECT ON
            app.current_publication_item,
            app.publication,
            app.publication_payload,
            app.publication_item,
            app.publication_bundle,
            app.publication_bundle_item,
            app.portfolio_position,
            app.portfolio_transaction,
            app.watchlist_item,
            app.thesis,
            app.option_history_policy,
            app.catalyst,
            app.thesis_automation_run,
            app.thesis_evidence_assessment,
            app.paper_order,
            catalog.option_contract,
            raw.content_item_instrument,
            raw.content_item,
            raw.disclosure,
            raw.market_event,
            raw.fundamental_observation,
            raw.quote,
            raw.confirmed_price_bar,
            raw.confirmed_quote,
            raw.price_bar,
            raw.price_bar_history,
            raw.price_bar_fact_availability,
            raw.option_quote,
            raw.option_snapshot,
            raw.broker_account_snapshot,
            ingest.source,
            ingest.run
          TO market_app;
        GRANT SELECT ON analysis.decision, analysis.option_decision,
                       analysis.ticker_decision, analysis.ticker_outcome,
                       analysis.symbol_feature
          TO market_app;
        GRANT SELECT ON analysis.source_signal TO market_app;
        GRANT EXECUTE ON FUNCTION raw.current_price_at(TIMESTAMPTZ, BIGINT[]) TO market_app;
        REVOKE ALL ON analysis.research_evaluator_signing_secret FROM PUBLIC, market_app, market_migrator;
        REVOKE ALL ON analysis.research_evaluator_output FROM PUBLIC, market_app, market_migrator;
        REVOKE ALL ON FUNCTION analysis.research_evaluator_signing_key() FROM PUBLIC, market_app, market_migrator;
        REVOKE ALL ON FUNCTION analysis.write_research_evaluator_output(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB, TEXT) FROM PUBLIC, market_migrator;
        GRANT EXECUTE ON FUNCTION analysis.write_research_evaluator_output(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB, TEXT) TO market_app;
        """
    )


def downgrade() -> None:
    # The hardening is enforced by role attributes and membership checks. Do
    # not recreate privileged memberships or grant protected object access on
    # downgrade; 0066 remains the safe compatibility boundary.
    pass
