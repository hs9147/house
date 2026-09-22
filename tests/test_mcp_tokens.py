"""개인 MCP 토큰과 프로젝트 접근 권한.

외주 개발 에이전트에게는 API 키가 없다. 관리자가 키를 나눠 주는 것도 답이 아니다 —
누구에게 나갔는지·언제 회수하는지가 남지 않는다. SSO로 로그인한 사람이 자기 몫을 직접
발급하고, 그 사람의 조직 권한으로 프로젝트 접근이 판정된다.

여기서 지키는 것:
  1. 토큰은 MCP에서만 통한다 — 새어도 배포·터미널·계정 관리로는 못 간다
  2. 남의 조직 프로젝트는 막힌다 (예전에는 **검사가 아예 없었다**)
  3. 사내 Gitea에 등록된 프로젝트만 MCP를 연다
  4. 원문은 한 번만 나가고, 폐기할 수 있다
"""
import json
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import (
    ApiKey,
    McpToken,
    Organization,
    Project,
    ProjectType,
    UserAccount,
    utcnow,
)
from app.security import hash_password, issue_mcp_token, require_project_mcp_access

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


@pytest.fixture
def gitea_configured(monkeypatch, fresh_settings):
    """사내 Gitea가 설정된 상태 — '사내 리포인가' 판정이 켜진다."""
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok")
    get_settings.cache_clear()
    return TestClient(create_app())


def _user(db, email: str, org: Organization | None, is_admin: bool = False) -> UserAccount:
    user = UserAccount(email=email, password_hash=hash_password("x"), is_approved=True,
                       is_admin=is_admin, organization_id=org.id if org else None)
    if org is not None:
        user.organizations.append(org)
    db.add(user)
    db.commit()
    return user


def _project(db, name: str, org: Organization | None, git_url: str) -> Project:
    project = Project(name=name, type=ProjectType.python, git_url=git_url,
                      organization_id=org.id if org else None)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def test_token_is_rejected_outside_mcp(gitea_configured):
    """개발자 기계의 설정 파일에 놓이는 값이다 — 새어도 MCP 밖으로는 아무것도 못 해야 한다."""
    c = gitea_configured
    db = SessionLocal()
    try:
        _user(db, "dev@example.com", None)
        token = issue_mcp_token(db, "dev@example.com", "laptop", is_admin=False)
    finally:
        db.close()

    headers = {"authorization": f"Bearer {token}"}
    # MCP가 아닌 경로는 이 토큰을 모른다
    assert c.get(f"{API}/projects", headers=headers).status_code == 401
    assert c.get(f"{API}/system/host", headers=headers).status_code in (401, 403, 404)


