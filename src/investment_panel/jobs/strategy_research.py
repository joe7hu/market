"""Run persisted strategy definitions over PostgreSQL confirmed daily bars."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any, Literal

from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.infrastructure.postgres.strategy_factory import StrategyFactoryRepository
from investment_panel.infrastructure.postgres.strategy_inputs import load_strategy_inputs
from investment_panel.settings import load_config
from investment_panel.workflows.strategies import StrategyWorkflow


def run(
    config_path: str | None = None,
    *,
    strategy_keys: list[str] | None = None,
    symbols: list[str] | None = None,
    benchmark_symbols: list[str] | None = None,
    as_of: datetime | None = None,
    mode: Literal["research", "replay"] = "research",
) -> dict[str, Any]:
    config = load_config(config_path)
    runtime = runtime_for_config(config)
    reference = as_of or datetime.now(UTC)
    if reference.tzinfo is None:
        raise ValueError("strategy research cutoff must be timezone-aware")
    reference = reference.astimezone(UTC)
    repository = StrategyFactoryRepository(runtime)
    with runtime.read() as connection:
        if strategy_keys:
            keys = tuple(strategy_keys)
        else:
            keys = tuple(
                str(row["strategy_key"])
                for row in connection.execute(
                    """SELECT strategy_key FROM analysis.strategy_revision
                       WHERE p3_enabled IS TRUE AND status <> 'superseded'
                       GROUP BY strategy_key ORDER BY strategy_key LIMIT 256""",
                ).fetchall()
            )
        if not keys:
            raise ValueError("no persisted strategy definitions are available")
    specs = tuple(repository.resolve(key) for key in keys)
    with runtime.read() as connection:
        inputs = load_strategy_inputs(
            connection, specs, as_of=reference, symbols=symbols, benchmark_symbols=benchmark_symbols,
        )
    workflow = StrategyWorkflow(repository)
    results = workflow.run(keys, inputs, input_cutoff=reference, mode=mode)
    return {
        "mode": mode,
        "input_cutoff": reference,
        "scope_count": len(inputs),
        "result_count": len(results),
        "available_count": sum(result.signal.status == "available" for result in results),
        "results": [
            {"scope": result.scope, "strategy_key": result.strategy_key, "revision": result.revision,
             "status": result.signal.status, "actionability": result.signal.actionability,
             "blockers": result.signal.blockers, "input_hash": result.input_hash}
            for result in results
        ],
    }


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--strategy", action="append", dest="strategy_keys")
    parser.add_argument("--ticker", action="append", dest="symbols")
    parser.add_argument("--benchmark", action="append", dest="benchmark_symbols")
    parser.add_argument("--as-of")
    parser.add_argument("--mode", choices=("research", "replay"), default="research")
    args = parser.parse_args(argv)
    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else None
    print(json.dumps(run(args.config, strategy_keys=args.strategy_keys, symbols=args.symbols, benchmark_symbols=args.benchmark_symbols, as_of=as_of, mode=args.mode), indent=2, default=str))


__all__ = ["main", "run"]
