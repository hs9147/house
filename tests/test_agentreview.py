"""에이전트 검토 — 배포된 커밋의 설명을 LLM이 읽어 용도를 남기고, 스마트워크가 그것으로 고른다."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import create_app
from app.models import (
    AgentProfile, Deployment, DeploymentStatus, LlmProvider, LlmProviderKind, Project,
)
from app.services import agentreview, smartwork, workspace
from app.services import llm as llm_service

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"
REPLY = json.dumps({
    "purpose": "협력사 풀을 조회·관리한다",
    "does": ["협력사 검색", "등급 확인"],
    "does_not": ["신규 협력사 등록 품의 작성"],
    "inputs": ["협력사명"],
}, ensure_ascii=False)


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """README·진입 파일·의존성 디렉터리가 있는 리포. 커밋한 뒤 워킹카피를 다른 브랜치로 옮긴다."""
    root = tmp_path / "supplier-pool"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@test")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("# 협력사 풀\n협력사를 검색하고 등급을 본다.", encoding="utf-8")
    (root / "app.py").write_text("import streamlit as st\nst.title('협력사 풀')", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.md").write_text("의존성 문서 — 넣으면 안 된다", encoding="utf-8")
    (root / "util.py").write_text("def f(): pass", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    sha = _git(root, "rev-parse", "HEAD")
    # 기획 세션이 워킹카피를 다른 브랜치로 옮겨 놓은 상태 — 검토는 배포된 커밋을 읽어야 한다.
    _git(root, "checkout", "-q", "-b", "plan")
    (root / "README.md").write_text("# 엉뚱한 초안", encoding="utf-8")
    return root, sha


@pytest.fixture
def app_db():
    create_app()  # 표를 만든다(create_all)


def _project(name="supplier-pool", sha="") -> int:
    with SessionLocal() as db:
        project = Project(name=name, type="react", git_url=f"https://git.test/{name}")
        db.add(project)
        db.flush()
        if sha:
            db.add(Deployment(project_id=project.id, git_sha=sha, image_tag="",
                              profile="release", status=DeploymentStatus.running))
        db.commit()
        return project.id


def _provider() -> LlmProvider:
    return LlmProvider(name="p", kind=LlmProviderKind.openai, base_url="https://x", model="m")


def test_sources_put_human_written_descriptions_first():
    paths = ["util.py", "app.py", "docs/기획서.md", "README.md", "package.json",
             "node_modules/a/README.md", "tests/test_app.py", "pages/1_검색.py"]
    assert agentreview.pick_sources(paths) == [
        "README.md", "docs/기획서.md", "app.py", "pages/1_검색.py", "package.json"]
    # 하위 폴더 배포면 그 폴더 것과 루트의 설명만
    assert agentreview.pick_sources(["README.md", "api/app.py", "web/main.tsx", "web/README.md"],
                                    "web") == ["README.md", "web/README.md", "web/main.tsx"]


def test_review_reads_the_deployed_commit_and_records_what_it_fed(repo, app_db, monkeypatch):
    root, sha = repo
    pid = _project(sha=sha)
    seen: list = []
    monkeypatch.setattr(workspace, "code_workdir", lambda project: root)
    monkeypatch.setattr(llm_service, "default_provider", lambda db: _provider())
    monkeypatch.setattr(llm_service, "chat_completion",
                        lambda p, messages, db=None: seen.append(messages) or f"```json\n{REPLY}\n```")
    with SessionLocal() as db:
        row = agentreview.review(db, db.get(Project, pid), sha)
        assert row.error is None
        assert (row.purpose, row.does_not) == ("협력사 풀을 조회·관리한다", ["신규 협력사 등록 품의 작성"])
        # 근거는 서버가 넣은 파일이다 — 의존성 폴더·잡다한 코드는 넣지 않는다
        assert row.sources == ["README.md", "app.py"]
        assert (row.git_sha, row.provider, row.model) == (sha, "p", "m")
    facts = seen[0][1]["content"]
    assert "협력사를 검색하고 등급을 본다" in facts and "엉뚱한 초안" not in facts
    assert "넣으면 안 된다" not in facts


def test_failed_review_keeps_the_previous_summary_and_says_why(repo, app_db, monkeypatch):
    root, sha = repo
    pid = _project(sha=sha)
    monkeypatch.setattr(workspace, "code_workdir", lambda project: root)
    monkeypatch.setattr(llm_service, "default_provider", lambda db: _provider())
    monkeypatch.setattr(llm_service, "chat_completion", lambda p, m, db=None: REPLY)
    with SessionLocal() as db:
        agentreview.review(db, db.get(Project, pid), sha)
    monkeypatch.setattr(llm_service, "default_provider", lambda db: None)
    with SessionLocal() as db:
        row = agentreview.review(db, db.get(Project, pid), "b" * 40)
        assert "기본 LLM" in row.error
        assert row.purpose == "협력사 풀을 조회·관리한다"
    monkeypatch.setattr(llm_service, "default_provider", lambda db: _provider())
    monkeypatch.setattr(llm_service, "chat_completion", lambda p, m, db=None: "잘 모르겠습니다")
    with SessionLocal() as db:
        assert "JSON" in agentreview.review(db, db.get(Project, pid), sha).error


def test_stale_when_missing_redeployed_or_failed_long_ago():
    now = datetime.now(timezone.utc)
    ok = AgentProfile(git_sha="a", error=None, reviewed_at=now)
    assert agentreview.stale(None, "a")
    assert not agentreview.stale(ok, "a")
    assert agentreview.stale(ok, "b")  # 다시 배포됐다
    failed = AgentProfile(git_sha="a", error="x", reviewed_at=now - timedelta(minutes=5))
    assert not agentreview.stale(failed, "a", now)  # 실패가 계속되는 동안 턴마다 걸지 않는다
    assert agentreview.stale(failed, "a", now + agentreview.RETRY_AFTER)


def test_smartwork_picks_by_purpose_and_reviews_unreviewed_agents(app_db, monkeypatch):
    """목록에는 용도가 실리고, 검토 전인 것은 뒤에서 검토를 건다 — 그 턴은 기다리지 않는다."""
    reviewed = _project("supplier-pool", sha="a" * 40)
    fresh = _project("leave-agent", sha="c" * 40)
    with SessionLocal() as db:
        db.add(AgentProfile(project_id=reviewed, git_sha="a" * 40, purpose="협력사 풀을 조회한다",
                            does=["협력사 검색"], does_not=["등록 품의 작성"]))
        db.commit()
    scheduled: list = []
    monkeypatch.setattr(agentreview, "schedule", lambda pid, sha: scheduled.append((pid, sha)))
    captured: dict = {}

    def fake_chat(provider, messages, db=None, tools=None, tool_executor=None):
        captured["system"] = messages[0]["content"]
        captured["tools"] = tools
        return "완료"

    monkeypatch.setattr(llm_service, "chat_completion", fake_chat)
    with SessionLocal() as db:
        from app.models import ApiKey
        key = ApiKey(name="admin", is_admin=True, key_hash="x")
        smartwork.chat(db, key, _provider(), [{"role": "user", "content": "협력사 등록 품의 써 줘"}], [])

    assert scheduled == [(fresh, "c" * 40)]
    system = captured["system"]
    assert ("- supplier-pool (react, 조직 공용) — 목적: 협력사 풀을 조회한다 / 하는 일: 협력사 검색"
            " / 하지 않는 일: 등록 품의 작성") in system
    assert "leave-agent (react, 조직 공용) — 용도 검토 전" in system
    assert "목록에 하나뿐이라는 것은 근거가 아니다" in system
    names = [t["function"]["name"] for t in captured["tools"]]
    show = next(t for t in captured["tools"] if t["function"]["name"] == "show_agent")
    assert names[0] != "show_agent"  # 첫 자리라는 것만으로 먼저 불리지 않게
    assert show["function"]["parameters"]["required"] == ["name", "reason"]


def test_profile_api_reviews_only_release_deployments(repo, app_db, monkeypatch):
    root, sha = repo
    c = TestClient(create_app())
    undeployed = _project("draft-app")
    assert c.post(f"{API}/projects/{undeployed}/agent-profile/review", headers=ADMIN).status_code == 409
    pid = _project(sha=sha)
    assert c.get(f"{API}/projects/{pid}/agent-profile", headers=ADMIN).json() == \
        {"release_sha": sha, "stale": True, "profile": None}

    monkeypatch.setattr(workspace, "code_workdir", lambda project: root)
    monkeypatch.setattr(llm_service, "default_provider", lambda db: _provider())
    monkeypatch.setattr(llm_service, "chat_completion", lambda p, m, db=None: REPLY)
    out = c.post(f"{API}/projects/{pid}/agent-profile/review", headers=ADMIN).json()
    assert out["stale"] is False and out["profile"]["does"] == ["협력사 검색", "등급 확인"]
    assert c.get(f"{API}/projects/{pid}/agent-profile", headers=ADMIN).json()["stale"] is False
    assert c.get(f"{API}/projects/{pid}/agent-profile").status_code == 401
