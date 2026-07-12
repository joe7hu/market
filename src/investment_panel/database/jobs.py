"""PostgreSQL operational job state and single-flight semantics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg import errors
from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime


class JobRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def start(self, job_name: str, *, stale_after: timedelta = timedelta(hours=3)) -> dict[str, Any]:
        self.mark_stale(stale_after=stale_after)
        with self.runtime.transaction() as connection:
            try:
                with connection.transaction():
                    row = connection.execute(
                        """
                        INSERT INTO ops.job_run (job_name, status, started_at, heartbeat_at)
                        VALUES (%s, 'running', now(), now())
                        RETURNING id, job_name, status, started_at, heartbeat_at, finished_at, error, summary
                        """,
                        [job_name],
                    ).fetchone()
            except errors.UniqueViolation:
                row = connection.execute(
                    """
                    SELECT id, job_name, status, started_at, heartbeat_at, finished_at, error, summary
                    FROM ops.job_run WHERE job_name = %s AND status = 'running'
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    [job_name],
                ).fetchone()
                return {**dict(row), "id": str(row["id"]), "created": False}
        return {**dict(row), "id": str(row["id"]), "created": True}

    def finish(
        self,
        job_id: str | UUID,
        status: str,
        *,
        summary: Any | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"succeeded", "partial", "failed", "skipped"}:
            raise ValueError("job status is invalid")
        with self.runtime.transaction() as connection:
            row = connection.execute(
                """
                UPDATE ops.job_run
                SET status = %s, finished_at = now(), error = %s, summary = %s
                WHERE id = %s AND status = 'running'
                RETURNING id, job_name, status, started_at, heartbeat_at, finished_at, error, summary
                """,
                [status, error, Jsonb(summary if summary is not None else ({} if error is None else {"error": error})), job_id],
            ).fetchone()
        if row is None:
            raise ValueError(f"job is not running: {job_id}")
        return {**dict(row), "id": str(row["id"])}

    def rows(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT id, job_name, status, started_at, heartbeat_at, finished_at, error, summary
                FROM ops.job_run ORDER BY started_at DESC LIMIT %s
                """,
                [limit],
            ).fetchall()
        return [{**dict(row), "id": str(row["id"])} for row in rows]

    def mark_stale(
        self,
        *,
        stale_after: timedelta = timedelta(hours=3),
        reason: str | None = None,
    ) -> int:
        message = reason or f"Refresh job did not finish within {stale_after}."
        cutoff = datetime.now(UTC) - stale_after
        with self.runtime.transaction() as connection:
            result = connection.execute(
                """
                UPDATE ops.job_run
                SET status = 'failed', finished_at = now(), error = %s, summary = %s
                WHERE status = 'running' AND heartbeat_at < %s
                """,
                [message, Jsonb({"error": message}), cutoff],
            )
        return int(result.rowcount)

    def heartbeat(self, job_id: str | UUID) -> bool:
        with self.runtime.transaction() as connection:
            result = connection.execute(
                "UPDATE ops.job_run SET heartbeat_at = now() WHERE id = %s AND status = 'running'",
                [job_id],
            )
        return result.rowcount == 1

    def fail_all_running(self, reason: str) -> int:
        with self.runtime.transaction() as connection:
            result = connection.execute(
                """
                UPDATE ops.job_run
                SET status = 'failed', finished_at = now(), error = %s, summary = %s
                WHERE status = 'running'
                """,
                [reason, Jsonb({"error": reason})],
            )
        return int(result.rowcount)
