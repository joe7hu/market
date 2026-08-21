"""Fast static checks for the current Market architecture.

These checks do not import the application or contact a database. They protect
the seams that make the repository easy to navigate: typed owners, explicit
facades, valid entry points, and a clean PostgreSQL-only runtime.
"""

from __future__ import annotations

import ast
from contextlib import redirect_stdout
from io import StringIO
import json
import re
import tomllib
from pathlib import Path

from scripts.architecture_inventory import (
    KNOWN_COMPATIBILITY_ROUTES,
    compatibility_references,
    console_script_violations,
    local_import_cycles,
    production_private_imports,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROD_ROOTS = [REPO_ROOT / "app", REPO_ROOT / "src" / "investment_panel"]

KNOWN_CYCLE_COMPONENTS = frozenset()
KNOWN_PRIVATE_IMPORT_EDGES = frozenset()


def _prod_py_files() -> list[Path]:
    return sorted(
        path
        for root in PROD_ROOTS
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _all_py_files() -> list[Path]:
    return sorted(
        path
        for root in (*PROD_ROOTS, REPO_ROOT / "tests")
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_compact_inventory_is_complete() -> None:
    from scripts import architecture_inventory

    output = StringIO()
    with redirect_stdout(output):
        assert architecture_inventory.main() == 0
    lines = output.getvalue().splitlines()
    assert len(lines) < 200, f"Architecture inventory is too large: {len(lines)} lines"
    required_sections = {
        "production_lines:",
        "openapi_routes:",
        "owner_exports:",
        "local_import_cycles:",
        "private_cross_module_imports:",
        "router_database_violations:",
        "reexport_only_modules:",
        "console_entrypoints:",
        "generated_contracts:",
        "compatibility_references:",
    }
    assert required_sections <= set(lines)


def _private_import_edge(finding: str) -> str:
    relative, imported = finding.split(" ", 1)
    relative = relative.rsplit(":", 1)[0]
    return f"{relative} {imported.rsplit('.', 1)[0]}"


def test_import_cycles_are_ratchet_guarded() -> None:
    actual = {frozenset(cycle) for cycle in local_import_cycles()}
    unexpected = actual - KNOWN_CYCLE_COMPONENTS
    assert not unexpected, f"Local import cycles remain: {sorted(map(sorted, unexpected))}"


def test_private_cross_module_imports_are_ratchet_guarded() -> None:
    actual = {_private_import_edge(finding) for finding in production_private_imports()}
    unexpected = actual - KNOWN_PRIVATE_IMPORT_EDGES
    assert not unexpected, "Production private imports remain:\n  " + "\n  ".join(sorted(unexpected))


def test_tests_use_public_module_interfaces() -> None:
    violations = []
    for path in (REPO_ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            private = [alias.name for alias in node.names if alias.name.startswith("_")]
            if private:
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} imports {', '.join(private)}")
    assert not violations, "Tests import private module names:\n  " + "\n  ".join(violations)


def test_options_actions_uses_bounded_domain_owners() -> None:
    path = REPO_ROOT / "app" / "actions" / "options.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    domain_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("investment_panel.database.")
        and node.module != "investment_panel.database.authority"
    }
    assert domain_modules == {
        "investment_panel.database.options_decision_system",
        "investment_panel.database.options_execution",
        "investment_panel.database.options_history",
        "investment_panel.database.options_recovery_read",
        "investment_panel.database.options_research",
    }
    assert len(domain_modules) <= 5


def test_deep_owner_modules_have_explicit_exports() -> None:
    modules = (
        "src/investment_panel/core/event_scout.py",
        "src/investment_panel/core/event_scout_runtime.py",
        "src/investment_panel/database/options_decision_system.py",
        "src/investment_panel/database/options_execution.py",
        "src/investment_panel/database/options_history.py",
        "src/investment_panel/database/options_recovery_read.py",
        "src/investment_panel/database/options_research.py",
    )
    missing = [module for module in modules if "__all__" not in (REPO_ROOT / module).read_text(encoding="utf-8")]
    assert not missing, "Deep owner modules need explicit __all__: " + ", ".join(missing)


def _forbidden_runtime_tokens() -> tuple[str, ...]:
    # Keep the guard itself free of the exact retired product name. This makes
    # the same zero-code scan apply to all Python, package, and lock files.
    retired_store = "duck" + "db"
    return (
        retired_store,
        retired_store + "_path",
        "market_" + retired_store + "_path",
        "investment." + retired_store,
        "legacy_" + "import",
        "pandas-" + "datareader",
    )


def test_runtime_has_no_retired_storage_or_importer_markers() -> None:
    runtime_roots = (
        REPO_ROOT / "frontend" / "src",
        REPO_ROOT / "prompts",
        REPO_ROOT / "scripts",
    )
    runtime_files = [
        path
        for root in runtime_roots
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    files = [
        *_all_py_files(),
        *runtime_files,
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "uv.lock",
        REPO_ROOT / "package.json",
        REPO_ROOT / "package-lock.json",
        REPO_ROOT / "Makefile",
    ]
    violations = []
    tokens = _forbidden_runtime_tokens()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace").casefold()
        for token in tokens:
            if token.casefold() in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {token!r}")
    assert not violations, "Retired storage/importer markers remain:\n  " + "\n  ".join(violations)


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


def _module_path(module: str) -> Path | None:
    if module == "app" or module.startswith("app."):
        relative = Path(*module.split("."))
        root = REPO_ROOT
    elif module == "investment_panel" or module.startswith("investment_panel."):
        relative = Path(*module.split("."))
        root = REPO_ROOT / "src"
    else:
        return None
    package_path = root / relative / "__init__.py"
    module_path = (root / relative).with_suffix(".py")
    if package_path.exists():
        return package_path
    if module_path.exists():
        return module_path
    if (root / relative).is_dir():
        return root / relative
    return None


def test_local_imports_resolve_to_existing_modules() -> None:
    violations = []
    for path in _all_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for module in _imported_modules(tree):
            if (module_path := _module_path(module)) is None and (
                module == "app"
                or module.startswith("app.")
                or module == "investment_panel"
                or module.startswith("investment_panel.")
            ):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports missing {module}")
    assert not violations, "Imports point to deleted local modules:\n  " + "\n  ".join(violations)


FACADE_PACKAGES = (
    "investment_panel.core.panel",
    "investment_panel.core.decision",
    "investment_panel.core.brokers",
)


def _facade_dir(dotted: str) -> Path:
    root = REPO_ROOT / "src" if dotted.startswith("investment_panel.") else REPO_ROOT
    return root / Path(*dotted.split("."))


def test_external_code_imports_facade_not_submodules() -> None:
    violations = []
    for path in _prod_py_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=relative)
        for module in _imported_modules(tree):
            for facade in FACADE_PACKAGES:
                if not module.startswith(facade + "."):
                    continue
                if _facade_dir(facade) in path.parents:
                    continue
                violations.append(f"{relative} imports {module}")
    assert not violations, "External code reaches into facade internals:\n  " + "\n  ".join(violations)


def test_public_facades_are_explicit() -> None:
    violations = []
    for facade in (*FACADE_PACKAGES, "app.data_access"):
        path = _facade_dir(facade) / "__init__.py"
        text = path.read_text(encoding="utf-8", errors="replace")
        if "__all__" not in text:
            violations.append(f"{path.relative_to(REPO_ROOT)} has no explicit __all__")
        if "__getattr__" in text or "import_module" in text:
            violations.append(f"{path.relative_to(REPO_ROOT)} uses dynamic compatibility loading")
    assert not violations, "Facade contract violations:\n  " + "\n  ".join(violations)


def test_application_seams_are_static_and_split() -> None:
    expected = {
        "dependencies.py",
        "panel_snapshot.py",
        "job_control.py",
        "request_security.py",
    }
    seam_dir = REPO_ROOT / "app"
    assert {path.name for path in seam_dir.glob("*.py")} >= expected
    assert not (seam_dir / "deps.py").exists()
    assert not (seam_dir / "panel_contracts.py").exists()


def test_configuration_has_one_typed_owner_and_no_raw_config_parameters() -> None:
    config_owners: list[Path] = []
    raw_parameters: list[str] = []
    for path in _prod_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "load_config":
                config_owners.append(path)
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if argument.arg != "config" or argument.annotation is None:
                    continue
                annotation = ast.unparse(argument.annotation)
                if any(token in annotation for token in ("Any", "dict", "Mapping")):
                    raw_parameters.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} config: {annotation}"
                    )

    assert config_owners == [REPO_ROOT / "src" / "investment_panel" / "core" / "config.py"]
    assert not raw_parameters, "Application owners must accept typed configuration:\n  " + "\n  ".join(raw_parameters)
    assert not (REPO_ROOT / "app" / "data_access" / "config.py").exists()
    assert not (REPO_ROOT / "src" / "investment_panel" / "core" / "retention.py").exists()


