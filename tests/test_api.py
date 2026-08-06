"""API 인증·프로젝트 CRUD·웹훅 서명·시크릿 마스킹 검증 (런타임 호출 없는 경로만)."""
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.main import create_app

ADMIN = {"x-api-key": "test-admin-key"}


def _client() -> TestClient:
    return TestClient(create_app())


def test_requires_api_key():
    c = _client()
    assert c.get("/paas/api/v1/projects").status_code == 401
    assert c.get("/paas/api/v1/projects", headers={"x-api-key": "wrong"}).status_code == 401


def test_key_name_is_not_a_credential():
    """계정명은 감사 로그·콘솔에 노출되는 공개 식별자다 — 그것만으로 인증되면 안 된다."""
    c = _client()
    assert c.post("/paas/api/v1/keys", json={"name": "ci-bot"}, headers=ADMIN).status_code == 201
    assert c.get("/paas/api/v1/projects", headers={"x-api-key": "ci-bot"}).status_code == 401


def test_project_crud_and_env_masking():
    c = _client()
    body = {
        "name": "shop-front",
        "type": "react",
        "git_url": "https://git.example.com/org/shop-front",
    }
    r = c.post("/paas/api/v1/projects", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["default_profile"] == "release"

    assert c.post("/paas/api/v1/projects", json=body, headers=ADMIN).status_code == 409

    r = c.put(f"/paas/api/v1/projects/{pid}/env", json={"key": "API_TOKEN", "value": "s3cret"}, headers=ADMIN)
    assert r.status_code == 204
    r = c.get(f"/paas/api/v1/projects/{pid}/env", headers=ADMIN)
    assert r.json() == [{"key": "API_TOKEN", "is_secret": True, "value": "•••"}]


def test_delete_project_removes_related_rows_and_keeps_audit(monkeypatch, fresh_settings, tmp_path):
    """프로젝트 삭제 — 딸린 행·워크스페이스 클론은 지우고, Gitea 리포·감사 로그는 남긴다."""
    from app.config import get_settings

    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "workspaces"))
    get_settings.cache_clear()

    c = _client()
    pid = c.post("/paas/api/v1/projects", json={
        "name": "doomed", "type": "react", "git_url": "https://git.example.com/o/doomed",
    }, headers=ADMIN).json()["id"]
    prov = c.post("/paas/api/v1/llm/providers", json={
        "name": "p", "kind": "openai", "base_url": "https://api.example.com",
        "api_key": "sk-secret", "model": "m",
    }, headers=ADMIN).json()["id"]
    c.put(f"/paas/api/v1/projects/{pid}/env",
          json={"key": "API_TOKEN", "value": "s3cret"}, headers=ADMIN)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]
    workdir = tmp_path / "workspaces" / "doomed"
    workdir.mkdir(parents=True)
    (workdir / "app.py").write_text("print('hi')\n", encoding="utf-8")

    assert c.delete(f"/paas/api/v1/projects/{pid}", headers=ADMIN).status_code == 204

    assert c.get(f"/paas/api/v1/projects/{pid}/env", headers=ADMIN).status_code == 404
    assert c.get(f"/paas/api/v1/plan/sessions/{sid}", headers=ADMIN).status_code == 404
    assert not workdir.exists()  # 워크스페이스 클론 정리
    assert not any(p["name"] == "doomed" for p in c.get("/paas/api/v1/projects", headers=ADMIN).json())
    # 감사 로그는 삭제 이력을 남긴다
    events = c.get("/paas/api/v1/audit", headers=ADMIN).json()
    assert any(e["action"] == "project.delete" and e["target"] == "doomed" for e in events)