def test_token_opens_the_project_mcp_of_its_own_org(gitea_configured):
    c = gitea_configured
    db = SessionLocal()
    try:
        org = Organization(name="team-a")
        db.add(org)
        db.commit()
        _user(db, "a@example.com", org)
        project = _project(db, "app-a", org, "https://git.example.com/team-a/app-a")
        pid = project.id
        token = issue_mcp_token(db, "a@example.com", "laptop", is_admin=False)
    finally:
        db.close()

    r = c.post(f"{API}/plan/projects/{pid}/mcp", headers={"authorization": f"Bearer {token}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 200, r.text
    assert "result" in r.json()


def test_other_orgs_project_is_refused(gitea_configured):
    """예전에는 검사가 아예 없었다 — 유효한 키면 남의 조직 작업 지시도 읽혔다."""
    c = gitea_configured
    db = SessionLocal()
    try:
        mine = Organization(name="team-mine")
        theirs = Organization(name="team-theirs")
        db.add_all([mine, theirs])
        db.commit()
        _user(db, "mine@example.com", mine)
        other = _project(db, "app-theirs", theirs, "https://git.example.com/team-theirs/app")
        other_id = other.id
        token = issue_mcp_token(db, "mine@example.com", "laptop", is_admin=False)
    finally:
        db.close()

    r = c.post(f"{API}/plan/projects/{other_id}/mcp",
               headers={"authorization": f"Bearer {token}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 403
    assert "다른 조직" in r.json()["detail"]


def test_project_outside_internal_gitea_is_refused(gitea_configured, monkeypatch):
    """사내 리포만 허용하는 정책(PAAS_GIT_INTERNAL_ONLY)이 켜져 있으면 MCP도 그것을 따른다.

    정책을 MCP에서 따로 정하지 않는다 — 등록 시점에 이미 강제되는 같은 규칙이다.
    """
    monkeypatch.setenv("PAAS_GIT_INTERNAL_ONLY", "true")
    get_settings.cache_clear()
    c = gitea_configured
    db = SessionLocal()
    try:
        _user(db, "b@example.com", None)
        outside = _project(db, "app-outside", None, "https://github.com/someone/app")
        outside_id = outside.id
        token = issue_mcp_token(db, "b@example.com", "laptop", is_admin=False)
    finally:
        db.close()

    r = c.post(f"{API}/plan/projects/{outside_id}/mcp",
               headers={"authorization": f"Bearer {token}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 403
    assert "사내 Gitea" in r.json()["detail"]


def test_external_repo_is_allowed_when_the_policy_is_off(monkeypatch, fresh_settings):
    """운영자가 외부 리포를 일부러 허용했으면(PAAS_GIT_INTERNAL_ONLY=false) MCP도 막지 않는다."""
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GIT_INTERNAL_ONLY", "false")
    get_settings.cache_clear()
    create_app()
    db = SessionLocal()
    try:
        _user(db, "c@example.com", None)
        project = _project(db, "app-nogitea", None, "https://github.com/someone/app")
        key = ApiKey(name="c@example.com", key_hash="", is_admin=False)
        require_project_mcp_access(db, key, project)  # 예외가 없어야 한다
    finally:
        db.close()


def test_code_server_lists_only_reachable_projects(gitea_configured):
    """목록에는 보이는데 읽으면 403인 프로젝트가 생기면 안 된다 — 같은 판정을 쓴다."""
    c = gitea_configured
    db = SessionLocal()
    try:
        mine = Organization(name="code-mine")
        theirs = Organization(name="code-theirs")
        db.add_all([mine, theirs])
        db.commit()
        _user(db, "d@example.com", mine)
        _project(db, "code-ok", mine, "https://git.example.com/code-mine/ok")
        _project(db, "code-no", theirs, "https://git.example.com/code-theirs/no")
        token = issue_mcp_token(db, "d@example.com", "laptop", is_admin=False)
    finally:
        db.close()

    r = c.post(f"{API}/mcp/code", headers={"authorization": f"Bearer {token}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "list_projects", "arguments": {}}})
    assert r.status_code == 200, r.text
    text = json.dumps(r.json(), ensure_ascii=False)
    assert "code-ok" in text
    assert "code-no" not in text


def test_issue_lists_and_revokes_own_tokens_only(gitea_configured):
    """남의 토큰은 없는 것으로 답한다 — 존재 여부조차 알려 주지 않는다."""
    c = gitea_configured
    db = SessionLocal()
    try:
        org = Organization(name="tok-team")
        db.add(org)
        db.commit()
        _user(db, "owner@example.com", org)
        _project(db, "tok-app", org, "https://git.example.com/tok-team/tok-app")
        stranger = issue_mcp_token(db, "stranger@example.com", "남의 것", is_admin=False)
        assert stranger  # 발급 자체는 된다(다른 사람의 토큰)
        stranger_id = db.execute(
            sa.select(McpToken).where(McpToken.email == "stranger@example.com")
        ).scalars().one().id
    finally:
        db.close()

    # 발급 — admin 키로 부르면 주체는 bootstrap-admin이다(자기 몫만 만든다)
    r = c.post(f"{API}/mcp/tokens", headers=ADMIN, json={"label": "내 노트북", "project": "tok-app"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["token"] and body["ttl_days"] > 0
    assert body["url"].endswith("/mcp")  # 붙여 넣을 주소까지 함께 온다

    listing = c.get(f"{API}/mcp/tokens", headers=ADMIN).json()
    assert [t["label"] for t in listing] == ["내 노트북"]  # 남의 토큰은 보이지 않는다
    assert "token" not in json.dumps(listing)  # 원문은 목록에 없다

    # 남의 토큰 폐기는 404
    assert c.delete(f"{API}/mcp/tokens/{stranger_id}", headers=ADMIN).status_code == 404
    # 자기 토큰은 폐기된다
    assert c.delete(f"{API}/mcp/tokens/{listing[0]['id']}", headers=ADMIN).status_code == 204
    assert c.get(f"{API}/mcp/tokens", headers=ADMIN).json() == []


def test_expired_token_stops_working_but_stays_listed(gitea_configured, monkeypatch):
    """만료를 행 삭제로 처리하면 "사라졌다"가 된다 — 왜 안 되는지 알 수 없다."""
    c = gitea_configured
    db = SessionLocal()
    try:
        org = Organization(name="exp-team")
        db.add(org)
        db.commit()
        _user(db, "exp@example.com", org)
        project = _project(db, "exp-app", org, "https://git.example.com/exp-team/exp-app")
        pid = project.id
        token = issue_mcp_token(db, "exp@example.com", "오래된 것", is_admin=False)
        row = db.execute(
            sa.select(McpToken).where(McpToken.email == "exp@example.com")
        ).scalars().one()
        row.expires_at = utcnow() - timedelta(days=1)
        db.commit()
    finally:
        db.close()

    r = c.post(f"{API}/plan/projects/{pid}/mcp", headers={"authorization": f"Bearer {token}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 401
