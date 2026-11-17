"""부서별 기본 LLM과 게이트웨이의 부서 검사 — 부서마다 다른 AWS 프로필(프로바이더)을 쓰게 한다.

보는 것: 부서가 정해진 호출은 그 부서의 기본 LLM이 먼저(없거나 쓸 수 없으면 전역 기본), 기본 지정은 전역 것이나
그 부서 전용만, 게이트웨이(/proxy/llm)는 다른 부서 전용 프로바이더를 그 부서 사람이 아니면 거절한다."""
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import create_app
from app.models import LlmProvider, LlmProviderKind, Organization, UserAccount
from app.security import hash_password, issue_mcp_token
from app.services import llm as llm_service

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


def _setup():
    c = TestClient(create_app())
    with SessionLocal() as db:
        # 공용 테스트 DB다 — 지우지 않고 기본 표시만 내린다(다른 테스트의 프로바이더·세션이 가리킬 수 있다)
        db.query(LlmProvider).update({LlmProvider.is_default: False})
        import uuid
        tag = uuid.uuid4().hex[:6]
        a, b = Organization(name=f"dept-a-{tag}"), Organization(name=f"dept-b-{tag}")
        db.add_all([a, b])
        db.flush()
        rows = [LlmProvider(name=f"global-{tag}", kind=LlmProviderKind.openai, base_url="https://g", model="g",
                            is_default=True),
                LlmProvider(name=f"a-bedrock-{tag}", kind=LlmProviderKind.openai, base_url="https://a", model="a",
                            organization_id=a.id, aws_profile="team-a"),
                LlmProvider(name=f"b-bedrock-{tag}", kind=LlmProviderKind.openai, base_url="https://b", model="b",
                            organization_id=b.id, aws_profile="team-b")]
        db.add_all(rows)
        user = UserAccount(email=f"a-user-{tag}@example.com", password_hash=hash_password("x"), is_approved=True,
                           organization_id=a.id)
        user.organizations.append(a)
        db.add(user)
        db.commit()
        ids = {"a": a.id, "b": b.id, "tag": tag, "global-llm": rows[0].id, "a-bedrock": rows[1].id,
               "b-bedrock": rows[2].id}
    return c, ids


def test_department_default_comes_first_and_falls_back_to_global():
    c, ids = _setup()
    assert c.put(f"{API}/orgs/{ids['a']}/llm", json={"llm_provider_id": ids["a-bedrock"]}, headers=ADMIN).status_code == 200
    with SessionLocal() as db:
        assert llm_service.default_provider(db, ids["a"]).name == f"a-bedrock-{ids['tag']}"
        assert llm_service.default_provider(db, ids["b"]).name == f"global-{ids['tag']}"      # 지정 없음 → 전역 기본
        assert llm_service.default_provider(db).name == f"global-{ids['tag']}"               # 부서를 모르는 호출
    # 다른 부서 전용은 기본으로 둘 수 없고, 비우면 전역 기본으로 돌아간다
    bad = c.put(f"{API}/orgs/{ids['a']}/llm", json={"llm_provider_id": ids["b-bedrock"]}, headers=ADMIN)
    assert bad.status_code == 400 and "다른 부서 전용" in bad.json()["detail"]
    listed = {o["name"]: o for o in c.get(f"{API}/orgs", headers=ADMIN).json()}
    assert listed[f"dept-a-{ids['tag']}"]["llm_provider_id"] == ids["a-bedrock"]
    assert c.put(f"{API}/orgs/{ids['a']}/llm", json={"llm_provider_id": None}, headers=ADMIN).status_code == 200
    with SessionLocal() as db:
        assert llm_service.default_provider(db, ids["a"]).name == f"global-{ids['tag']}"


def test_gateway_refuses_another_departments_provider(monkeypatch):
    c, ids = _setup()
    monkeypatch.setattr(llm_service, "chat_completion", lambda provider, messages, db=None: f"from {provider.name}")
    with SessionLocal() as db:
        token = issue_mcp_token(db, f"a-user-{ids['tag']}@example.com", "laptop", is_admin=False)
    headers = {"authorization": f"Bearer {token}"}
    body = {"messages": [{"role": "user", "content": "x"}]}
    own = c.post(f"{API}/proxy/llm?provider_id={ids['a-bedrock']}", json=body, headers=headers)
    assert own.status_code == 200 and own.json()["reply"] == f"from a-bedrock-{ids['tag']}"
    assert c.post(f"{API}/proxy/llm?provider_id={ids['global-llm']}", json=body, headers=headers).status_code == 200
    other = c.post(f"{API}/proxy/llm?provider_id={ids['b-bedrock']}", json=body, headers=headers)
    assert other.status_code == 403 and "부서 전용" in other.json()["detail"]
    assert c.post(f"{API}/proxy/llm?provider_id={ids['b-bedrock']}", json=body, headers=ADMIN).status_code == 200
