"""Print a compact, static architecture inventory for agent navigation.

The inventory is deliberately small. It reports shape and ownership, while the
architecture tests turn the important rules into ratchets. It does not import
the application or contact PostgreSQL.
"""

from __future__ import annotations

import ast
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import tomllib
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROD_ROOTS = (ROOT / "app", ROOT / "src" / "investment_panel")
FRONTEND_SRC = ROOT / "frontend" / "src"
OWNER_MODULES = (
    "app/dependencies.py",
    "app/panel_snapshot.py",
    "app/job_control.py",
    "app/request_security.py",
    "app/actions/options.py",
    "app/actions/event_scout.py",
    "src/investment_panel/database/panel_models.py",
    "src/investment_panel/database/panel_queries.py",
    "src/investment_panel/core/event_scout.py",
    "src/investment_panel/core/event_scout_runtime.py",
    "src/investment_panel/database/event_scout.py",
    "src/investment_panel/database/options_history.py",
    "src/investment_panel/database/options_decision_system.py",
    "src/investment_panel/database/options_research.py",
    "src/investment_panel/database/options_execution.py",
    "src/investment_panel/database/options_recovery_read.py",
    "src/investment_panel/database/options_publication.py",
    "src/investment_panel/database/ingestion.py",
    "frontend/src/generated/apiSchema.ts",
)
AREAS = ("api", "config", "options", "providers", "frontend")
KNOWN_COMPATIBILITY_FILES = frozenset({"app/deps.py", "app/panel_contracts.py"})
KNOWN_COMPATIBILITY_ROUTES = frozenset({
    "/api/decision-truth",
    "/api/etf-premiums",
    "/api/options/history/surface/legacy",
    "/api/tradingview-chart-state",
    "/api/watchlist-screen",
})
COMPATIBILITY_MARKERS = (
    "surface/legacy",
    "tradingview_chart_state",
    "etf_premiums",
    "watchlist-screen",
    "panel_contracts",
    "app.deps",
)
FINAL_ARCHITECTURE_INVARIANTS = {
    "availability_authority": "raw.price_bar_fact_availability + raw.quote_fact_availability",
    "implemented_storage_phases": "plan,archive fundamental-history|options,verify,restore,compact price-confirmations",
    "scheduler_concurrency": "2",
    "option_hot_retention_days": "7",
    "option_archive_retention_days": "730",
    "derived_retention_days": "30",
    "forbidden_retired_markers": "TradingViewProvider, data_sources.tradingview, current-price legacy fallback",
}
RETIRED_MARKERS = (
    "TradingViewProvider",
    "data_sources.tradingview",
    "config.data_sources.tradingview",
)
FORBIDDEN_TOKENS = ("duck" + "db", "duck" + "db_path", "pandas-" + "datareader")


def _prod_py_files() -> list[Path]:
    return sorted(
        path
        for root in PROD_ROOTS
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _module_name(path: Path) -> str:
    if path.is_relative_to(ROOT / "app"):
        relative = path.relative_to(ROOT).with_suffix("")
    else:
        relative = path.relative_to(ROOT / "src").with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _local_module_paths() -> dict[str, Path]:
    return {_module_name(path): path for path in _prod_py_files()}


def _relative_import_module(current: str, path: Path, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    package = current.split(".")
    if path.name != "__init__.py":
        package.pop()
    if node.level > 1:
        package = package[: -(node.level - 1)]
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package)


def _dependency_targets(path: Path, tree: ast.AST, local_modules: set[str]) -> set[str]:
    """Return local modules imported anywhere in a production module."""

    current = _module_name(path)
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _relative_import_module(current, path, node)
            candidates = [base] if base else []
            if base:
                candidates.extend(f"{base}.{alias.name}" for alias in node.names)
        else:
            continue
        targets.update(candidate for candidate in candidates if candidate in local_modules and candidate != current)
    return targets


def local_import_graph() -> dict[str, set[str]]:
    local_modules = set(_local_module_paths())
    graph: dict[str, set[str]] = {}
    for path in _prod_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        graph[_module_name(path)] = _dependency_targets(path, tree, local_modules)
    return graph


def local_import_cycles() -> list[tuple[str, ...]]:
    """Return strongly connected production-module groups."""

    graph = local_import_graph()
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(graph.get(node, ())):
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] != indexes[node]:
            return
        component: list[str] = []
        while True:
            target = stack.pop()
            on_stack.remove(target)
            component.append(target)
            if target == node:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for node in sorted(graph):
        if node not in indexes:
            visit(node)
    return sorted(components)


