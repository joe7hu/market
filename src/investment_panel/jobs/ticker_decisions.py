"""Publish immutable ticker-first decisions and refresh their outcomes."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.core.config import AppConfig, load_config
from investment_panel.core.decision import build_ticker_decision
from investment_panel.core.panel import TICKER_INITIAL_TABLES
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.panel_models import load_postgres_tables
from investment_panel.database.runtime import JOB_PROFILE
from investment_panel.database.ticker_decisions import TickerDecisionRepository
from investment_panel.database.ticker_execution import TickerPaperExecutionRepository


PUBLISH_INPUT_TABLES = tuple(
    name for name in TICKER_INITIAL_TABLES
    if name not in {"ticker_decisions", "ticker_outcomes"}
)


def publish(
    config_path: str | None = None,
    *,
    symbols: Iterable[str] | None = None,
    as_of: datetime | None = None,
    limit: int = 2_000,
) -> dict[str, Any]:
    """Build and persist one point-in-time decision per equity or ETF ticker.

    The publisher deliberately excludes the persisted decision table from its
    input tables. A publication job must create a new revision when any
    dependency changes; the API read path may use the latest immutable row.
    """

    config = load_config(config_path)
    runtime = runtime_for_config(config)
    reference = _utc(as_of or datetime.now(UTC))
    benchmark_symbols = _catalog_symbols(runtime, limit=10_000)
    selected = _normalise_symbols(benchmark_symbols, symbols, limit=limit)
    benchmark = _freeze_benchmark(runtime, benchmark_symbols, reference)
    repository = TickerDecisionRepository(runtime)
    published: list[dict[str, Any]] = []
    decisions_for_paper: list[Any] = []
    skipped = 0
    failures: list[dict[str, str]] = []
    for symbol in selected:
        try:
            tables, metadata = load_postgres_tables(
                config,
                PUBLISH_INPUT_TABLES,
                query_row_limits={name: 64 for name in PUBLISH_INPUT_TABLES},
                query_symbol_filter={symbol},
                runtime_profile=JOB_PROFILE,
            )
            if not metadata.get("available_model_count"):
                raise RuntimeError("ticker input read models are unavailable")
            # The benchmark is written before the read so its membership is
            # part of the same point-in-time input manifest as the decision.
            tables.setdefault("ticker_benchmark_snapshot", []).append(benchmark)
            decision = build_ticker_decision(symbol, tables, as_of=reference)
            prior = repository.latest(symbol)
            if prior is not None and prior.input_manifest.input_hash == decision.input_manifest.input_hash:
                skipped += 1
                decisions_for_paper.append(decision)
                continue
            published.append(repository.publish(decision))
            decisions_for_paper.append(decision)
        except Exception as exc:  # one ticker cannot block the universe
            failures.append({"ticker": symbol, "error": f"{type(exc).__name__}: {exc}"})
    # Outcome maturity is independent of whether this run created a new
    # revision. A replay must still resolve older recommendations.
    outcome_result = repository.refresh_outcomes(now=reference)
    paper_staging = _stage_eligible(runtime, config, decisions_for_paper)
    paper_execution = TickerPaperExecutionRepository(runtime, config).process(now=reference)
    status = "ok" if not failures else "partial" if published or skipped else "failed"
    return {
        "status": status,
        "database": "postgresql",
        "as_of": reference,
        "universe_count": len(selected),
        "benchmark": benchmark,
        "published_count": len(published),
        "skipped_count": skipped,
        "failed_count": len(failures),
        "failures": failures[:50],
        "outcomes": outcome_result,
        "paper_staging": paper_staging,
        "paper_execution": paper_execution,
    }


def _catalog_symbols(runtime: Any, *, limit: int) -> list[str]:
    with runtime.read() as connection:
        rows = connection.execute(
            """
            SELECT symbol
            FROM catalog.instrument
            WHERE asset_class IN ('equity', 'etf')
            ORDER BY symbol
            LIMIT %s
            """,
            [max(1, min(int(limit), 10_000))],
        ).fetchall()
    return sorted({str(row["symbol"]).strip().upper() for row in rows if row["symbol"]})


def _normalise_symbols(
    available: Iterable[str],
    symbols: Iterable[str] | None,
    *,
    limit: int,
) -> list[str]:
    requested = {
        str(symbol).strip().upper()
        for symbol in symbols or ()
        if str(symbol).strip()
    }
    available_set = {str(symbol).strip().upper() for symbol in available if str(symbol).strip()}
    if requested:
        return sorted(requested & available_set)
    return sorted(available_set)[:max(1, min(int(limit), 10_000))]


def _freeze_benchmark(runtime: Any, symbols: Iterable[str], as_of: datetime) -> dict[str, Any]:
    """Freeze the equity denominator independently from option availability.

    The catalog is an explicit current membership source, not a claim that the
    catalog is a complete official index. The coverage field makes that limit
    visible until an official point-in-time constituent feed is available.
    """

    members = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    encoded_members = json.dumps(members, separators=(",", ":"), ensure_ascii=True)
    membership_hash = hashlib.sha256(encoded_members.encode("utf-8")).hexdigest()
    row = {
        "benchmark_key": "market-equity-etf",
        "as_of": as_of,
        "available_at": as_of,
        "membership_hash": membership_hash,
        "member_count": len(members),
        "source_id": "catalog.instrument",
        "source_version": "20260822_0049",
        "exact_membership": members,
        "coverage": {
            "catalog_membership_complete": True,
            "official_index_membership": False,
            "catalog_limit": 10_000,
            "price_coverage": "measured_by_confirmed_price_rows",
            "options_availability_affects_breadth": False,
        },
    }
    with runtime.transaction(JOB_PROFILE) as connection:
        connection.execute(
            """
            INSERT INTO analysis.ticker_benchmark_snapshot (
                benchmark_key, as_of, available_at, membership_hash, member_count,
                source_id, source_version, exact_membership, coverage
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (benchmark_key, as_of) DO UPDATE SET
                available_at = EXCLUDED.available_at,
                membership_hash = EXCLUDED.membership_hash,
                member_count = EXCLUDED.member_count,
                source_id = EXCLUDED.source_id,
                source_version = EXCLUDED.source_version,
                exact_membership = EXCLUDED.exact_membership,
                coverage = EXCLUDED.coverage
            """,
            [
                row["benchmark_key"], row["as_of"], row["available_at"],
                row["membership_hash"], row["member_count"], row["source_id"],
                row["source_version"], Jsonb(row["exact_membership"]),
                Jsonb(row["coverage"]),
            ],
        )
    return row


def _stage_eligible(runtime: Any, config: AppConfig, decisions: Iterable[Any]) -> dict[str, Any]:
    settings = config.analysis.options_decision_system
    if settings.mode != "paper" or not settings.ticker_paper_actions_enabled:
        return {"status": "disabled", "staged": [], "skipped": []}
    repository = TickerPaperExecutionRepository(runtime, config)
    staged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for decision in decisions:
        expression = decision.selected_expression
        action = decision.capital_action.action.value
        if action not in {
            "BUY", "ADD", "HEDGE", "WAIT_FOR_PRICE", "TRIM", "EXIT",
        }:
            skipped.append({"ticker": decision.ticker, "reason": "capital_action_not_orderable"})
            continue
        if expression is None or expression.kind.value == "CASH" or expression.status != "eligible":
            skipped.append({"ticker": decision.ticker, "reason": "no_eligible_non_cash_expression"})
            continue
        if expression.quantity is None or expression.quantity <= 0:
            skipped.append({"ticker": decision.ticker, "reason": "quantity_unavailable"})
            continue
        entry = expression.entry_range
        limit_price = ((entry.low + entry.high) / 2) if entry is not None else None
        if limit_price is None or limit_price <= 0:
            skipped.append({"ticker": decision.ticker, "reason": "entry_limit_unavailable"})
            continue
        key = f"ticker:{decision.ticker}:{decision.decision_revision}:{expression.kind.value}:{decision.capital_action.expires_at}"
        try:
            staged.append(repository.stage(
                ticker=decision.ticker,
                decision=decision,
                expression_kind=expression.kind.value,
                idempotency_key=key,
                quantity=expression.quantity,
                limit_price=limit_price,
            ))
        except ValueError as exc:
            skipped.append({"ticker": decision.ticker, "reason": str(exc)})
    return {"status": "ok", "staged": staged, "skipped": skipped}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--ticker", action="append", dest="tickers")
    parser.add_argument("--limit", type=int, default=2_000)
    args = parser.parse_args()
    print(json.dumps(publish(args.config, symbols=args.tickers, limit=args.limit), indent=2, default=str))


__all__ = ["PUBLISH_INPUT_TABLES", "publish"]
