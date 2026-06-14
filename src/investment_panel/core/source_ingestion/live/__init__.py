"""Live opencli ingestion adapters (X/social, news, blogs).

These are the first *live fetchers* in ``source_ingestion`` — unlike ``canonical``
(which only re-materializes data already in other tables) they pull fresh content
through ``providers/opencli.py:OpenCliRunner`` and persist it through the canonical
writers. Each fetch records a ``source_run`` (ok / rate_limited / failed) so the
source catalog can surface rate-limit status.

Import the public fetchers from this package, not the submodules (facade rule).
"""

from __future__ import annotations

from investment_panel.core.source_ingestion.live.blog_sources import fetch_substack, fetch_web_rss
from investment_panel.core.source_ingestion.live.common import (
    LiveFetchResult,
    extract_symbols,
    known_symbols,
    record_live_run,
)
from investment_panel.core.source_ingestion.live.news_sources import fetch_news
from investment_panel.core.source_ingestion.live.x_sources import fetch_x_account, fetch_x_list

__all__ = [
    "LiveFetchResult",
    "extract_symbols",
    "known_symbols",
    "record_live_run",
    "fetch_news",
    "fetch_substack",
    "fetch_web_rss",
    "fetch_x_account",
    "fetch_x_list",
]