def test_advisory_provider_and_option_agent_are_single_typed_seams() -> None:
    old_modules = (
        REPO_ROOT / "src" / "investment_panel" / "jobs" / "provider_request.py",
        REPO_ROOT / "src" / "investment_panel" / "jobs" / "openai_option_agent.py",
        REPO_ROOT / "src" / "investment_panel" / "jobs" / "deepseek_option_agent.py",
        REPO_ROOT / "src" / "investment_panel" / "jobs" / "openai_option_agent_auth.py",
    )
    assert not [path.relative_to(REPO_ROOT) for path in old_modules if path.exists()]

    production_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in _prod_py_files()
    )
    assert "meta_sink" not in production_text

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject.get("project", {}).get("scripts", {})
    retired_scripts = {
        "market-openai-option-thesis-agent",
        "market-codex-option-thesis-agent",
        "market-openai-option-postmortem-agent",
        "market-codex-option-postmortem-agent",
        "market-openai-option-agent",
        "market-codex-option-agent",
        "market-deepseek-option-thesis-agent",
        "market-deepseek-option-postmortem-agent",
        "market-deepseek-option-agent",
    }
    assert not retired_scripts & set(scripts)
    assert [name for name in scripts if "option-agent" in name] == ["market-run-option-agent"]
    assert scripts.get("market-run-option-agent") == "investment_panel.jobs.run_option_agent:main"

    from investment_panel.core.agent_providers import provider_catalog, resolve_provider_selection

    catalog = provider_catalog()
    assert set(catalog) == {"codex", "deepseek"}
    assert all(resolve_provider_selection(name).command == "market-run-option-agent" for name in catalog)


