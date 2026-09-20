"""Read-only smoke checks for the deployed workstation, with a redacted report.

No refresh, funding, strategy promotion, or order endpoint is called. This checks
API contracts and observed readiness, not strategy profitability or fill quality.
Run with the local session's normal network access; no credentials are harvested.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from investment_panel.domain.panel import PANEL_SCOPE_TABLES
from investment_panel.infrastructure.postgres.migrations import HEAD_REVISION

RUNTIME_ENDPOINT = ("runtime", "/api/status")
ENDPOINTS = (
    RUNTIME_ENDPOINT,
    ("workflow", "/api/workstation/status"),
    ("market", "/api/panel-snapshot?scope=market&limit=120"),
    ("paper", "/api/paper/performance?book=paper"),
    ("nav", "/api/paper/account-history?days=7"),
    ("learning", "/api/research/overview"),
    ("opportunities", "/api/panel-snapshot?scope=opportunities&limit=20"),
    ("paper_trades", "/api/paper/trades?book=paper&limit=20"),
)
TODAY_STABILITY_ENDPOINTS = (
    ("today", "/api/today"),
    ("today_snapshot", "/api/panel-snapshot?scope=today"),
)
TODAY_STABILITY_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ContractError(ValueError):
    """The response cannot be interpreted as the requested contract."""


def object_value(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{name} must be an object")
    return value


def list_value(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{name} must be an array")
    return value


def count_value(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{name} must be a nonnegative integer")
    return value


def frontend_build_matches(actual: object, expected: str) -> bool:
    value = str(actual or "")
    return value == expected or bool(re.fullmatch(r"[0-9a-f]{4,40}", value, re.I) and expected.startswith(value))


def stability_digest(name: str, payload: dict[str, Any]) -> str:
    if name == "today":
        actions = payload.get("actions")
        value = [
            {key: field for key, field in action.items()
             if key != "current_at" or action.get("current_at_is_fallback") is not True}
            for action in actions
        ] if isinstance(actions, list) else actions
    elif name == "today_snapshot":
        value = {"scope": payload.get("scope"), "tables": payload.get("tables")}
    else:
        raise ValueError("Unknown stability check")
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_json(base_url: str, path: str, timeout: float) -> dict[str, Any]:
    request = Request(base_url.rstrip("/") + path, method="GET", headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ContractError("Response exceeds the bounded review size")
    def invalid_constant(_value: str) -> None:
        raise ContractError("Response contains a non-finite JSON number")
    return object_value(json.loads(data, parse_constant=invalid_constant), "response")


def assess(name: str, payload: dict[str, Any], *, expected_commit: str | None = None,
           expected_schema: str | None = None) -> dict[str, Any]:
    """Return only allowlisted counters/states: never accounts, tickers or prompts."""
    result: dict[str, Any] = {"check": name, "status": "pass", "warnings": [], "evidence": {}}
    warnings, evidence = result["warnings"], result["evidence"]
    if name == "runtime":
        if not isinstance(payload.get("ready"), bool):
            raise ContractError("Runtime readiness must be explicit")
        metadata = object_value(payload.get("metadata"), "runtime.metadata")
        release = object_value(metadata.get("release"), "runtime.release")
        evidence.update(
            backend_commit=release.get("backend_commit"),
            frontend_build=release.get("frontend_build"),
            scheduler_release=release.get("scheduler_release"),
            schema=metadata.get("schema_revision"),
        )
        if payload["ready"] is not True:
            warnings.append("Runtime reports incomplete source/read readiness")
        if metadata.get("schema_compatible") is not True:
            warnings.append("Database schema compatibility is not established")
        if expected_schema and metadata.get("schema_revision") != expected_schema:
            warnings.append("Deployed schema does not match the requested revision")
        if expected_commit:
            if release.get("backend_commit") != expected_commit:
                warnings.append("Deployed backend_commit is not the requested full commit SHA")
            if release.get("frontend_build") not in {None, "", "unknown"} and not frontend_build_matches(release.get("frontend_build"), expected_commit):
                warnings.append("Deployed frontend_build is not the requested commit SHA")
            if release.get("scheduler_release") != expected_commit:
                warnings.append("Deployed scheduler_release is not the requested full commit SHA")
    elif name == "workflow":
        if payload.get("paper_only") is not True:
            raise ContractError("Workflow must explicitly declare paper-only scope")
        failures = list_value(payload.get("failed_reads"), "workflow.failed_reads")
        market = object_value(payload.get("market"), "workflow.market")
        workers = list_value(payload.get("workers"), "workflow.workers")
        evidence.update(failed_read_count=len(failures), market_status=market.get("status"),
                        market_session=payload.get("market_session"), worker_count=len(workers))
        if failures:
            warnings.append("One or more workflow evidence queries failed")
        if market.get("status") != "available":
            warnings.append("Required Market evidence is not current and available")
        manager = next((object_value(row, "worker") for row in workers if isinstance(row, dict)
                        and row.get("job") == "process_options_paper_orders"), {})
        evidence["paper_manager_status"] = manager.get("status")
        if manager.get("status") not in {"succeeded", "running"}:
            warnings.append("Paper manager is not confirmed active/healthy")
        for population in ("paper", "observations"):
            row = object_value(payload.get(population), population)
            if row.get("status") != "available":
                warnings.append(f"{population} population could not be read; no zero is assumed")
            else:
                counts = object_value(row.get("counts"), population + ".counts")
                evidence[population + "_record_count"] = sum(count_value(value, "population count") for value in counts.values())
    elif name == "market":
        if payload.get("scope") != "market":
            raise ContractError("Market response has the wrong scope")
        status = object_value(payload.get("status"), "market.status")
        tables = object_value(payload.get("tables"), "market.tables")
        if status.get("ready") is not True:
            warnings.append("Market read is not ready")
        publications = set()
        model_counts = {}
        for key in ("market_environment_assets", "market_environment_model", "market_state_snapshot", "coverage_matrix"):
            table = object_value(tables.get(key), key)
            rows = list_value(table.get("rows"), key + ".rows")
            total = count_value(table.get("count"), key + ".count")
            if total < len(rows):
                raise ContractError("Loaded rows exceed the declared model population")
            model_counts[key] = total
            for row in rows:
                row = object_value(row, "market row")
                if row.get("publication_id"):
                    publications.add(str(row["publication_id"]))
            if not rows:
                warnings.append(f"Required model {key} has no loaded rows")
        if len(publications) > 1:
            raise ContractError("Required Market models mix different publications")
        evidence.update(model_counts=model_counts, publication_count=len(publications))
    elif name == "today":
        status = object_value(payload.get("status"), "today.status")
        actions = list_value(payload.get("actions"), "today.actions")
        if status.get("ready") is not True:
            warnings.append("Today read is not ready")
        vague = ("a complete trade plan is not available", "review the evidence below", "review evidence")
        blocked_count = 0
        for source in actions:
            item = object_value(source, "today action")
            blocked_count += int(bool(item.get("primary_blocker")))
            text = " ".join(str(item.get(key) or "") for key in ("next_action", "rationale")).lower()
            if any(phrase in text for phrase in vague):
                warnings.append("An action still uses vague plan/evidence copy; inspect the corresponding ticker locally")
            if item.get("primary_blocker") and not str(item.get("next_action") or "").strip():
                raise ContractError("Blocked action has no next step")
        evidence.update(loaded_action_count=len(actions), blocked_action_count=blocked_count)
    elif name == "today_snapshot":
        if payload.get("scope") != "today":
            raise ContractError("Today snapshot has the wrong scope")
        status = object_value(payload.get("status"), "today_snapshot.status")
        tables = object_value(payload.get("tables"), "today_snapshot.tables")
        for table_name in PANEL_SCOPE_TABLES["today"]:
            table = object_value(tables.get(table_name), f"today_snapshot.{table_name}")
            rows = list_value(table.get("rows"), f"today_snapshot.{table_name}.rows")
            if count_value(table.get("count"), f"today_snapshot.{table_name}.count") < len(rows):
                raise ContractError("Today snapshot table count is smaller than its loaded rows")
        if status.get("ready") is not True:
            warnings.append("Today snapshot is not ready")
    elif name == "opportunities":
        if payload.get("scope") != "opportunities":
            raise ContractError("Opportunity response has the wrong scope")
        status = object_value(payload.get("status"), "opportunities.status")
        table = object_value(object_value(payload.get("tables"), "opportunities.tables").get("opportunities_ranked"), "opportunities table")
        rows = list_value(table.get("rows"), "opportunities.rows")
        total = count_value(table.get("count"), "opportunities.count")
        if total < len(rows):
            raise ContractError("Loaded opportunities exceed the declared population")
        if status.get("ready") is not True:
            warnings.append("Opportunity read is not ready")
        states: dict[str, int] = {}
        for source in rows:
            row = object_value(source, "opportunity")
            state = row.get("presentation_state")
            if state not in {"paper_review", "review", "watch", "research", "blocked"}:
                raise ContractError("Opportunity presentation state is missing or invalid")
            states[state] = states.get(state, 0) + 1
            if row.get("presentation_blocker") and (state != "blocked" or not row.get("presentation_next_action")):
                raise ContractError("Expired or inconsistent plan must be blocked with a next step")
            if state in {"paper_review", "review"}:
                plan = object_value(row.get("trade_plan"), "opportunity trade plan")
                if plan.get("eligibility") != "ACTIONABLE":
                    raise ContractError("Published-terms state lacks actionable published terms")
        evidence.update(loaded_count=len(rows), total_count=total, state_counts=states)
    elif name == "paper_trades":
        rows = list_value(payload.get("rows"), "paper trades")
        verified = 0
        for source in rows:
            row = object_value(source, "paper trade")
            if row.get("mark_status") != "verified" or row.get("mark_stale"):
                if row.get("remaining_quantity") and row.get("unrealized_pnl") is not None:
                    raise ContractError("Unverified open mark must not claim unrealized P&L")
                continue
            # Age alone is not failure: the last completed-session mark can be
            # valid on a weekend. Verify causal source clocks, never redate them.
            observed = datetime.fromisoformat(str(row.get("mark_observed_at")).replace("Z", "+00:00"))
            available = datetime.fromisoformat(str(row.get("mark_available_at")).replace("Z", "+00:00"))
            if observed.tzinfo is None or available.tzinfo is None or not observed <= available <= datetime.now(UTC):
                raise ContractError("Verified valuation mark has missing or inconsistent clocks")
            verified += 1
        evidence.update(loaded_order_count=len(rows), verified_mark_count=verified)
    elif name == "paper":
        counts = object_value(payload.get("counts"), "paper.counts")
        total = count_value(counts.get("total_orders"), "paper.total_orders")
        filled = count_value(counts.get("filled_orders"), "paper.filled_orders")
        if filled > total:
            raise ContractError("Filled order count exceeds the total")
        account = object_value(payload.get("account"), "paper.account")
        if account.get("paper_only") is not True:
            raise ContractError("Account must explicitly be paper-only")
        evidence.update(order_count=total, filled_count=filled, account_status=account.get("status"))
        if account.get("status") == "complete":
            values = [account.get(key) for key in ("nav", "opening_cash", "net_pnl")]
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) for value in values):
                raise ContractError("A complete account requires finite accounting values")
            nav, opening, pnl = map(lambda value: Decimal(str(value)), values)
            if abs(nav - opening - pnl) > Decimal("0.011"):
                raise ContractError("Whole-account NAV, opening cash and P&L do not reconcile")
        else:
            warnings.append("Paper funding/account evidence needs local review; the script does not fund or repair it")
        # Zero orders/fills is not itself failure, and a profitable result is not required.
    elif name == "nav":
        if payload.get("paper_only") is not True or payload.get("book") != "paper":
            raise ContractError("NAV response has the wrong account scope")
        points = list_value(payload.get("points"), "nav.points")
        previous = None
        gap_count = 0
        for source in points:
            point = object_value(source, "nav point")
            at = datetime.fromisoformat(str(point.get("at")).replace("Z", "+00:00"))
            if at.tzinfo is None or previous is not None and at <= previous:
                raise ContractError("NAV observations must have strictly increasing aware timestamps")
            previous = at
            if point.get("status") == "complete":
                value = point.get("nav")
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                    raise ContractError("Complete NAV point has no finite observed value")
            else:
                gap_count += 1
                if point.get("nav") is not None:
                    raise ContractError("Incomplete NAV evidence must be a gap, not a price")
        evidence.update(point_count=len(points), gap_count=gap_count)
        if gap_count:
            warnings.append("Recorded NAV contains explicit evidence gaps; reconcile marks locally")
    elif name == "learning":
        if payload.get("paper_only") is not True:
            raise ContractError("Learning overview must declare paper-only scope")
        strategy = object_value(payload.get("strategy_lane"), "learning.strategy_lane")
        prediction = object_value(payload.get("prediction_lane"), "learning.prediction_lane")
        evidence.update(strategy_status=strategy.get("status"), prediction_status=prediction.get("status"))
        if strategy.get("status") in {"misconfigured", "failed", "unavailable"}:
            warnings.append("Strategy evidence collection needs local remediation")
    else:
        raise ValueError("Unknown check")
    if warnings:
        result["status"] = "needs_attention"
    return result


def _check_endpoint(name: str, endpoint: str, base_url: str, timeout: float, *, expected_commit: str | None,
                    expected_schema: str | None, attempt: int | None = None) -> tuple[dict[str, Any], str | None]:
    payload: dict[str, Any] | None = None
    try:
        payload = read_json(base_url, endpoint, timeout)
        check = assess(name, payload, expected_commit=expected_commit, expected_schema=expected_schema)
    except HTTPError as error:
        check = {"check": name, "status": "failed", "error": f"HTTP {error.code}; inspect local server logs"}
    except (URLError, OSError, ValueError, TypeError) as error:
        # Do not publish response bodies or raw exception strings: they can
        # contain account data, SQL, URLs or credentials from a local server.
        check = {"check": name, "status": "failed", "error": type(error).__name__ + "; inspect this endpoint locally"}
    return ({**check, "endpoint": endpoint, **({"attempt": attempt} if attempt is not None else {})},
            stability_digest(name, payload) if attempt is not None and payload is not None and check["status"] != "failed" else None)


def verify(base_url: str, *, timeout: float = 20, expected_commit: str | None = None,
           expected_schema: str | None = None, release_candidate: bool = False) -> dict[str, Any]:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Use an http(s) server origin without credentials, query or path")
    if not isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("Timeout must be between zero and 60 seconds")
    endpoints = (RUNTIME_ENDPOINT,) if release_candidate else ENDPOINTS
    checks = []
    for name, endpoint in endpoints:
        check, _digest = _check_endpoint(name, endpoint, base_url, timeout, expected_commit=expected_commit,
                                         expected_schema=expected_schema)
        checks.append(check)
    previous_digests: dict[str, str] = {}
    for attempt in range(1, TODAY_STABILITY_ATTEMPTS + 1):
        for name, endpoint in TODAY_STABILITY_ENDPOINTS:
            check, digest = _check_endpoint(name, endpoint, base_url, timeout, expected_commit=expected_commit,
                                            expected_schema=expected_schema, attempt=attempt)
            if digest and name in previous_digests and digest != previous_digests[name]:
                check["status"] = "needs_attention"
                check.setdefault("warnings", []).append(f"Repeated {name} response changed during the smoke check")
            if digest:
                previous_digests[name] = digest
            checks.append(check)
    return {"checked_at": datetime.now(UTC).isoformat(), "read_only": True,
            "status": "failed" if any(row["status"] == "failed" for row in checks) else
                      "needs_attention" if any(row["status"] == "needs_attention" for row in checks) else "pass",
            "checks": checks,
            "limitations": "Separate read cutoffs; no end-to-end fill, browser, provider-entitlement or profitability verification."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-schema", default=HEAD_REVISION)
    parser.add_argument("--release-candidate", action="store_true", help="Check runtime identity and repeat the Today read paths after restart")
    parser.add_argument("--strict", action="store_true", help="Return nonzero for operational warnings as well as contract/transport failures")
    parser.add_argument("--output", type=Path, help="Optional local JSON report; raw financial response bodies are never written")
    args = parser.parse_args()
    try:
        report = verify(args.base_url, timeout=args.timeout, expected_commit=args.expected_commit,
                        expected_schema=args.expected_schema, release_candidate=args.release_candidate)
    except ValueError as error:
        parser.error(str(error))
    rendered = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 1 if report["status"] == "failed" or args.strict and report["status"] != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
