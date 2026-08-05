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
    assert _push(c, docs_only).json() == {"skipped": "plan artifacts only"}

    # 코드가 함께 바뀌면 평소대로 배포된다
    mixed = {"ref": "refs/heads/main", "repository": repo, "commits": [
        {"modified": ["docs/agent-planning/01-기획서.md"], "added": ["src/app.py"], "removed": []},
    ]}
    assert _push(c, mixed).json() == {"triggered": ["hooked"]}

    # 변경 목록이 없는 payload는 판단하지 않고 기존대로 배포한다
    unknown = {"ref": "refs/heads/main", "repository": repo}
    assert _push(c, unknown).json() == {"triggered": ["hooked"]}


def test_audit_trail_recorded():
    c = _client()
    c.post("/paas/api/v1/projects", json={
        "name": "audit-target", "type": "python",
        "git_url": "https://git.example.com/org/api",
    }, headers=ADMIN)
    rows = c.get("/paas/api/v1/audit", headers=ADMIN).json()
    assert any(r["action"] == "project.create" and r["target"] == "audit-target" for r in rows)
