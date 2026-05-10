"""Refresh Arco/Birdclaw thesis evidence into DuckDB."""

from __future__ import annotations

import argparse
import json

from investment_panel.core.arco import ingest_arco_theses, load_arco_context
from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.core.status import write_source_status


def run(config_path: str | None = None) -> dict[str, int | str]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    context = load_arco_context(config.arco)
    with db(config.database.duckdb_path) as con:
        rows = ingest_arco_theses(con, context)
    result = {
        "database": str(config.database.duckdb_path),
        "rows": rows,
        "bookmarks_path": context.get("bookmarks_path"),
        "manifest_path": context.get("manifest_path"),
    }
    status_path = write_source_status(
        config,
        "mini-market-arco-import",
        {
            "source": "market-mini",
            "job": "update_arco_data",
            "origin": "derived_analysis",
            **result,
        },
    )
    return {**result, "status_path": str(status_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2))


if __name__ == "__main__":
    main()