def test_known_compatibility_files_and_routes_are_closed_sets() -> None:
    observed_files = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in _prod_py_files()
        if path.name in {"deps.py", "panel_contracts.py"}
        or any(token in path.name.casefold() for token in ("legacy", "compat", "deprecated"))
    }
    assert not observed_files

    openapi = REPO_ROOT / "frontend" / "src" / "generated" / "openapi.json"
    routes = set(__import__("json").loads(openapi.read_text(encoding="utf-8"))["paths"])
    observed_routes = {
        route
        for route in routes
        if any(token in route.casefold() for token in ("legacy", "compat", "deprecated", "watchlist-screen", "etf-premiums", "tradingview-chart-state", "decision-truth"))
    }
    assert not observed_routes
    assert not {marker: count for marker, count in compatibility_references().items() if count}


def test_route_manifest_matches_generated_openapi() -> None:
    import json

    manifest = json.loads((REPO_ROOT / "docs" / "api-route-manifest.json").read_text(encoding="utf-8"))
    openapi = json.loads(
        (REPO_ROOT / "frontend" / "src" / "generated" / "openapi.json").read_text(encoding="utf-8")
    )
    actual = {
        path: sorted(method.upper() for method in operations if method in {"get", "post", "put", "patch", "delete"})
        for path, operations in openapi["paths"].items()
    }
    assert actual == manifest


def test_retained_routes_have_explicit_router_owners() -> None:
    import json

    from fastapi.routing import APIRoute
    from app.main import app

    manifest = json.loads((REPO_ROOT / "docs" / "api-route-manifest.json").read_text(encoding="utf-8"))
    manifest_routes = {
        (path, method)
        for path, methods in manifest.items()
        for method in methods
    }
    observed: dict[tuple[str, str], str] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        for method in route.methods or ():
            if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                observed[(route.path, method)] = route.endpoint.__module__

    assert set(observed) == manifest_routes
    assert all(module.startswith("app.routers.") for module in observed.values())


