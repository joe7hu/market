"""Print a compact architecture inventory without emitting an import graph."""

from __future__ import annotations

import ast
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
OWNER_MODULES = (
    "app/dependencies.py",
    "app/panel_snapshot.py",
    "app/job_control.py",
    "app/request_security.py",
    "app/actions/options.py",
    "app/actions/event_scout.py",
    "src/investment_panel/database/panel_models.py",
    "src/investment_panel/database/panel_queries.py",
    "src/investment_panel/database/event_scout.py",
    "src/investment_panel/database/options_decision_system.py",
    "src/investment_panel/database/ingestion.py",
    "frontend/src/generated/apiSchema.ts",
)
FORBIDDEN_TOKENS = ("duck" + "db", "duck" + "db_path", "pandas-" + "datareader")


def _scripts() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return sorted(config.get("project", {}).get("scripts", {}))


def _missing_local_imports() -> list[str]:
    missing: list[str] = []
    for scan_root in (ROOT / "app", ROOT / "src" / "investment_panel"):
        for path in scan_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.level != 0 or not node.module:
                    continue
                if not (node.module.startswith("app") or node.module.startswith("investment_panel")):
                    continue
                dotted = ROOT / ("src" if node.module.startswith("investment_panel") else "") / Path(*node.module.split("."))
                if not ((dotted.with_suffix(".py")).exists() or (dotted / "__init__.py").exists() or dotted.is_dir()):
                    missing.append(f"{path.relative_to(ROOT)} -> {node.module}")
    return missing


def main() -> int:
    print("entrypoints:")
    for name in _scripts():
        print(f"  {name}")
    print("owner_modules:")
    for relative in OWNER_MODULES:
        print(f"  {relative}")

    failures: list[str] = []
    scan_roots = (
        ROOT / "app",
        ROOT / "src",
        ROOT / "tests",
        ROOT / "scripts",
        ROOT / "frontend" / "src",
        ROOT / "prompts",
    )
    for root in scan_roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".md", ".py", ".toml", ".lock", ".json", ".ts", ".tsx", ".sh"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").casefold()
            for token in FORBIDDEN_TOKENS:
                if token.casefold() in text:
                    failures.append(f"{path.relative_to(ROOT)} contains {token}")
    failures.extend(_missing_local_imports())
    print("forbidden_dependency_checks:")
    print("  " + ("PASS" if not failures else "FAIL"))
    if failures:
        print("failures:")
        for failure in failures[:20]:
            print(f"  {failure}")
        if len(failures) > 20:
            print(f"  ... {len(failures) - 20} more")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
