import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import get_db
from app.main import create_app
from app.models import Base, ApiKey, Module, ModuleBinding, ModuleType, Organization, Project, ProjectType
from app.security import require_admin, require_api_key

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
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


def test_project_module_report_endpoint(client, db_session):
    org = Organization(name="TestOrg")
    db_session.add(org)
    db_session.commit()

    project = Project(
        name="test-report-proj",
        type=ProjectType.python,
        git_url="https://git.example.com/test.git",
        branch="main",
        organization_id=org.id,
    )
    db_session.add(project)
    db_session.commit()

    module = Module(
        name="test-db-mod",
        type=ModuleType.database,
        category="database",
        config={"dsn": "sqlite:///:memory:"},
    )
    db_session.add(module)
    db_session.commit()

    binding = ModuleBinding(
        project_id=project.id,
        module_id=module.id,
        env_prefix="DB",
    )
    db_session.add(binding)
    db_session.commit()

    res = client.get(f"/paas/api/v1/projects/{project.id}/module-report")
    assert res.status_code == 200
    data = res.json()

    assert data["project_id"] == project.id
    assert data["project_name"] == "test-report-proj"
    assert data["org_name"] == "TestOrg"
    assert data["total_active_modules"] == 1
    assert data["total_injected_envs"] > 0
    assert len(data["active_modules"]) == 1
    assert data["active_modules"][0]["name"] == "test-db-mod"
    assert data["active_modules"][0]["env_prefix"] == "DB"