def test_delete_project_requires_admin_and_404s_for_unknown():
    c = _client()
    pid = c.post("/paas/api/v1/projects", json={
        "name": "keep-me", "type": "react", "git_url": "https://git.example.com/o/keep-me",
    }, headers=ADMIN).json()["id"]
    issued = c.post("/paas/api/v1/keys", json={"name": "ci-bot"}, headers=ADMIN).json()["key"]

    assert c.delete(f"/paas/api/v1/projects/{pid}", headers={"x-api-key": issued}).status_code == 403
    assert c.delete("/paas/api/v1/projects/9999", headers=ADMIN).status_code == 404
    # 거절된 삭제로 프로젝트가 사라지지 않는다
    assert c.get(f"/paas/api/v1/projects/{pid}/env", headers=ADMIN).status_code == 200


def test_issue_key_and_use_it():
    c = _client()
    r = c.post("/paas/api/v1/keys", json={"name": "ci-bot"}, headers=ADMIN)
    assert r.status_code == 201
    issued = r.json()["key"]
    assert issued.startswith("paas_")
    assert c.get("/paas/api/v1/projects", headers={"x-api-key": issued}).status_code == 200
    # 일반 키로는 관리자 엔드포인트 접근 불가
    assert c.get("/paas/api/v1/audit", headers={"x-api-key": issued}).status_code == 403


def test_webhook_signature_required():
    c = _client()
    payload = {"ref": "refs/heads/main", "repository": {"clone_url": "https://x/y/z.git"}}
    raw = json.dumps(payload).encode()

    r = c.post("/paas/webhooks/git", content=raw, headers={"x-hub-signature-256": "sha256=bad"})
    assert r.status_code == 401

    sig = hmac.new(b"test-webhook-secret", raw, hashlib.sha256).hexdigest()
    r = c.post(
        "/paas/webhooks/git", content=raw,
        headers={"x-hub-signature-256": f"sha256={sig}",
                 "content-type": "application/json"},
    )
    assert r.status_code == 200
    assert "skipped" in r.json()


def _push(c: TestClient, payload: dict):
    raw = json.dumps(payload).encode()
    sig = hmac.new(b"test-webhook-secret", raw, hashlib.sha256).hexdigest()
    return c.post("/paas/webhooks/git", content=raw,
                  headers={"x-hub-signature-256": f"sha256={sig}",
                           "content-type": "application/json"})


def test_plan_artifact_only_push_does_not_deploy():
    """기획 산출물만 바뀐 push는 배포 신호가 아니다 — 단계 확정 머지가 운영본을 재배포하면 안 된다."""
    c = _client()
    c.post("/paas/api/v1/projects", json={
        "name": "hooked", "type": "react", "git_url": "https://git.example.com/o/hooked",
    }, headers=ADMIN)
    repo = {"clone_url": "https://git.example.com/o/hooked.git"}

    docs_only = {"ref": "refs/heads/main", "repository": repo, "commits": [
        {"modified": ["docs/agent-planning/01-기획서.md"], "added": [], "removed": []},
    ]}
    assert _push(c, docs_only).json()["skipped"] == "plan artifacts only"

    # 코드가 함께 바뀌면 평소대로 배포된다
    mixed = {"ref": "refs/heads/main", "repository": repo, "commits": [
        {"modified": ["docs/agent-planning/01-기획서.md"], "added": ["src/app.py"], "removed": []},
    ]}
    assert _push(c, mixed).json()["triggered"] == ["hooked"]

    # 변경 목록이 없는 payload는 판단하지 않고 기존대로 배포한다
    unknown = {"ref": "refs/heads/main", "repository": repo}
    assert _push(c, unknown).json()["triggered"] == ["hooked"]


def _set_latest_deployment(project_name: str, sha: str, status: str):
    """이 프로젝트의 "최신" 배포 레코드를 직접 심어(웹훅 재전달 중복 스킵 검증용)."""
    from sqlalchemy import select as sa_select

    from app.db import SessionLocal
    from app.models import Deployment, DeploymentStatus, Project

    with SessionLocal() as db:
        project = db.execute(
            sa_select(Project).where(Project.name == project_name)
        ).scalar_one()
        db.add(Deployment(
            project_id=project.id, git_sha=sha, image_tag="", profile="release",
            status=DeploymentStatus(status),
        ))
        db.commit()


