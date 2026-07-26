"""Module 환경 주입 규약, 시크릿 암호화/마스킹, 프리뷰 이름·TTL 검증."""
from datetime import datetime, timedelta, timezone

from app.models import Module, ModuleType, PreviewSession
from app.services import modules as svc
from app.services import preview


def _module(mtype: ModuleType, config: dict) -> Module:
    return Module(name="m", type=mtype, config=svc.encrypt_config(config))


def test_external_api_env():
    m = _module(ModuleType.external_api,
                {"url": "https://pay.example.com", "api_key": "sk-123"})
    env = svc.binding_env(m, "pay")
    assert env == {"PAY_URL": "https://pay.example.com", "PAY_API_KEY": "sk-123"}
    # 저장 형태는 암호문, 마스킹 조회는 평문 노출 없음
    assert m.config["api_key"] != "sk-123"
    assert svc.masked_config(m.config)["api_key"] == "•••"


def test_database_env_encrypted_dsn():
    dsn = "postgresql://u:pw@db/prod"
    m = _module(ModuleType.database, {"dsn": dsn})
    assert svc.binding_env(m, "MAIN")["MAIN_DSN"] == dsn
    # 저장 형태는 암호문 — 평문 DSN 전체가 그대로 남아있지 않아야 한다.
    # (2글자 부분 문자열 검사는 base64 암호문에 우연히 등장할 수 있어 플레이키했음)
    assert dsn not in str(m.config)


def test_internal_api_env_resolves_by_tier():
    """1차(small)는 target 프로젝트의 실제 배포 URL과 동일한 서브패스 규칙을 쓴다
    (db 없이 호출하면 조직을 알 수 없으니 "_" 자리로 안전하게 떨어진다)."""
    m = _module(ModuleType.internal_api, {"target_project": "mail-api"})
    small = svc.binding_env(m, "MAIL")
    assert small == {"MAIL_URL": "https://apps.test/apps/_/mail-api/"}

    from app.config import get_settings
    enterprise = get_settings().model_copy(
        update={"tier": "enterprise", "k8s_namespace": "paas-apps"}
    )
    ent = svc.binding_env(m, "MAIL", settings=enterprise)
    assert ent == {"MAIL_URL": "http://paas-mail-api.paas-apps.svc"}


def test_internal_api_env_uses_target_projects_organization(fresh_settings):
    """db가 주어지면 target_project를 조회해 그 프로젝트의 실제 조직 이름으로
    서브패스를 구성한다 — 실제 배포 URL(services/deployer.py)과 정확히 일치해야 한다."""
    from app.db import Base, engine
    from app.models import Organization, Project, ProjectType
    from sqlalchemy.orm import Session

    Base.metadata.create_all(engine)
    with Session(engine) as db:
        org = Organization(name="acme")
        db.add(org)
        db.commit()
        db.add(Project(name="mail-api", type=ProjectType.python,
                        organization_id=org.id, git_url="https://git.example.com/x"))
        db.commit()

        m = _module(ModuleType.internal_api, {"target_project": "mail-api"})
        env = svc.binding_env(m, "MAIL", db=db)
        assert env == {"MAIL_URL": "https://apps.test/apps/acme/mail-api/"}

        db.query(Project).delete()
        db.query(Organization).delete()
        db.commit()


def test_file_storage_env():
    m = _module(ModuleType.file_storage,
                {"endpoint": "http://seaweed:8333", "bucket": "assets"})
    env = svc.binding_env(m, "FS")
    assert env == {"FS_ENDPOINT": "http://seaweed:8333", "FS_BUCKET": "assets"}


def test_available_resources_global_and_org_scope():
    from app.db import Base, engine
    from app.models import Organization, Project, ProjectType
    from sqlalchemy.orm import Session

    Base.metadata.create_all(engine)
    with Session(engine) as db:
        org = Organization(name="shop-team")
        db.add(org)
        db.commit()

        db.add_all([
            Module(name="news-api", type=ModuleType.external_api, category="news", config={}),
            Module(name="llm-main", type=ModuleType.internal_api, category="llm", config={}),
            Module(name="shared-files", type=ModuleType.file_storage, config={}),
            Module(name="shop-db", type=ModuleType.database, organization_id=org.id, config={}),
        ])
        db.commit()

        shop_project = Project(name="shop-web", type=ProjectType.react,
                                organization_id=org.id, git_url="https://git.example.com/x")
        other_project = Project(name="other-app", type=ProjectType.python,
                                 git_url="https://git.example.com/y")
        db.add_all([shop_project, other_project])
        db.commit()

        shop_resources = {r["name"] for r in svc.available_resources(db, shop_project)}
        assert shop_resources == {"news-api", "llm-main", "shared-files", "shop-db"}

        other_resources = {r["name"] for r in svc.available_resources(db, other_project)}
        assert other_resources == {"news-api", "llm-main", "shared-files"}

        news = next(r for r in svc.available_resources(db, shop_project) if r["name"] == "news-api")
        assert news == {"id": news["id"], "name": "news-api", "type": "external_api",
                        "category": "news", "scope": "global"}
        shop_db = next(r for r in svc.available_resources(db, shop_project) if r["name"] == "shop-db")
        assert shop_db["scope"] == "org"

        db.query(Module).delete()
        db.query(Project).delete()
        db.query(Organization).delete()
        db.commit()


def test_preview_naming_and_expiry():
    assert preview.preview_unit_name("shop", 7) == "shop-pv7"
    assert preview.preview_domain("shop", 7) == "shop-pv7.apps.test"

    now = datetime.now(timezone.utc)
    live = PreviewSession(project_id=1, branch="b", expires_at=now + timedelta(minutes=5))
    dead = PreviewSession(project_id=1, branch="b", expires_at=now - timedelta(minutes=5))
    naive = PreviewSession(project_id=1, branch="b",
                           expires_at=(now - timedelta(minutes=5)).replace(tzinfo=None))
    assert not preview.is_expired(live, now)
    assert preview.is_expired(dead, now)
    assert preview.is_expired(naive, now)  # SQLite naive datetime도 처리
