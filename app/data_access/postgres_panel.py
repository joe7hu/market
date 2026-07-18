"""API adapter for the PostgreSQL panel model catalog."""

from investment_panel.database.panel_models import load_postgres_tables

__all__ = ["load_postgres_tables"]