def test_webhook_redelivery_of_already_running_commit_is_skipped():
    """Gitea가 같은 push를 재전달하면(네트워크 재시도, 관리자의 수동 Redeliver 등)
    이미 그 커밋으로 배포 완료된 상태라 또 빌드·재기동할 필요가 없다."""
    c = _client()
    c.post("/paas/api/v1/projects", json={
        "name": "redelivered", "type": "react", "git_url": "https://git.example.com/o/redelivered",
    }, headers=ADMIN)
    _set_latest_deployment("redelivered", "a" * 40, "running")

    repo = {"clone_url": "https://git.example.com/o/redelivered.git"}
    push = {"ref": "refs/heads/main", "repository": repo, "after": "a" * 40,
            "commits": [{"modified": ["src/app.py"], "added": [], "removed": []}]}
    body = _push(c, push).json()
    assert body["triggered"] == []
    assert body["skipped_duplicate"] == ["redelivered"]


def test_webhook_redelivery_while_still_building_is_skipped():
    """첫 배포가 아직 진행 중(building)인 시점에 같은 커밋의 재전달이 와도 또 트리거하지
    않는다 — 진행 중인 배포가 그 커밋을 그대로 이어서 처리한다."""
    c = _client()
    c.post("/paas/api/v1/projects", json={
        "name": "still-building", "type": "react", "git_url": "https://git.example.com/o/still-building",
    }, headers=ADMIN)
    _set_latest_deployment("still-building", "b" * 40, "building")

    repo = {"clone_url": "https://git.example.com/o/still-building.git"}
    push = {"ref": "refs/heads/main", "repository": repo, "after": "b" * 40,
            "commits": [{"modified": ["src/app.py"], "added": [], "removed": []}]}
    body = _push(c, push).json()
    assert body["triggered"] == []
    assert body["skipped_duplicate"] == ["still-building"]


def test_webhook_still_triggers_for_a_new_commit():
    """최신 배포와 다른 커밋이면(정상적인 새 push) 중복 판정 없이 그대로 배포한다."""
    c = _client()
    c.post("/paas/api/v1/projects", json={
        "name": "new-commit", "type": "react", "git_url": "https://git.example.com/o/new-commit",
    }, headers=ADMIN)
    _set_latest_deployment("new-commit", "c" * 40, "running")

    repo = {"clone_url": "https://git.example.com/o/new-commit.git"}
    push = {"ref": "refs/heads/main", "repository": repo, "after": "d" * 40,
            "commits": [{"modified": ["src/app.py"], "added": [], "removed": []}]}
    body = _push(c, push).json()
    assert body["triggered"] == ["new-commit"]
    assert body["skipped_duplicate"] == []


def test_webhook_retriggers_for_the_same_commit_after_a_failed_deploy():
    """직전 배포가 실패(failed)로 끝났다면, 같은 커밋을 다시 push해도(예: 원인 조치 없이
    재시도) 중복으로 보지 않고 다시 트리거한다 — failed는 "이미 배포됨"이 아니다."""
    c = _client()
    c.post("/paas/api/v1/projects", json={
        "name": "retry-after-fail", "type": "react",
        "git_url": "https://git.example.com/o/retry-after-fail",
    }, headers=ADMIN)
    _set_latest_deployment("retry-after-fail", "e" * 40, "failed")

    repo = {"clone_url": "https://git.example.com/o/retry-after-fail.git"}
    push = {"ref": "refs/heads/main", "repository": repo, "after": "e" * 40,
            "commits": [{"modified": ["src/app.py"], "added": [], "removed": []}]}
    body = _push(c, push).json()
    assert body["triggered"] == ["retry-after-fail"]
    assert body["skipped_duplicate"] == []


def test_audit_trail_recorded():
    c = _client()
    c.post("/paas/api/v1/projects", json={
        "name": "audit-target", "type": "python",
        "git_url": "https://git.example.com/org/api",
    }, headers=ADMIN)
    rows = c.get("/paas/api/v1/audit", headers=ADMIN).json()
    assert any(r["action"] == "project.create" and r["target"] == "audit-target" for r in rows)
