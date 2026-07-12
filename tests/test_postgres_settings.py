from __future__ import annotations

from pathlib import Path

from app.data_access.config import load_config as load_api_config
from investment_panel.core.config import load_config as load_core_config
from investment_panel.database.authority import close_cached_runtimes
from investment_panel.database.configuration import SettingRepository
from investment_panel.database.runtime import DatabaseRuntime


def test_postgresql_settings_overlay_yaml_without_rewriting_it(
    migrated_postgres_dsn: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"database:\n  url: {migrated_postgres_dsn}\n"
        "agents:\n  option_thesis:\n    enabled: true\n    limit: 8\n"
        "research_sources:\n  news:\n    enabled: true\n    providers: [reuters]\n",
        encoding="utf-8",
    )
    original = config_path.read_text(encoding="utf-8")
    monkeypatch.delenv("MARKET_DATABASE_URL", raising=False)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        settings = SettingRepository(runtime)
        settings.set_section("agents", {"option_thesis": {"enabled": False, "limit": 3}})
        settings.set_section("research_sources", {"news": {"providers": ["hackernews"]}})
    finally:
        runtime.close()
    try:
        api = load_api_config(config_path)
        core = load_core_config(config_path)
        assert api["agents"]["option_thesis"]["enabled"] is False
        assert api["agents"]["option_thesis"]["limit"] == 3
        assert api["research_sources"]["news"]["providers"] == ["hackernews"]
        assert core.agents.option_thesis.enabled is False
        assert core.agents.option_thesis.limit == 3
        assert core.research_sources.news.providers == ["hackernews"]
        assert config_path.read_text(encoding="utf-8") == original
    finally:
        close_cached_runtimes()


def test_setting_repository_rejects_unapproved_sections(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        repository = SettingRepository(runtime)
        try:
            repository.set_section("database", {"url": "postgresql://attacker"})
        except ValueError as exc:
            assert "not writable" in str(exc)
        else:
            raise AssertionError("database settings must not be writable through app.setting")
    finally:
        runtime.close()
