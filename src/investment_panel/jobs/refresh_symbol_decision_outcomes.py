"""Refresh point-in-time stock and ticker-first outcomes; never stage orders."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.settings import load_config
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.infrastructure.postgres.symbol_decision_outcomes import SymbolDecisionOutcomeRepository
from investment_panel.infrastructure.postgres.ticker_decisions import TickerDecisionRepository

OUTCOME_BATCH_SIZE = 25


def run(config_path: str | None = "config.yaml") -> dict[str, Any]:
    config = load_config(config_path)
    runtime = runtime_for_config(config)
    symbol_outcomes = SymbolDecisionOutcomeRepository(runtime).refresh()
    ticker_repository = TickerDecisionRepository(runtime)
    # Each ticker has six horizon writes. Keep the pass below the scheduler
    # timeout so later worker classes are not starved by this backlog.
    ticker_outcomes = ticker_repository.refresh_outcomes(limit=OUTCOME_BATCH_SIZE)
    publish_attributions = getattr(ticker_repository, "publish_outcome_attributions", None)
    pending_attributions = ticker_repository.has_pending_outcome_attributions()
    if not callable(publish_attributions):
        attribution_result = {"status": "failed", "reason": "canonical_attribution_publisher_missing"}
    elif pending_attributions:
        attribution_result = {
            "status": "skipped",
            "reason": "outcome_attribution_pending",
        }
    else:
        attribution_result = publish_attributions()
    attribution_status = str(attribution_result.get("status") or "skipped")
    downstream_status = (
        "ok" if attribution_status == "ok" else
        "failed" if attribution_status == "failed" else
        "blocked" if attribution_status == "blocked" else "not_run"
    )
    status = str(symbol_outcomes.get("status") or "ok")
    if status == "ok" and attribution_status in {"blocked", "failed"}:
        status = "partial"
    return {
        **symbol_outcomes,
        "symbol_outcomes": symbol_outcomes,
        "ticker_outcomes": ticker_outcomes,
        "ticker_outcome_attribution": attribution_result,
        "attribution_publication_id": attribution_result.get("attribution_publication_id"),
        "database": "postgresql",
        "paper_orders": 0,
        "status": status,
        "source_status": str(symbol_outcomes.get("status") or "ok"),
        "downstream_status": downstream_status,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.config), default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
