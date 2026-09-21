"""Collect monitored reference quotes and produce features independent of options.

Collectors are bounded, per-symbol failures are retained, and no account/order
state is changed. Publication has its own visibility cutoff and cadence.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any

from investment_panel.settings import load_config
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
from investment_panel.infrastructure.postgres.monitored_universe import monitored_universe
from investment_panel.infrastructure.postgres.symbol_trends import refresh_symbol_trend_features
from investment_panel.infrastructure.providers.assessment_quotes import fetch_assessment_quote


def collect(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    runtime = runtime_for_config(config)
    repository = IngestionRepository(runtime)
    universe = monitored_universe(runtime, config.watchlist)
    groups: dict[str, list[dict[str, str]]] = {}
    for item in universe:
        groups.setdefault("crypto" if item["asset_class"] == "crypto" else "equity", []).append(item)
    errors: dict[str, str] = {}
    stored = 0
    for market, members in groups.items():
        source = f"assessment-{market}-quotes"
        repository.register_source(source, name=f"{market.title()} assessment references", family="market_data",
            kind="crypto_quote" if market == "crypto" else "intraday_quote", origin="Coinbase Exchange / Yahoo chart",
            capabilities={"quotes": True, "assessment_only": True, "execution_quotes": False})
        with repository.run(source, "assessment_quotes", started_at=datetime.now(UTC)) as ingestion:
            quotes: list[dict[str, Any]] = []
            local_errors: dict[str, str] = {}
            # Four bounded HTTP requests; never one thread per monitored asset.
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(fetch_assessment_quote, row["symbol"], row["asset_class"]): row["symbol"] for row in members}
                for future in as_completed(futures):
                    symbol = futures[future]
                    try:
                        quotes.append(future.result())
                    except Exception as exc:
                        local_errors[symbol] = f"{type(exc).__name__}: {exc}"
            count = repository.store_quotes(ingestion.id, source, quotes)
            if quotes and not count:
                local_errors.update({row["symbol"]: "No quote accepted by confirmation authority" for row in quotes})
            status = "failed" if not count else "partial" if local_errors else "succeeded"
            ingestion.finish(status, item_count=count, instrument_count=count,
                failure_detail="; ".join(f"{key}: {value}" for key, value in local_errors.items())[:4000] or None,
                summary={"requested": len(members), "stored": count, "errors": local_errors,
                         "assessment_only": True, "providers": sorted({row["provider"] for row in quotes})})
            stored += count
            errors.update(local_errors)
    return {"status": "failed" if errors and not stored else "partial" if errors else "ok",
            "requested": len(universe), "stored": stored, "errors": errors, "assessment_only": True}


def features(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    runtime = runtime_for_config(config)
    symbols = [row["symbol"] for row in monitored_universe(runtime, config.watchlist)]
    reference = datetime.now(UTC)
    analysis_repository = AnalysisRepository(runtime)
    run_id = analysis_repository.start_run("monitored-symbol-features", input_cutoff=reference,
        code_version="monitored-features.v1", inputs={"symbols": symbols})
    try:
        result = refresh_symbol_trend_features(runtime, run_id, as_of=reference, symbols=symbols)
        status = "succeeded" if not result["failures"] else "partial" if result["complete_count"] else "failed"
        analysis_repository.finish_run(run_id, status, {"feature_count": result["feature_count"], "failures": result["failures"]})
        return {**result, "run_id": str(run_id), "status": "ok" if status == "succeeded" else status}
    except Exception as exc:
        analysis_repository.finish_run(run_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
        raise


__all__ = ["collect", "features"]
