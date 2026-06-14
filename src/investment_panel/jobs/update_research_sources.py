"""Pull live news + blog/memo sources via opencli.

News providers (bloomberg/reuters/google-news/hackernews) are fetched hourly and
blogs (substack/web RSS) daily. Each fetch records a ``source_run`` and respects
``OpenCliRateLimitError`` by short-circuiting the remaining calls in its group.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.providers.opencli import OpenCliRunner
from investment_panel.core.source_ingestion.live import (
    fetch_news,
    fetch_substack,
    fetch_web_rss,
    known_symbols,
)


def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    news_config = config.research_sources.news
    blogs_config = config.research_sources.blogs

    init_db(config.database.duckdb_path)
    runner = OpenCliRunner(
        command=config.data_sources.opencli.command,
        timeout_seconds=config.data_sources.opencli.timeout_seconds,
    )
    news_runs: list[dict[str, Any]] = []
    blog_runs: list[dict[str, Any]] = []
    with db(config.database.duckdb_path, read_only=False) as con:
        known = known_symbols(con)

        if news_config.enabled:
            for provider in news_config.providers:
                result = fetch_news(con, runner, provider, limit=news_config.limit, known=known)
                news_runs.append(result.as_dict())
                if result.rate_limited:
                    break

        if blogs_config.enabled:
            for url in blogs_config.substack_urls:
                result = fetch_substack(con, runner, url, known=known)
                blog_runs.append(result.as_dict())
                if result.rate_limited:
                    break
            for url in blogs_config.rss_urls:
                result = fetch_web_rss(con, runner, url, known=known)
                blog_runs.append(result.as_dict())
                if result.rate_limited:
                    break

    runs = news_runs + blog_runs
    items = sum(int(run.get("items") or 0) for run in runs)
    signals = sum(int(run.get("signals") or 0) for run in runs)
    rate_limited = any(run.get("rate_limited") for run in runs)
    return {
        "status": "rate_limited" if rate_limited else "ok",
        "source": "research",
        "database": str(config.database.duckdb_path),
        "items": items,
        "signals": signals,
        "rate_limited": rate_limited,
        "news_runs": news_runs,
        "blog_runs": blog_runs,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2, default=str))


if __name__ == "__main__":
    main()