def test_generated_contracts_are_current() -> None:
    from scripts.generate_openapi import rendered_schema
    from scripts.generate_panel_contract import rendered_contract

    openapi = REPO_ROOT / "frontend" / "src" / "generated" / "openapi.json"
    panel_contract = REPO_ROOT / "frontend" / "src" / "generated" / "panelContract.ts"
    assert openapi.read_text(encoding="utf-8") == rendered_schema()
    assert panel_contract.read_text(encoding="utf-8") == rendered_contract()


def test_frontend_domain_responses_are_named_and_api_modules_are_local() -> None:
    openapi = json.loads(
        (REPO_ROOT / "frontend" / "src" / "generated" / "openapi.json").read_text(encoding="utf-8")
    )
    schema_names = set(openapi["components"]["schemas"])
    frontend_root = REPO_ROOT / "frontend" / "src"
    assert not (frontend_root / "api.ts").exists()

    broad_imports: list[str] = []
    duplicate_types: list[str] = []
    declaration = re.compile(r"^\s*export\s+(?:type|interface)\s+([A-Za-z0-9_]+)")
    for path in sorted(frontend_root.rglob("*.ts")) + sorted(frontend_root.rglob("*.tsx")):
        if "generated" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"from\s+[\"'](?:@/api|\.\.?/api)[\"']", text):
            broad_imports.append(path.relative_to(REPO_ROOT).as_posix())
        for line in text.splitlines():
            match = declaration.search(line)
            if match and match.group(1) in schema_names and "ApiSchema[" not in line and "components[" not in line:
                duplicate_types.append(f"{path.relative_to(REPO_ROOT)}: {match.group(1)}")

    assert not broad_imports, "Frontend imports the deleted broad API module:\n  " + "\n  ".join(broad_imports)
    assert not duplicate_types, "Handwritten frontend types duplicate named OpenAPI schemas:\n  " + "\n  ".join(duplicate_types)


def test_shallow_postgres_reexport_modules_are_removed() -> None:
    removed = (
        "postgres_panel.py",
        "postgres_queries.py",
        "postgres_source_queries.py",
        "postgres_watchlist.py",
    )
    assert not [name for name in removed if (REPO_ROOT / "app" / "data_access" / name).exists()]


def test_live_catalog_writes_use_instrument_owner() -> None:
    violations = []
    owner = REPO_ROOT / "src" / "investment_panel" / "database" / "instruments.py"
    for path in _prod_py_files():
        if path == owner:
            continue
        if "INSERT INTO catalog.instrument" in path.read_text(encoding="utf-8", errors="replace"):
            violations.append(str(path.relative_to(REPO_ROOT)))
    assert not violations, "Live catalog writes must use database.instruments:\n  " + "\n  ".join(violations)


def test_ingestion_clients_use_managed_run_lifecycle() -> None:
    violations = []
    for path in _prod_py_files():
        if path.name == "ingestion.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        repository_names = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "IngestionRepository"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if isinstance(node.func.value, ast.Name) and node.func.value.id in repository_names:
                if node.func.attr in {"start_run", "finish_run"}:
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not violations, "Ingestion clients must use IngestionRepository.run:\n  " + "\n  ".join(violations)


def test_http_routers_do_not_construct_database_repositories() -> None:
    violations = []
    for path in (REPO_ROOT / "app" / "routers").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "investment_panel.database"
            ):
                violations.append(f"{path.name}:{node.lineno} imports {node.module}")
    assert not violations, "Routers must call application owners, not database adapters:\n  " + "\n  ".join(violations)


def test_console_scripts_target_existing_functions() -> None:
    assert not console_script_violations()
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject.get("project", {}).get("scripts", {})
    violations = []
    for name, target in scripts.items():
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
    assert not violations, "Console scripts have no valid owner:\n  " + "\n  ".join(violations)
