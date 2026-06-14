"""FastAPI routers. Add a new responsibility router here and include it in
`ALL_ROUTERS`; do not grow route logic back into `app/main.py`."""
from __future__ import annotations

from app.routers import brokers, market_data, options, panel, portfolio, sources, system, theses, tickers

ALL_ROUTERS = [
    panel.router,
    tickers.router,
    portfolio.router,
    theses.router,
    sources.router,
    market_data.router,
    options.router,
    brokers.router,
    system.router,
]
