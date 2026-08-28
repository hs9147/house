import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import create_app
from app.models import Base, ApiKey, Module, ModuleBinding, ModuleType, Organization, Project, ProjectType
from app.security import require_admin, require_api_key

@pytest.fixture
def db_session():
    # StaticPool로 연결 **하나**를 공유한다. sqlite :memory:의 기본 풀
    # (SingletonThreadPool)은 스레드마다 다른 빈 DB를 주는데, TestClient는 동기
    # 엔드포인트를 워커 스레드에서 돌린다 — 요청 도중 커밋으로 연결이 반납되는 순간부터
    # "no such table"이 난다(실제로 그랬다: 모듈 생성은 되고 그 뒤 감사 기록에서 터졌다).
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


def test_platform_module_report_endpoint(client, db_session):
    """전역 리포트 — 조직 이름과 모듈 감사 이력까지 함께 나온다.

    이 엔드포인트는 첫 줄부터 500이었다: Module에 organization 관계가 없는데
    joinedload(Module.organization)을 걸고 있었다. 이력 쪽도 감사 표에 없는 필드
    (r.payload — 실제 컬럼은 detail)를 읽고 있어서, 관계를 고쳐도 모듈 감사 이벤트가
    하나라도 있으면 다시 500이 났다. 두 자리 모두 **데이터가 있어야** 드러나므로
    행을 직접 심지 않고 API로 만든다.
    """
    org = Organization(name="TestOrg")
    db_session.add(org)
    db_session.commit()

    created = client.post("/paas/api/v1/modules", json={
        "name": "pay-api", "type": "external_api", "category": "payment",
        "organization_id": org.id, "config": {"url": "https://pay.example"},
    })
    assert created.status_code == 201, created.text

    res = client.get("/paas/api/v1/modules/usage-report")
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["total_modules"] == 1
    assert data["total_bindings"] == 0
    entry = data["modules"][0]
    assert entry["module_name"] == "pay-api"
    assert entry["type"] == "external_api"
    assert entry["organization_name"] == "TestOrg"  # Module.organization 관계
    assert entry["bound_project_count"] == 0

    assert [h["action"] for h in data["recent_history"]] == ["module.create"]
    assert data["recent_history"][0]["payload"] == {"type": "external_api"}


def test_platform_module_report_counts_bindings(client, db_session):
    project = Project(
        name="shop-web", type=ProjectType.react,
        git_url="https://git.example.com/x.git", branch="main",
    )
    db_session.add(project)
    db_session.commit()

    module = client.post("/paas/api/v1/modules", json={
        "name": "pay-api", "type": "external_api", "config": {"url": "https://pay.example"},
    }).json()
    bound = client.post(
        f"/paas/api/v1/projects/{project.id}/modules/{module['id']}/bind",
        json={"env_prefix": "PAY"})
    assert bound.status_code == 201, bound.text

    data = client.get("/paas/api/v1/modules/usage-report").json()
    assert data["total_bindings"] == 1
    assert data["modules"][0]["bound_projects"] == ["shop-web"]
    # 조직 없는 모듈은 전역이다 — 이름이 없다고 오류가 아니다
    assert data["modules"][0]["organization_name"] is None


def test_project_module_report_includes_audit_history(client, db_session):
    """프로젝트 리포트의 이력도 같은 필드를 읽는다 — 여기도 감사 이벤트가 있어야 드러난다."""
    project = Project(
        name="shop-web", type=ProjectType.react,
        git_url="https://git.example.com/x.git", branch="main",
    )
    db_session.add(project)
    db_session.commit()

    module = client.post("/paas/api/v1/modules", json={
        "name": "pay-api", "type": "external_api", "config": {"url": "https://pay.example"},
    }).json()
    bound = client.post(
        f"/paas/api/v1/projects/{project.id}/modules/{module['id']}/bind",
        json={"env_prefix": "PAY"})
    assert bound.status_code == 201, bound.text

    res = client.get(f"/paas/api/v1/projects/{project.id}/module-report")
    assert res.status_code == 200, res.text
    history = res.json()["history"]
    assert [h["action"] for h in history] == ["module.bind"]
    assert history[0]["payload"]
