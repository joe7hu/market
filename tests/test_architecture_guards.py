"""Fast static checks for the current Market architecture.

These checks do not import the application or contact a database. They protect
the seams that make the repository easy to navigate: typed owners, explicit
facades, valid entry points, and a clean PostgreSQL-only runtime.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROD_ROOTS = [REPO_ROOT / "app", REPO_ROOT / "src" / "investment_panel"]
MAX_LINES = 700


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


def test_no_module_exceeds_line_budget() -> None:
    offenders = []
    for path in _prod_py_files():
        line_count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        if line_count > MAX_LINES:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {line_count} lines")
    assert not offenders, (
        f"Modules over the line budget ({MAX_LINES}):\n  " + "\n  ".join(offenders)
    )


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
    files = [*_all_py_files(), REPO_ROOT / "pyproject.toml", REPO_ROOT / "uv.lock"]
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
    return REPO_ROOT / "src" / Path(*dotted.split("."))


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
    for facade in FACADE_PACKAGES:
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
    deps_text = (seam_dir / "deps.py").read_text(encoding="utf-8")
    assert "__getattr__" not in deps_text
    assert "import_module" not in deps_text
    assert "__all__" in deps_text


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
