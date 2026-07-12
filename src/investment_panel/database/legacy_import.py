"""Selective, idempotent DuckDB-to-PostgreSQL durable-state importer."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from psycopg.types.json import Jsonb

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.legacy_bootstrap import (
    import_earnings_events,
    import_fundamental_facts,
    import_latest_options,
    import_source_catalog,
    import_source_signals,
)
from investment_panel.database.source_facts import SourceFactRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


DURABLE_TABLES = (
    "portfolio_positions",
    "manual_watchlist",
    "theses",
    "trade_journal",
    "option_strategy_versions",
    "agent_thesis",
    "agent_postmortem",
    "strategy_mutation_proposal",
    "prices_daily",
    "source_items",
    "source_registry",
    "ticker_source_signals",
    "disclosures",
    "catalysts",
    "earnings_events",
    "equity_fundamentals",
    "analyst_estimates",
    "market_valuation_metric_points",
    "options_chain",
)
EXCLUDED_DERIVED_TABLES = ("option_snapshot", "option_features", "candidate_event", "radar_alert", "shadow_trade")


class LegacyImporter:
    def __init__(self, runtime: DatabaseRuntime, duckdb_path: str | Path) -> None:
        self.runtime = runtime
        self.duckdb_path = Path(duckdb_path).resolve()

    def run(self, *, report_path: str | Path | None = None) -> dict[str, Any]:
        import duckdb

        if not self.duckdb_path.is_file():
            raise FileNotFoundError(self.duckdb_path)
        legacy = duckdb.connect(str(self.duckdb_path), read_only=True)
        try:
            available = {row[0] for row in legacy.execute("SHOW TABLES").fetchall()}
            source_counts = {
                table: int(legacy.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                for table in (*DURABLE_TABLES, *EXCLUDED_DERIVED_TABLES)
                if table in available
            }
            source_catalog = _rows(legacy, "source_registry") if "source_registry" in available else []
            content_rows = _rows(legacy, "source_items") if "source_items" in available else []
            source_signal_rows = _rows(legacy, "ticker_source_signals") if "ticker_source_signals" in available else []
            import_source_catalog(self.runtime, source_catalog)
            imported = {
                "portfolio_positions": self._import_portfolio(_rows(legacy, "portfolio_positions")) if "portfolio_positions" in available else 0,
                "manual_watchlist": self._import_watchlist(_rows(legacy, "manual_watchlist")) if "manual_watchlist" in available else 0,
                "theses": self._import_theses(_rows(legacy, "theses")) if "theses" in available else 0,
                "trade_journal": self._import_trade_journal(_rows(legacy, "trade_journal")) if "trade_journal" in available else 0,
                "option_strategy_versions": self._import_strategies(_rows(legacy, "option_strategy_versions")) if "option_strategy_versions" in available else 0,
                "prices_daily": self._import_prices(_rows(legacy, "prices_daily")) if "prices_daily" in available else 0,
                "source_registry": len(source_catalog),
                "source_items": self._import_content(content_rows),
                "ticker_source_signals": import_source_signals(self.runtime, source_signal_rows),
                "disclosures": self._import_disclosures(_rows(legacy, "disclosures")) if "disclosures" in available else 0,
                "catalysts": self._import_catalysts(_rows(legacy, "catalysts")) if "catalysts" in available else 0,
                "earnings_events": import_earnings_events(
                    self.runtime, _rows(legacy, "earnings_events") if "earnings_events" in available else []
                ),
            }
            imported["fundamental_observations"] = import_fundamental_facts(
                self.runtime,
                fundamentals=_rows(legacy, "equity_fundamentals") if "equity_fundamentals" in available else [],
                estimates=_rows(legacy, "analyst_estimates") if "analyst_estimates" in available else [],
                market_valuations=_rows(legacy, "market_valuation_metric_points") if "market_valuation_metric_points" in available else [],
            )
            latest_options = _rows_query(
                legacy,
                """
                WITH latest AS (
                    SELECT symbol, max(observed_at) AS observed_at
                    FROM options_chain GROUP BY symbol
                )
                SELECT chain.* FROM options_chain chain
                JOIN latest USING (symbol, observed_at)
                """,
            ) if "options_chain" in available else []
            option_counts = import_latest_options(self.runtime, latest_options)
            imported["latest_option_snapshots"] = option_counts["snapshots"]
            imported["latest_option_quotes"] = option_counts["quotes"]
            agent_tables = [table for table in ("agent_thesis", "agent_postmortem", "strategy_mutation_proposal") if table in available]
            imported.update(self._import_agent_artifacts({table: _rows(legacy, table) for table in agent_tables}))
        finally:
            legacy.close()
        rebuilds = self._rebuild_publications()
        target_counts = self._target_counts()
        report = {
            "status": "ok",
            "source": str(self.duckdb_path),
            "source_sha256": _file_sha256(self.duckdb_path),
            "generated_at": datetime.now(UTC).isoformat(),
            "source_counts": source_counts,
            "imported_or_updated": imported,
            "target_counts": target_counts,
            "rebuilds": rebuilds,
            "excluded_derived": {table: source_counts.get(table, 0) for table in EXCLUDED_DERIVED_TABLES},
            "policy": {
                "durable_tables": list(DURABLE_TABLES),
                "excluded_reason": "recomputed from normalized raw facts or superseded operational history",
                "duckdb_modified": False,
            },
        }
        if report_path:
            destination = Path(report_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        return report

    def _rebuild_publications(self) -> dict[str, Any]:
        from investment_panel.database.market_analysis import refresh_market_publication
        from investment_panel.database.options_analysis import refresh_options_radar
        from investment_panel.database.today_analysis import refresh_today_publication

        results: dict[str, Any] = {}
        for name, refresh in (
            ("options_radar", refresh_options_radar),
            ("market", refresh_market_publication),
            ("today", refresh_today_publication),
        ):
            try:
                results[name] = refresh(self.runtime)
            except Exception as exc:
                results[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        return results

    def _import_portfolio(self, rows: list[dict[str, Any]]) -> int:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for row in rows:
                instrument_id = _upsert_instrument(connection, row["symbol"], row["symbol"], "equity", "portfolio")
                connection.execute(
                    """
                    INSERT INTO app.portfolio_position
                        (instrument_id, quantity, average_cost, purchase_date, notes, updated_at)
                    VALUES (%s, %s, %s, %s, %s, now())
                    ON CONFLICT (instrument_id) DO UPDATE
                    SET quantity = EXCLUDED.quantity, average_cost = EXCLUDED.average_cost,
                        purchase_date = EXCLUDED.purchase_date, notes = EXCLUDED.notes,
                        updated_at = now()
                    """,
                    [instrument_id, row.get("quantity") or 0, row.get("avg_cost") or 0, row.get("purchase_date"), row.get("notes") or ""],
                )
        return len(rows)

    def _import_watchlist(self, rows: list[dict[str, Any]]) -> int:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for row in rows:
                symbol = str(row["symbol"]).upper()
                instrument_id = _upsert_instrument(
                    connection, symbol, row.get("name") or symbol, row.get("asset_class") or "equity", "watchlist"
                )
                connection.execute(
                    """
                    INSERT INTO app.watchlist_item
                        (instrument_id, watch_state, notes, created_at, updated_at)
                    VALUES (%s, %s, %s, COALESCE(%s, now()), COALESCE(%s, now()))
                    ON CONFLICT (instrument_id) DO UPDATE
                    SET watch_state = EXCLUDED.watch_state, notes = EXCLUDED.notes,
                        updated_at = EXCLUDED.updated_at
                    """,
                    [instrument_id, row.get("watch_state") or "watched", row.get("notes") or "", row.get("created_at"), row.get("updated_at")],
                )
        return len(rows)

    def _import_theses(self, rows: list[dict[str, Any]]) -> int:
        changed = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for row in rows:
                symbol = str(row["symbol"]).upper()
                document = _json_value(row.get("thesis_json"), {})
                checksum = _json_hash(document)
                instrument = connection.execute(
                    """
                    INSERT INTO catalog.instrument (symbol, name, asset_class, category)
                    VALUES (%s, %s, 'equity', 'legacy-import')
                    ON CONFLICT (symbol) DO UPDATE SET updated_at = now() RETURNING id
                    """,
                    [symbol, symbol],
                ).fetchone()
                current = connection.execute(
                    "SELECT revision, thesis FROM app.thesis WHERE instrument_id = %s AND status = 'current' FOR UPDATE",
                    [instrument["id"]],
                ).fetchone()
                if current and (current["thesis"] or {}).get("_legacy_import_checksum") == checksum:
                    continue
                document["_legacy_import_checksum"] = checksum
                revision = int(current["revision"] or 0) + 1 if current else 1
                connection.execute(
                    "UPDATE app.thesis SET status = 'superseded', updated_at = now() "
                    "WHERE instrument_id = %s AND status = 'current'",
                    [instrument["id"]],
                )
                connection.execute(
                    "INSERT INTO app.thesis (instrument_id, revision, status, thesis, updated_at) "
                    "VALUES (%s, %s, 'current', %s, COALESCE(%s, now()))",
                    [instrument["id"], revision, Jsonb(document), row.get("updated_at")],
                )
                changed += 1
        return changed

    def _import_strategies(self, rows: list[dict[str, Any]]) -> int:
        repository = AnalysisRepository(self.runtime)
        for row in rows:
            repository.register_strategy(
                str(row["strategy_version"]),
                int(row.get("version") or 1),
                name=str(row.get("strategy_name") or row["strategy_version"]),
                status=str(row.get("status") or "legacy"),
                parameters=_json_value(row.get("parameters"), {}),
            )
        return len(rows)

    def _import_trade_journal(self, rows: list[dict[str, Any]]) -> int:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for row in rows:
                symbol = str(row.get("ticker") or "UNKNOWN").upper()
                instrument = connection.execute(
                    "INSERT INTO catalog.instrument (symbol, name, asset_class, category) "
                    "VALUES (%s, %s, 'equity', 'journal') ON CONFLICT (symbol) DO UPDATE SET updated_at = now() RETURNING id",
                    [symbol, symbol],
                ).fetchone()
                details = {key: _json_value(value, value) for key, value in row.items() if key not in {"journal_id", "ticker", "created_at", "notes"}}
                connection.execute(
                    """
                    INSERT INTO app.trade_journal (id, instrument_id, created_at, action, rationale, details)
                    VALUES (%s, %s, COALESCE(%s, now()), 'legacy_option_entry', %s, %s)
                    ON CONFLICT (id) DO UPDATE SET rationale = EXCLUDED.rationale, details = EXCLUDED.details
                    """,
                    [uuid5(NAMESPACE_URL, f"market:trade-journal:{row['journal_id']}"), instrument["id"], row.get("created_at"), row.get("notes"), Jsonb(details)],
                )
        return len(rows)

    def _import_agent_artifacts(self, tables: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
        counts = {table: 0 for table in tables}
        if not tables:
            return counts
        with self.runtime.transaction(JOB_PROFILE) as connection:
            agent_run = connection.execute(
                "SELECT id FROM analysis.agent_run WHERE provider = 'legacy-duckdb' ORDER BY started_at LIMIT 1"
            ).fetchone()
            if agent_run is None:
                agent_run = connection.execute(
                    """
                    INSERT INTO analysis.agent_run
                        (provider, model, trigger, started_at, finished_at, status, summary)
                    VALUES ('legacy-duckdb', 'selective-import', 'migration', now(), now(), 'succeeded', %s)
                    RETURNING id
                    """,
                    [Jsonb({"source": str(self.duckdb_path)})],
                ).fetchone()
            id_columns = {"agent_thesis": "thesis_id", "agent_postmortem": "postmortem_id", "strategy_mutation_proposal": "proposal_id"}
            for table, rows in tables.items():
                for row in rows:
                    legacy_id = str(row[id_columns[table]])
                    task_kind = {
                        "agent_thesis": "option_thesis",
                        "agent_postmortem": "option_postmortem",
                    }.get(table, f"legacy_{table}")
                    exists = connection.execute(
                        "SELECT 1 FROM analysis.agent_task WHERE task_kind = %s AND request->>'legacy_id' = %s",
                        [task_kind, legacy_id],
                    ).fetchone()
                    if exists:
                        continue
                    result = {key: _json_value(value, value) for key, value in row.items()}
                    connection.execute(
                        """
                        INSERT INTO analysis.agent_task
                            (agent_run_id, task_kind, status, request, result, validation, created_at, updated_at)
                        VALUES (%s, %s, 'imported', %s, %s, %s, COALESCE(%s, now()), now())
                        """,
                        [
                            agent_run["id"], task_kind,
                            Jsonb({"legacy_id": legacy_id, "source_table": table}), Jsonb(result),
                            Jsonb({"authority": "historical_evidence_only"}), row.get("created_at"),
                        ],
                    )
                    counts[table] += 1
        return counts

    def _import_prices(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        repository = IngestionRepository(self.runtime)
        source_id = "legacy-price-history"
        repository.register_source(
            source_id, name="Legacy price history", family="migration", kind="daily_bars",
            origin=str(self.duckdb_path), capabilities={"price_bars": True},
        )
        run_id = repository.start_run(source_id, "price_bars")
        normalized = [
            {
                "symbol": row.get("symbol"), "date": row.get("date"),
                "open": row.get("open"), "high": row.get("high"), "low": row.get("low"),
                "close": row.get("close"), "volume": row.get("volume"),
            }
            for row in rows
        ]
        count = repository.store_price_bars(run_id, source_id, normalized)
        repository.finish_run(run_id, "succeeded", item_count=count)
        return count

    def _import_content(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        repository = IngestionRepository(self.runtime)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            source_id = str(row.get("source_id") or "legacy-content").strip()
            grouped.setdefault(source_id, []).append(row)
        with self.runtime.read() as connection:
            registered = {
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM ingest.source WHERE id = ANY(%s)", [list(grouped)]
                ).fetchall()
            }
        stored = 0
        for source_id, source_rows in grouped.items():
            if source_id not in registered:
                repository.register_source(
                    source_id, name=source_id.replace("_", " ").title(), family="legacy",
                    kind="content", origin=str(self.duckdb_path), capabilities={"content": True},
                )
            run_id = repository.start_run(source_id, "content")
            normalized = [
                {
                    "source_key": row.get("id"), "kind": row.get("source_kind") or row.get("kind") or "article",
                    "title": row.get("title"), "url": row.get("url"), "author": row.get("author"),
                    "published_at": row.get("published_at"), "observed_at": row.get("observed_at"),
                    "summary": row.get("summary"), "license_status": row.get("license_status"),
                    "tickers": _json_value(row.get("tickers"), []),
                    "metadata": {"legacy_id": row.get("id"), "legacy_source_id": source_id},
                }
                for row in source_rows
            ]
            counts = SourceFactRepository(self.runtime).store_content_items(run_id, source_id, normalized)
            repository.finish_run(run_id, "succeeded", item_count=counts["items"])
            stored += counts["items"]
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                DELETE FROM raw.content_item legacy
                WHERE legacy.source_id = 'legacy-content'
                  AND EXISTS (
                    SELECT 1 FROM raw.content_item current
                    WHERE current.source_id = legacy.metadata->>'legacy_source_id'
                      AND current.source_key = legacy.source_key
                  )
                """
            )
        return stored

    def _import_disclosures(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        repository = IngestionRepository(self.runtime)
        source_id = "legacy-disclosures"
        repository.register_source(
            source_id, name="Legacy disclosures", family="migration", kind="disclosure",
            origin=str(self.duckdb_path), capabilities={"disclosures": True},
        )
        run_id = repository.start_run(source_id, "disclosures")
        normalized = [
            {
                "source_key": row.get("id"), "source_type": row.get("source_type"),
                "trader_name": row.get("trader_name"), "filer_name": row.get("filer_name"),
                "symbol": row.get("symbol"), "event_date": row.get("event_date"),
                "filed_date": row.get("filed_date"), "action": row.get("action"),
                "amount_text": row.get("amount"), "source_url": row.get("source_url"),
                "details": _json_value(row.get("raw"), {}),
            }
            for row in rows
        ]
        count = SourceFactRepository(self.runtime).store_disclosures(run_id, source_id, normalized)
        repository.finish_run(run_id, "succeeded", item_count=count)
        return count

    def _import_catalysts(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        repository = IngestionRepository(self.runtime)
        source_id = "legacy-events"
        repository.register_source(
            source_id, name="Legacy event calendar", family="migration", kind="calendar",
            origin=str(self.duckdb_path), capabilities={"events": True},
        )
        run_id = repository.start_run(source_id, "events")
        normalized = [
            {
                "source_key": row.get("id"), "symbol": row.get("symbol"),
                "event_scope": row.get("event_scope"), "event_kind": row.get("event_kind"),
                "title": row.get("event"), "starts_at": row.get("start_at") or row.get("event_date"),
                "ends_at": row.get("end_at"), "importance": row.get("importance"),
                "verification_status": row.get("verification_status"), "source_url": row.get("source_url"),
                "expected_impact": row.get("expected_impact"),
                "details": _json_value(row.get("raw"), {}),
            }
            for row in rows
        ]
        count = SourceFactRepository(self.runtime).store_market_events(run_id, source_id, normalized)
        repository.finish_run(run_id, "succeeded", item_count=count)
        return count

    def _target_counts(self) -> dict[str, int]:
        queries = {
            "portfolio_positions": "SELECT count(*) FROM app.portfolio_position",
            "manual_watchlist": "SELECT count(*) FROM app.watchlist_item",
            "theses_current": "SELECT count(*) FROM app.thesis WHERE status = 'current'",
            "trade_journal": "SELECT count(*) FROM app.trade_journal",
            "strategy_revisions": "SELECT count(*) FROM analysis.strategy_revision",
            "legacy_agent_tasks": "SELECT count(*) FROM analysis.agent_task WHERE request ? 'legacy_id'",
            "price_bars": "SELECT count(*) FROM raw.price_bar",
            "content_items": "SELECT count(*) FROM raw.content_item",
            "content_item_links": "SELECT count(*) FROM raw.content_item_instrument",
            "source_signals": "SELECT count(*) FROM analysis.source_signal",
            "disclosures": "SELECT count(*) FROM raw.disclosure",
            "market_events": "SELECT count(*) FROM raw.market_event",
            "fundamental_observations": "SELECT count(*) FROM raw.fundamental_observation",
            "option_snapshots": "SELECT count(*) FROM raw.option_snapshot",
            "option_quotes": "SELECT count(*) FROM raw.option_quote",
        }
        with self.runtime.read() as connection:
            return {name: int(connection.execute(query).fetchone()["count"]) for name, query in queries.items()}


def _rows(connection: Any, table: str) -> list[dict[str, Any]]:
    cursor = connection.execute(f"SELECT * FROM {table}")
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _rows_query(connection: Any, query: str) -> list[dict[str, Any]]:
    cursor = connection.execute(query)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _upsert_instrument(connection: Any, symbol: Any, name: Any, asset_class: Any, category: str) -> int:
    normalized = str(symbol).strip().upper()
    row = connection.execute(
        """
        INSERT INTO catalog.instrument (symbol, name, asset_class, category)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE
        SET name = EXCLUDED.name, asset_class = EXCLUDED.asset_class, updated_at = now()
        RETURNING id
        """,
        [normalized, str(name or normalized), str(asset_class or "equity"), category],
    ).fetchone()
    return int(row["id"])


def _json_value(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list, int, float, bool)) or value is None:
        return fallback if value is None else value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return fallback


def _json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duckdb", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    runtime = DatabaseRuntime(args.database_url)
    runtime.open()
    try:
        report = LegacyImporter(runtime, args.duckdb).run(report_path=args.report)
    finally:
        runtime.close()
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