def production_private_imports() -> list[str]:
    """List cross-module imports of private names for the architecture ratchet."""

    local_modules = set(_local_module_paths())
    findings: list[str] = []
    for path in _prod_py_files():
        current = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            target = _relative_import_module(current, path, node)
            if target not in local_modules:
                continue
            for alias in node.names:
                if alias.name.startswith("_") and target != current:
                    findings.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno} {target}.{alias.name}"
                    )
    return sorted(findings)


def router_database_imports() -> list[str]:
    findings: list[str] = []
    for path in sorted((ROOT / "app" / "routers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("investment_panel.database"):
                findings.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno} {node.module}")
    return findings


def _literal_all(path: Path) -> tuple[str, int]:
    if path.suffix != ".py":
        return "generated", 0
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            return "implicit", 0
        return "explicit", len(value) if isinstance(value, (list, tuple, set)) else 0
    return "missing", 0


def _relative_source_paths() -> list[Path]:
    return [
        *_prod_py_files(),
        *(
            path
            for path in FRONTEND_SRC.rglob("*")
            if path.is_file()
            and path.suffix in {".ts", ".tsx"}
            and "generated" not in path.parts
            and ".test." not in path.name
            and ".spec." not in path.name
        ),
    ]


def _area_matches(path: Path, area: str | None) -> bool:
    if area is None:
        return True
    relative = path.relative_to(ROOT).as_posix()
    if area == "api":
        return relative.startswith((
            "app/",
            "src/investment_panel/core/panel/",
            "src/investment_panel/database/panel",
            "src/investment_panel/database/current_quotes.py",
            "src/investment_panel/database/sources.py",
            "src/investment_panel/database/superinvestor_portfolios.py",
            "frontend/src/api/",
            "frontend/src/apiTransport.ts",
        ))
    if area == "config":
        return relative.startswith((
            "app/dependencies.py",
            "app/response_contracts.py",
            "app/routers/settings.py",
            "app/data_access/settings.py",
            "src/investment_panel/core/config",
            "src/investment_panel/core/agent_config.py",
            "src/investment_panel/core/options_recovery_config.py",
            "src/investment_panel/database/configuration.py",
        ))
    if area == "options":
        return (
            relative.startswith(("app/actions/options.py", "app/routers/options"))
            or "/options" in relative
            or "/option_" in relative
            or relative.startswith(("frontend/src/api/options.ts", "frontend/src/views/options"))
        )
    if area == "providers":
        return relative.startswith((
            "src/investment_panel/providers/",
            "src/investment_panel/core/agent_providers.py",
            "src/investment_panel/jobs/run_option_agent.py",
            "src/investment_panel/jobs/option_agent_workflow.py",
            "src/investment_panel/jobs/run_option_recovery_agents.py",
            "frontend/src/api/agent.ts",
        ))
    if area == "frontend":
        return relative.startswith("frontend/src/") and "generated" not in relative and ".test." not in relative and ".spec." not in relative
    raise ValueError(f"unknown architecture area: {area}")


def _area_source_paths(area: str | None) -> list[Path]:
    return [path for path in _relative_source_paths() if _area_matches(path, area)]


def owner_exports(area: str | None = None) -> list[tuple[str, str, int]]:
    result: list[tuple[str, str, int]] = []
    for relative in OWNER_MODULES:
        path = ROOT / relative
        if area is not None and not _area_matches(path, area):
            continue
        kind, count = _literal_all(path) if path.exists() else ("missing-file", 0)
        result.append((relative, kind, count))
    return result


def reexport_only_modules() -> list[str]:
    result: list[str] = []
    for path in _prod_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        meaningful: list[ast.AST] = []
        imports = False
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports = True
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
                    continue
            meaningful.append(node)
        if imports and not meaningful:
            result.append(path.relative_to(ROOT).as_posix())
    return sorted(result)


def _scripts() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return sorted(config.get("project", {}).get("scripts", {}))


def _module_path(module: str) -> Path | None:
    if module == "app" or module.startswith("app."):
        relative = Path(*module.split("."))
        root = ROOT
    elif module == "investment_panel" or module.startswith("investment_panel."):
        relative = Path(*module.split("."))
        root = ROOT / "src"
    else:
        return None
    package_path = root / relative / "__init__.py"
    module_path = (root / relative).with_suffix(".py")
    if package_path.exists():
        return package_path
    if module_path.exists():
        return module_path
    return None


def console_script_violations() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    violations: list[str] = []
    for name, target in config.get("project", {}).get("scripts", {}).items():
        try:
            module_name, function_name = target.split(":", 1)
        except ValueError:
            violations.append(f"{name}: malformed target {target!r}")
            continue
        module_path = _module_path(module_name)
        if module_path is None:
            violations.append(f"{name}: missing module {module_name}")
            continue
        tree = ast.parse(module_path.read_text(encoding="utf-8", errors="replace"), filename=str(module_path))
        functions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if function_name not in functions:
            violations.append(f"{name}: {module_name}:{function_name} is not defined")
    return violations


def _route_category(path: str) -> str:
    if path in KNOWN_COMPATIBILITY_ROUTES:
        return "compatibility"
    if path == "/api/status" or path.startswith("/api/health") or path.startswith("/api/refresh-jobs"):
        return "health/jobs"
    if path.startswith("/api/panel") or path in {"/api/dashboard", "/api/signals"}:
        return "read-model"
    if "/options" in path or "option-" in path or path.startswith("/api/paper-orders"):
        return "options"
    if "portfolio" in path or "watchlist" in path:
        return "portfolio"
    if "agent" in path:
        return "agent"
    if "source" in path or "disclos" in path or "superinvestor" in path:
        return "sources"
    if "thesis" in path or "catalyst" in path or "trader-twin" in path:
        return "thesis"
    if "ticker" in path:
        return "ticker"
    if "broker" in path:
        return "broker"
    if "event" in path:
        return "events"
    if "setting" in path:
        return "settings"
    return "other"


def route_inventory() -> tuple[int, int, Counter[str]]:
    path = FRONTEND_SRC / "generated" / "openapi.json"
    if not path.exists():
        return 0, 0, Counter()
    contract = json.loads(path.read_text(encoding="utf-8"))
    paths = contract.get("paths", {})
    operations = sum(
        1
        for item in paths.values()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    )
    return len(paths), operations, Counter(_route_category(route) for route in paths)


def compatibility_references() -> dict[str, int]:
    counts: dict[str, int] = {}
    scan_roots = (*PROD_ROOTS, FRONTEND_SRC)
    for marker in COMPATIBILITY_MARKERS:
        count = 0
        for root in scan_roots:
            for path in root.rglob("*"):
                if (
                    path.is_file()
                    and "__pycache__" not in path.parts
                    and path.suffix in {".py", ".ts", ".tsx"}
                    and marker.casefold() in path.read_text(encoding="utf-8", errors="replace").casefold()
                ):
                    count += 1
        counts[marker] = count
    return counts


def final_architecture_inventory() -> dict[str, Any]:
    """Return the final lifecycle contracts for human and CI inspection."""

    counts = {marker: 0 for marker in RETIRED_MARKERS}
    for root in (ROOT / "app", ROOT / "src", ROOT / "config.yaml"):
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix not in {".py", ".yaml", ".yml", ".ts", ".tsx"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for marker in counts:
                counts[marker] += int(marker.casefold() in text.casefold())
    current_selector = (ROOT / "migrations" / "versions" / "20260821_0043_final_architecture_scale.py").read_text(
        encoding="utf-8", errors="replace"
    )
    return {
        **FINAL_ARCHITECTURE_INVARIANTS,
        "availability_cutover_migration": "20260821_0043" in current_selector,
        "current_price_projection_only": "include_legacy_fallback=False" in current_selector,
        "retired_marker_counts": counts,
    }


def production_lines_by_subsystem(area: str | None = None) -> dict[str, int]:
    totals: Counter[str] = Counter()
    paths = _area_source_paths(area)
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("frontend/"):
            subsystem = "frontend"
        elif relative.startswith("app/routers/"):
            subsystem = "app/routers"
        elif relative.startswith("app/actions/"):
            subsystem = "app/actions"
        elif relative.startswith("app/"):
            subsystem = "app"
        elif "/database/" in relative:
            subsystem = "database"
        elif "/jobs/" in relative:
            subsystem = "jobs"
        elif "/analysis/" in relative:
            subsystem = "analysis"
        elif "/providers/" in relative:
            subsystem = "providers"
        else:
            subsystem = "core"
        totals[subsystem] += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return dict(sorted(totals.items()))


def _missing_local_imports() -> list[str]:
    missing: list[str] = []
    local_modules = set(_local_module_paths())
    for path in _prod_py_files():
        current = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = _relative_import_module(current, path, node)
                candidates = [module] if module else []
            elif isinstance(node, ast.Import):
                candidates = [alias.name for alias in node.names]
            else:
                continue
            for module in candidates:
                if not module or not module.startswith(("app", "investment_panel")) or module in local_modules:
                    continue
                roots = (ROOT, ROOT / "src") if module.startswith("app") else (ROOT / "src",)
                relative = Path(*module.split("."))
                exists_as_namespace = any((root / relative).is_dir() for root in roots)
                if not exists_as_namespace:
                    missing.append(f"{path.relative_to(ROOT)} -> {module}")
    return sorted(set(missing))


def _print_area_inventory(area: str) -> int:
    print(f"area: {area}")
    print("production_lines:")
    for subsystem, count in production_lines_by_subsystem(area).items():
        print(f"  {subsystem}: {count}")
    print("owner_exports:")
    for relative, kind, count in owner_exports(area):
        print(f"  {relative}: {kind} ({count})")
    print("local_import_cycles:")
    cycles = local_import_cycles()
    for cycle in cycles:
        print("  " + " -> ".join(cycle))
    if not cycles:
        print("  none")
    print("private_cross_module_imports:")
    private = production_private_imports()
    for finding in private:
        print(f"  {finding}")
    if not private:
        print("  none")
    print("router_database_violations:")
    router_violations = router_database_imports()
    for finding in router_violations:
        print(f"  {finding}")
    if not router_violations:
        print("  none")
    failures = _missing_local_imports()
    failures.extend(console_script_violations())
    print("static_failures:")
    print("  PASS" if not failures else "  FAIL")
    for failure in failures:
        print(f"  {failure}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", choices=AREAS, help="print a compact inventory for one architecture area")
    args = parser.parse_args([] if argv is None else argv)
    if args.area:
        return _print_area_inventory(args.area)
    paths, operations, categories = route_inventory()
    print("production_lines:")
    for subsystem, count in production_lines_by_subsystem().items():
        print(f"  {subsystem}: {count}")
    print("python_modules:")
    print(f"  {len(_prod_py_files())}")
    print("openapi_routes:")
    print(f"  paths: {paths}")
    print(f"  operations: {operations}")
    for category, count in sorted(categories.items()):
        print(f"  category.{category}: {count}")
    print("owner_exports:")
    for relative, kind, count in owner_exports():
        print(f"  {relative}: {kind} ({count})")
    print("local_import_cycles:")
    cycles = local_import_cycles()
    for cycle in cycles:
        print("  " + " -> ".join(cycle))
    if not cycles:
        print("  none")
    print("private_cross_module_imports:")
    private = production_private_imports()
    for finding in private:
        print(f"  {finding}")
    if not private:
        print("  none")
    print("router_database_violations:")
    router_violations = router_database_imports()
    for finding in router_violations:
        print(f"  {finding}")
    if not router_violations:
        print("  none")
    print("reexport_only_modules:")
    for relative in reexport_only_modules():
        print(f"  {relative}")
    print("console_entrypoints:")
    for name in _scripts():
        print(f"  {name}")
    print("generated_contracts:")
    for relative in ("frontend/src/generated/openapi.json", "frontend/src/generated/apiSchema.ts"):
        path = ROOT / relative
        print(f"  {relative}: {'present' if path.exists() else 'missing'}")
    print("compatibility_references:")
    print(f"  known_files: {sum((ROOT / relative).exists() for relative in KNOWN_COMPATIBILITY_FILES)}")
    contract_path = FRONTEND_SRC / "generated" / "openapi.json"
    contract_paths = json.loads(contract_path.read_text(encoding="utf-8")).get("paths", {}) if contract_path.exists() else {}
    print(f"  known_routes: {sum(route in KNOWN_COMPATIBILITY_ROUTES for route in contract_paths)}")
    for marker, count in compatibility_references().items():
        print(f"  marker.{marker}: {count}")
    print("final_architecture:")
    for key, value in final_architecture_inventory().items():
        print(f"  {key}: {value}")

    failures = _missing_local_imports()
    failures.extend(console_script_violations())
    print("static_failures:")
    print("  PASS" if not failures else "  FAIL")
    for failure in failures:
        print(f"  {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
