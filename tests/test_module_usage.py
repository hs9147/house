"""도구 관리 — 영향받는 에이전트 확인(GET /modules/{id}/usage)과 확인 없이 지우지 않는 삭제."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import create_app
from app.models import (ApiKey, AuditEvent, Base, Module, ModuleBinding, ModuleType, Organization, Project,
                        ProjectType, Workflow)
from app.security import require_admin, require_api_key


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session):
    app = create_app()
    admin_key = ApiKey(name="admin", key_hash="test", is_admin=True)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_api_key] = lambda: admin_key
    app.dependency_overrides[require_admin] = lambda: admin_key
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _module(db, name="kotra-news"):
    row = Module(name=name, type=ModuleType.external_api, config={"url": "https://apis.data.go.kr/x"})
    db.add(row)
    db.commit()
    return row


def test_usage_lists_bindings_workflows_and_recent_callers(client, db_session):
    org = Organization(name="gp-sil")
    db_session.add(org)
    db_session.commit()
    project = Project(name="gp-letter", type=ProjectType.python, git_url="https://git.example.com/x.git",
                      branch="main", organization_id=org.id)
    module = _module(db_session)
    db_session.add(project)
    db_session.commit()
    db_session.add_all([
        ModuleBinding(project_id=project.id, module_id=module.id, env_prefix="KOTRA_NEWS"),
        Workflow(organization_id=org.id, name="주간 동향", spec={"nodes": [{"id": "a", "module": "kotra-news"}]}),
        Workflow(organization_id=org.id, name="다른 일", spec={"nodes": [{"id": "a", "module": "other"}]}),
        AuditEvent(actor="app-gp-letter", action="proxy.module.call", target="kotra-news", detail={}),
        AuditEvent(actor="app-gp-letter", action="proxy.module.call", target="kotra-news", detail={}),
        AuditEvent(actor="app-epi", action="proxy.module.call", target="kotra-news", detail={}),
        AuditEvent(actor="app-epi", action="proxy.module.call", target="other", detail={}),
    ])
    db_session.commit()

    res = client.get(f"/paas/api/v1/modules/{module.id}/usage")
    assert res.status_code == 200
    data = res.json()
    assert data["bindings"] == [{"project_id": project.id, "project_name": "gp-letter", "env_prefix": "KOTRA_NEWS"}]
    assert [w["name"] for w in data["workflows"]] == ["주간 동향"]
    assert [(c["actor"], c["calls"]) for c in data["callers"]] == [("app-gp-letter", 2), ("app-epi", 1)]


def test_delete_with_usage_needs_module_name(client, db_session):
    module = _module(db_session)
    db_session.add(AuditEvent(actor="app-epi", action="proxy.module.call", target="kotra-news", detail={}))
    db_session.commit()

    assert client.delete(f"/paas/api/v1/modules/{module.id}").status_code == 409
    assert client.delete(f"/paas/api/v1/modules/{module.id}?confirm=wrong").status_code == 409
    assert db_session.get(Module, module.id) is not None
    assert client.delete(f"/paas/api/v1/modules/{module.id}?confirm=kotra-news").status_code == 204


def test_delete_unused_module_needs_no_confirm(client, db_session):
    module = _module(db_session)
    assert client.delete(f"/paas/api/v1/modules/{module.id}").status_code == 204


def test_rename_to_existing_name_is_refused(client, db_session):
    module = _module(db_session)
    _module(db_session, "taken")
    res = client.put(f"/paas/api/v1/modules/{module.id}",
                     json={"name": "taken", "type": "external_api", "category": None, "organization_id": None,
                           "config": {}})
    assert res.status_code == 409
