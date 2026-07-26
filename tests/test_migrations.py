"""갭3 — Alembic 초기 리비전이 빈 DB에 전체 스키마를 만드는지 검증."""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

PLATFORM_DIR = Path(__file__).resolve().parent.parent

EXPECTED_TABLES = {
    "projects", "deployments", "env_vars", "api_keys", "audit_events",
    "llm_providers", "chat_sessions", "chat_messages", "proposed_changes",
    "modules", "module_bindings", "preview_sessions", "payments",
    "redirect_rules", "alembic_version",
}


@pytest.fixture
def alembic_cfg(monkeypatch, tmp_path, fresh_settings):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("PAAS_DATABASE_URL", url)
    from app.config import get_settings

    get_settings.cache_clear()
    cfg = Config(str(PLATFORM_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(PLATFORM_DIR / "migrations"))
    return cfg, url


def test_upgrade_head_creates_all_tables(alembic_cfg):
    cfg, url = alembic_cfg
    command.upgrade(cfg, "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert EXPECTED_TABLES <= tables, EXPECTED_TABLES - tables


def test_migration_matches_models(alembic_cfg):
    """마이그레이션으로 만든 스키마가 create_all 결과와 테이블 집합이 같아야 한다."""
    cfg, url = alembic_cfg
    command.upgrade(cfg, "head")
    migrated = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}

    from app.db import Base

    assert migrated == set(Base.metadata.tables.keys())
