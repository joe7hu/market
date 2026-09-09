"""Bounded PostgreSQL pages for review-only panel collections."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from investment_panel.settings import AppConfig
from investment_panel.infrastructure.postgres.agents import AgentRepository
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.infrastructure.postgres.panel_models import AGENT_MODELS, MODEL_ALIASES, QUERY_POLICIES


# These are review views: source values and counts may change between pages.
# Stable source identities prevent updates to returns or mark times from repeating rows.
REVIEW_PAGE_KEYS = {
    "candidate_event_mark": ("decision.id", "decision.created_at"),
    "candidate_event_attribution": ("decision.id", "decision.created_at"),
    "missed_winner_event": ("decision.id", "decision.created_at"),
    "strategy_backtest_result": ("evaluation.id", "evaluation.evaluated_at"),
    "strategy_forward_test_result": ("evaluation.id", "evaluation.evaluated_at"),
    "strategy_mutation_proposal": ("task.id", "task.created_at"),
}


def load_postgres_table_page(
    config: AppConfig,
    table_name: str,
    *,
    limit: int,
    snapshot_at: datetime,
    after: tuple[Any, str] | None = None,
) -> tuple[list[dict[str, Any]], int, tuple[Any, str] | None]:
    if not 1 <= limit <= 100:
        raise ValueError("review page limit must be between 1 and 100")
    runtime = runtime_for_config(config)
    if table_name in AGENT_MODELS:
        after_time = after[0] if after else None
        if isinstance(after_time, str):
            after_time = datetime.fromisoformat(after_time)
        rows, total = AgentRepository(runtime).rows_page(
            table_name,
            limit=limit,
            created_before=snapshot_at,
            after_created_at=after_time,
            after_id=after[1] if after else None,
        )
        next_after = (rows[-1]["created_at"], rows[-1]["request_id"]) if len(rows) == limit else None
        return rows, total, next_after
    model = MODEL_ALIASES.get(table_name) or table_name
    policy = QUERY_POLICIES.get(model)
    if policy is None or policy.custom_loader or model not in {*REVIEW_PAGE_KEYS, "strategy_cohort_result"}:
        raise ValueError(f"model does not support bounded paging: {table_name}")
    if snapshot_at.tzinfo is None or snapshot_at < datetime.now(UTC) - timedelta(minutes=30):
        raise ValueError("review cursor expired; reload the collection")
    query = policy.query.rsplit("ORDER BY", 1)[0].strip()
    if model == "strategy_cohort_result":
        query = f"SELECT cohort.*, stable_key AS __page_key FROM ({query}) cohort"
        cutoff = "true"
        params = []
    else:
        key, created_at = REVIEW_PAGE_KEYS[model]
        query = query.replace("SELECT", f"SELECT {key} AS __page_key, {created_at} AS __page_time,", 1)
        cutoff = "__page_time <= %s"
        params = [snapshot_at]
    after_key = None
    if after:
        if after[0] != model:
            raise ValueError("invalid review cursor collection")
        try:
            after_key = str(after[1]) if model == "strategy_cohort_result" else UUID(str(after[1]))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid review cursor key") from exc
        if not after_key:
            raise ValueError("invalid review cursor key")
    with runtime.snapshot() as connection:
        total = connection.execute(
            f"SELECT count(*) AS total FROM ({query}) review WHERE {cutoff}", params,
        ).fetchone()["total"]
        rows = connection.execute(
            f"SELECT * FROM ({query}) review WHERE {cutoff} "
            + ("AND __page_key < %s " if after_key is not None else "")
            + "ORDER BY __page_key DESC LIMIT %s",
            [*params, *([after_key] if after_key is not None else []), limit + 1],
        ).fetchall()
    page = rows[:limit]
    next_after = (model, str(page[-1]["__page_key"])) if len(rows) > limit else None
    for row in page:
        row.pop("__page_key")
        row.pop("__page_time", None)
    return page, total, next_after
