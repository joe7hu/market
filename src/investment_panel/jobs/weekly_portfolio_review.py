"""Weekly portfolio review memo."""

from __future__ import annotations

import argparse
from datetime import datetime
import json

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db, json_dumps, query_rows
from investment_panel.core.research import stable_id


def run(config_path: str | None = None) -> dict[str, str]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        candidates = query_rows(con, "SELECT symbol, score, decision FROM candidates ORDER BY run_date DESC, score DESC LIMIT 20")
        holdings = query_rows(con, "SELECT symbol, quantity, avg_cost FROM portfolio_positions ORDER BY symbol")
        markdown = build_review(candidates, holdings)
        report_id = stable_id(f"weekly:{datetime.utcnow().isoformat()}")
        con.execute(
            """
            INSERT INTO research_reports
            (id, symbol, created_at, report_type, report_markdown, report_json, evidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                report_id,
                "PORTFOLIO",
                datetime.utcnow().isoformat(),
                "weekly_portfolio_review",
                markdown,
                json_dumps({"candidates": candidates, "holdings": holdings}),
                json_dumps({"candidates": candidates, "holdings": holdings}),
            ],
        )
    return {"report_id": report_id}


def build_review(candidates: list[dict], holdings: list[dict]) -> str:
    lines = [
        "# Weekly Portfolio Review",
        "",
        "## Add / Research Candidates",
    ]
    research = [row for row in candidates if row.get("decision") == "research"][:8]
    lines.extend(f"- {row['symbol']}: score {row['score']}" for row in research) or lines.append("- None crossed research threshold.")
    lines.extend(["", "## Watch Only"])
    watch = [row for row in candidates if row.get("decision") == "watch"][:8]
    lines.extend(f"- {row['symbol']}: score {row['score']}" for row in watch) or lines.append("- No watch-only candidates.")
    lines.extend(["", "## Current Holdings"])
    lines.extend(f"- {row['symbol']}: quantity {row.get('quantity', 0)} at avg cost {row.get('avg_cost', 0)}" for row in holdings) or lines.append("- No portfolio CSV imported.")
    lines.extend(["", "## Review Checks", "- Verify any research candidate against primary sources before acting.", "- Avoid adding duplicate category exposure without a thesis update."])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2))


if __name__ == "__main__":
    main()

