"""API adapter for PostgreSQL-owned portfolio calculations."""

from investment_panel.database.portfolio_math import adjacent_session_dates, aligned_pair_returns

__all__ = ["adjacent_session_dates", "aligned_pair_returns"]
