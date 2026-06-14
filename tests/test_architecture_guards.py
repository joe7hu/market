"""Architecture guardrails — turn the ARCHITECTURE.md conventions into tests.

These are pure-AST/static checks: no DuckDB, no network, no app import, so they
run in milliseconds and never touch the shared write lock. They exist to stop the
two conventions that historically rotted (god-modules regrowing, and external code
reaching past a facade into its submodules) from silently coming back.

If a check here fails, the fix is almost always to honor the convention — not to
edit the allowlist. Only add an allowlist entry for a *deliberate, documented*
exception, with a reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROD_ROOTS = [REPO_ROOT / "app", REPO_ROOT / "src" / "investment_panel"]

# --- Module-size guard ------------------------------------------------------

# ARCHITECTURE.md: keep submodules scannable, target < ~700 lines. We hard-fail
# at this limit so a regrowing monolith trips the build instead of a reviewer.
MAX_LINES = 700

# Deliberate exceptions. path (relative to repo root) -> reason. Keep this short;
# every entry is a small debt you have chosen to carry.
SIZE_ALLOWLIST = {
    "src/investment_panel/core/schema.py": (
        "Single DDL string by design — see docs/schema-ddl-architecture-decision.md"
    ),
    "src/investment_panel/core/source_ingestion/canonical.py": (
        "769 lines, marginally over — split candidate, not a true monolith"
    ),
}


def _prod_py_files() -> list[Path]:
    files: list[Path] = []
    for root in PROD_ROOTS:
        for p in root.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            files.append(p)
    return files


def test_no_module_exceeds_line_budget() -> None:
    offenders = []
    for path in _prod_py_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        n = sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
        if n > MAX_LINES and rel not in SIZE_ALLOWLIST:
            offenders.append(f"{rel}: {n} lines (limit {MAX_LINES})")
    assert not offenders, (
        "Modules over the line budget — extract a responsibility submodule and "
        "re-export from the facade (see ARCHITECTURE.md), or add a documented "
        "exception to SIZE_ALLOWLIST:\n  " + "\n  ".join(sorted(offenders))
    )


def test_size_allowlist_has_no_stale_entries() -> None:
    """An allowlisted file that is no longer over budget should be removed."""
    stale = []
    for rel in SIZE_ALLOWLIST:
        path = REPO_ROOT / rel
        if not path.exists():
            stale.append(f"{rel}: file no longer exists")
            continue
        n = sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
        if n <= MAX_LINES:
            stale.append(f"{rel}: now {n} lines (<= {MAX_LINES}) — drop from allowlist")
    assert not stale, "Stale SIZE_ALLOWLIST entries:\n  " + "\n  ".join(stale)


# --- Facade-import guard ----------------------------------------------------

# ARCHITECTURE.md: "Import from the package, not from submodules, in external
# code." External code that reaches `core.panel.feed` instead of `core.panel`
# couples to internals the facade is meant to hide. Intra-package imports (a
# panel submodule importing a sibling) are fine and not flagged.
FACADE_PACKAGES = [
    "investment_panel.core.panel",
    "investment_panel.core.decision",
    "investment_panel.core.brokers",
    "investment_panel.core.free_sources",
    "investment_panel.core.disclosures",
    "app.data_access",
]

# Pre-existing deep imports, frozen by the ratchet: new ones are blocked, these
# are grandfathered. (importing_file, imported_module) -> reason / fix direction.
# Shrink this set over time; do not add to it without a real reason.
FACADE_IMPORT_ALLOWLIST: dict[tuple[str, str], str] = {
    # Re-export gaps: clean fix is to expose these from core/panel/__init__.py
    # and switch the import to the facade.
    ("app/data_access/loaders.py", "investment_panel.core.panel.read_session"):
        "re-export panel_read_session from the panel facade",
    ("app/data_access/payloads.py", "investment_panel.core.panel.payloads"):
        "re-export payload builders from the panel facade",
    ("app/data_access/payloads.py", "investment_panel.core.panel.ticker_dossier"):
        "re-export ticker_payload_tables from the panel facade",
    ("app/panel_contracts.py", "investment_panel.core.panel.contracts"):
        "thin re-export shim (import *) — fold into the panel facade",
    # Layering smells: worth a real fix, not just a re-export.
    ("src/investment_panel/core/robinhood_options/collector.py", "investment_panel.core.free_sources.coerce"):
        "cross-package internal import — prefer core/coercion.py",
    ("src/investment_panel/core/robinhood_options/collector.py", "investment_panel.core.free_sources.constants"):
        "cross-package internal import — move shared RADAR_*_DTE constants to a shared home",
}


def _facade_dir(dotted: str) -> Path:
    """Filesystem dir for a facade dotted path, across the src/ and app/ roots."""
    if dotted.startswith("investment_panel."):
        return REPO_ROOT / "src" / Path(*dotted.split("."))
    return REPO_ROOT / Path(*dotted.split("."))


def _imported_modules(tree: ast.AST) -> list[str]:
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — always intra-package, skip
                continue
            if node.module:
                mods.append(node.module)
    return mods


def test_external_code_imports_facade_not_submodules() -> None:
    violations = []
    for path in _prod_py_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
        for mod in _imported_modules(tree):
            for facade in FACADE_PACKAGES:
                if not mod.startswith(facade + "."):
                    continue  # exact-facade or unrelated import
                # Reaching into a submodule. Allowed only if the importer lives
                # inside that same facade package.
                inside = _facade_dir(facade) in path.parents
                if inside:
                    continue
                if (rel, mod) in FACADE_IMPORT_ALLOWLIST:
                    continue
                violations.append(f"{rel} imports {mod} (use '{facade}' instead)")
    assert not violations, (
        "External code imports a facade submodule directly — import from the "
        "package and add the symbol to its __init__.py if missing "
        "(see ARCHITECTURE.md):\n  " + "\n  ".join(sorted(violations))
    )


def test_facade_import_allowlist_has_no_stale_entries() -> None:
    """A grandfathered deep import that no longer occurs should be removed,
    so the ratchet keeps tightening."""
    seen: set[tuple[str, str]] = set()
    for path in _prod_py_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
        for mod in _imported_modules(tree):
            seen.add((rel, mod))
    stale = [f"{rel} -> {mod}" for (rel, mod) in FACADE_IMPORT_ALLOWLIST if (rel, mod) not in seen]
    assert not stale, (
        "Stale FACADE_IMPORT_ALLOWLIST entries (import is gone — drop them):\n  "
        + "\n  ".join(sorted(stale))
    )
