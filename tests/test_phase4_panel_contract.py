from investment_panel.core.panel import PHASE4_PORTFOLIO_TABLES, tables_for_scope
from investment_panel.database.panel_models import DIRECT_QUERIES


def test_phase4_read_models_are_shared_by_exactly_five_workspaces() -> None:
    scopes = ("today", "opportunities", "portfolio", "research", "health")
    assert all(set(PHASE4_PORTFOLIO_TABLES) <= set(tables_for_scope(scope)) for scope in scopes)
    assert all(name in DIRECT_QUERIES for name in PHASE4_PORTFOLIO_TABLES)
