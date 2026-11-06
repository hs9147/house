"""갭3 — Alembic 초기 리비전이 빈 DB에 전체 스키마를 만드는지 검증."""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

PLATFORM_DIR = Path(__file__).resolve().parent.parent

EXPECTED_TABLES = {
    "projects", "deployments", "env_vars", "api_keys", "audit_events",
    "llm_providers", "chat_sessions", "chat_messages", "build_tasks",
    "modules", "module_bindings", "preview_sessions", "payments",
    "redirect_rules", "user_accounts", "user_sessions", "user_organizations",
    "plan_artifacts", "port_allocations", "alembic_version",
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


def test_document_store_migration_after_create_all(alembic_cfg):
    """새 코드가 먼저 기동해 표를 만든 경우에도 저장소 리비전을 적용할 수 있다."""
    cfg, url = alembic_cfg
    command.upgrade(cfg, "fc3d4e5f6a7b")
    from app.db import Base

    Base.metadata.create_all(create_engine(url))
    command.upgrade(cfg, "head")
    assert "document_stores" in inspect(create_engine(url)).get_table_names()


def test_startup_names_the_missing_columns(monkeypatch, fresh_settings, tmp_path, capsys):
    """회귀: 새 컬럼이 생긴 버전으로 올리고 마이그레이션을 돌리지 않으면 그 테이블을
    건드리는 화면이 전부 500이 됐고, 화면에는 원인 단서가 없었다(기획 세션 이력·서버 구성).

    create_all은 없는 **테이블**만 만들고 기존 테이블에 컬럼을 추가하지 않는다 — 그래서
    기동할 때 무엇이 빠졌는지 이름으로 말해 줘야 한다.
    """
    import sqlite3

    import sqlalchemy as sa

    from app import main

    db_path = tmp_path / "behind.db"
    # 현재 모델로 스키마를 만든 뒤 컬럼 하나를 빼서 "뒤처진 DB"를 만든다
    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    main.Base.metadata.create_all(engine)
    engine.dispose()
    con = sqlite3.connect(db_path)
    con.execute("ALTER TABLE projects DROP COLUMN structure")
    con.commit()
    con.close()

    monkeypatch.setattr(main, "engine", sa.create_engine(f"sqlite:///{db_path.as_posix()}"))
    main._warn_if_schema_is_behind()
    out = capsys.readouterr().out
    assert "projects.structure" in out
    assert "alembic" in out  # 복구 방법도 함께 말한다


def test_startup_is_quiet_when_the_schema_matches(monkeypatch, fresh_settings, tmp_path, capsys):
    """맞는 DB에서 경고가 나오면 진짜 경고를 무시하게 된다."""
    import sqlalchemy as sa

    from app import main

    db_path = tmp_path / "ok.db"
    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    main.Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    main._warn_if_schema_is_behind()
    assert "뒤처졌습니다" not in capsys.readouterr().out


def test_upgrade_reaches_head_even_after_the_app_made_the_tables(alembic_cfg):
    """앱은 기동할 때 표를 직접 만든다(create_all) — 그 뒤에 upgrade head를 돌려도 「already exists」로 멈추지 않고
    끝까지 간다(migrations/env.py가 이미 된 것을 건너뛴다). 실측: 개발 DB가 mcp_tokens에서 멈춰 있었다."""
    cfg, url = alembic_cfg
    from app.db import Base
    from app.services.schemacheck import code_heads, missing_columns, revision_state

    engine = create_engine(url)
    Base.metadata.create_all(engine)
    command.upgrade(cfg, "head")
    state = revision_state(engine)
    assert state["db"] == code_heads() and state["behind"] is False
    assert missing_columns(engine, Base.metadata) == []
    command.upgrade(cfg, "head")                       # 다시 돌려도 그대로


def test_columns_without_a_revision_are_added_on_upgrade(alembic_cfg):
    """모델에 열을 더하고 리비전을 빠뜨린 경우 — upgrade head가 끝에 그 열을 더한다(더하기만)."""
    cfg, url = alembic_cfg
    from sqlalchemy import text

    from app.db import Base
    from app.services.schemacheck import missing_columns

    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE data_schemas DROP COLUMN unit_id"))
    assert missing_columns(engine, Base.metadata) == ["data_schemas.unit_id"]
    command.upgrade(cfg, "head")
    assert missing_columns(engine, Base.metadata) == []


def test_health_reports_a_revision_behind(alembic_cfg):
    cfg, url = alembic_cfg
    from app.services.schemacheck import revision_state

    command.upgrade(cfg, "fc3d4e5f6a7b")
    state = revision_state(create_engine(url))
    assert state["db"] == ["fc3d4e5f6a7b"] and state["behind"] is True
