"""Personal investment panel backend.

Runtime entry points live in explicit modules. Keeping package initialization
side-effect free prevents legacy import-only tooling from loading DuckDB into
the PostgreSQL application process.
"""
