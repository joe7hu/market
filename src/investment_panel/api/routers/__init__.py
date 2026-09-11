"""FastAPI routers. Add a new responsibility router here and include it in
`ALL_ROUTERS`; do not grow route logic back into `app/main.py`."""
from __future__ import annotations

from investment_panel.api.routers import agent, continuous_advisor, event_scout, market_data, options, panel, paper, portfolio, sources, storage, system, theses, tickers

ALL_ROUTERS = [
    panel.router,
    event_scout.router,
    tickers.router,
    portfolio.router,
    theses.router,
    sources.router,
    market_data.router,
    options.router,
    system.router,
    storage.router,
    agent.router,
    continuous_advisor.router,
    paper.router,
]
