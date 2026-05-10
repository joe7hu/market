"""Benchmark a derived trader replica portfolio against a reference snapshot."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.db import db, query_rows
from investment_panel.core.status import write_source_status


REFERENCE_HOLDINGS = {
    "nancy-pelosi-2026-05-10": {
        "source": "https://pelositracker.app/portfolios/nancy-pelosi",
        "note": "External reference only; not an ingestion source.",
        "total_invested": 38970000.0,
        "performance_percent": 54.6,
        "holdings": [
            {"symbol": "NVDA", "weight": 19.0},
            {"symbol": "GOOGL", "weight": 18.0},
            {"symbol": "AVGO", "weight": 17.0},
            {"symbol": "PANW", "weight": 8.0},
            {"symbol": "VST", "weight": 7.0},
            {"symbol": "AMZN", "weight": 7.0},
            {"symbol": "TEM", "weight": 5.0},
            {"symbol": "CRWD", "weight": 5.0},
            {"symbol": "IBTA.L", "weight": 4.0},
            {"symbol": "TSLA", "weight": 3.0},
            {"symbol": "MSFT", "weight": 3.0},
            {"symbol": "AAPL", "weight": 3.0},
        ],
    }
}


def run(config_path: str | None = None, trader: str = "Nancy Pelosi", reference: str = "nancy-pelosi-2026-05-10") -> dict[str, Any]:
    config = load_config(config_path)
    reference_row = REFERENCE_HOLDINGS[reference]
    with db(config.database.duckdb_path, read_only=True) as con:
        rows = query_rows(
            con,
            """
            SELECT raw
            FROM disclosures
            WHERE source_type = 'trader_portfolio_model' AND trader_name = ?
            """,
            [trader],
        )
    if not rows:
        raise ValueError(f"No trader_portfolio_model row found for {trader}")
    model = rows[0]["raw"]
    if isinstance(model, str):
        model = json.loads(model)
    result = benchmark_model(model, reference_row)
    payload = {
        "status": result["verdict"],
        "trader": trader,
        "reference": reference,
        "reference_source": reference_row["source"],
        "reference_note": reference_row["note"],
        **result,
    }
    status_path = write_source_status(
        config,
        f"mini-market-trader-benchmark-{slug(trader)}",
        {
            "source": "market-mini",
            "job": "benchmark_trader_replica",
            "origin": "verification",
            **payload,
        },
    )
    return {**payload, "status_path": str(status_path)}


def benchmark_model(model: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    model_holdings = {row["symbol"]: row for row in model.get("holdings", [])}
    reference_holdings = {row["symbol"]: row for row in reference.get("holdings", [])}
    overlap = sorted(set(model_holdings) & set(reference_holdings))
    missing = sorted(set(reference_holdings) - set(model_holdings))
    extra = sorted(set(model_holdings) - set(reference_holdings))
    top_reference = [row["symbol"] for row in reference.get("holdings", [])[:6]]
    top_model = [row["symbol"] for row in model.get("holdings", [])[:6]]
    top_overlap = [symbol for symbol in top_reference if symbol in top_model]
    weight_errors = {
        symbol: abs(float(model_holdings[symbol].get("weight") or 0) - float(reference_holdings[symbol].get("weight") or 0))
        for symbol in overlap
    }
    mean_weight_error = sum(weight_errors.values()) / len(weight_errors) if weight_errors else 100.0
    model_total = float(model.get("total_value") or 0)
    reference_total = reference.get("current_market_value")
    total_value_error_pct = (
        abs(model_total - float(reference_total)) / float(reference_total) * 100 if reference_total else None
    )
    symbol_overlap_ratio = len(overlap) / len(reference_holdings) if reference_holdings else 0.0
    top_overlap_ratio = len(top_overlap) / len(top_reference) if top_reference else 0.0
    close = symbol_overlap_ratio >= 0.9 and top_overlap_ratio >= 0.75 and mean_weight_error <= 8.0
    if total_value_error_pct is not None:
        close = close and total_value_error_pct <= 25.0
    return {
        "verdict": "close" if close else "not_close",
        "symbol_overlap_ratio": round(symbol_overlap_ratio, 4),
        "top_overlap_ratio": round(top_overlap_ratio, 4),
        "mean_weight_error": round(mean_weight_error, 4),
        "total_value_error_pct": round(total_value_error_pct, 4) if total_value_error_pct is not None else None,
        "model_total_value": model_total,
        "reference_total_value": reference_total,
        "overlap": overlap,
        "missing": missing,
        "extra": extra,
        "top_model": top_model,
        "top_reference": top_reference,
        "weight_errors": weight_errors,
    }


def slug(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--trader", default="Nancy Pelosi")
    parser.add_argument("--reference", default="nancy-pelosi-2026-05-10")
    args = parser.parse_args()
    print(json.dumps(run(args.config, args.trader, args.reference), indent=2))


if __name__ == "__main__":
    main()
