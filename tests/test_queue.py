"""갭2 — 비동기 배포 큐: wait=false 202 즉시 반환 → 백그라운드 파이프라인 완료 폴링."""
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import BuildProfile
from app.services import deployer
from app.services.build import BuildResult
from app.services.runtime.base import Endpoint

ADMIN = {"x-api-key": "test-admin-key"}


class _FakeRuntime:
    def start(self, spec):
        return Endpoint(host="127.0.0.1", port=8123)

    def stop(self, *a): ...
    def status(self, *a): return "running"
    def logs(self, *a, **kw): return ""


def _mock_pipeline(monkeypatch, tmp_path, delay: float = 0.0):
    def fake_checkout(project, git_sha=None):
        if delay:
            time.sleep(delay)
        return tmp_path, "a" * 40

    monkeypatch.setattr(deployer, "checkout", fake_checkout)
    monkeypatch.setattr(
        deployer, "build_image",
        lambda project, workdir, sha, profile: BuildResult(
            image_tag=f"{project.name}:{sha[:12]}", internal_port=8000,
            log_path=Path(tmp_path / "b.log"), profile=profile,
        ),
    )
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(deployer.proxy, "configure", lambda *a, **kw: None)


def _create_project(c: TestClient, name: str) -> int:
    return c.post("/paas/api/v1/projects", json={
        "name": name, "type": "python", "git_url": "https://git.example.com/x",
    }, headers=ADMIN).json()["id"]


def test_queued_deploy_returns_202_then_completes(monkeypatch, tmp_path):
    _mock_pipeline(monkeypatch, tmp_path)
    c = _client = TestClient(create_app())
    pid = _create_project(c, "queued-app")

    r = c.post(f"/paas/api/v1/projects/{pid}/deploy", json={"profile": "release", "wait": False},
               headers=ADMIN)
    assert r.status_code == 202
    dep_id = r.json()["id"]
    assert r.json()["status"] == "building"

    # 백그라운드 파이프라인 완료 폴링 (목킹이라 수 초 내)
    for _ in range(50):
        rows = c.get(f"/paas/api/v1/projects/{pid}/deployments", headers=ADMIN).json()
        row = next(d for d in rows if d["id"] == dep_id)
        if row["status"] != "building":
            break
        time.sleep(0.1)
    assert row["status"] == "running", row
    assert row["git_sha"] == "a" * 40
    assert row["image_tag"].startswith("queued-app:")


def test_sync_deploy_still_default(monkeypatch, tmp_path):
    _mock_pipeline(monkeypatch, tmp_path)
    c = TestClient(create_app())
    pid = _create_project(c, "sync-app")
    r = c.post(f"/paas/api/v1/projects/{pid}/deploy", json={"profile": "development"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["status"] == "running"


def test_queued_deploy_conflicts_marked_failed(monkeypatch, tmp_path):
    _mock_pipeline(monkeypatch, tmp_path, delay=0.5)
    c = TestClient(create_app())
    pid = _create_project(c, "busy-app")

    first = c.post(f"/paas/api/v1/projects/{pid}/deploy", json={"wait": False}, headers=ADMIN)
    second = c.post(f"/paas/api/v1/projects/{pid}/deploy", json={"wait": False}, headers=ADMIN)
    assert first.status_code == 202 and second.status_code == 202

    ids = {first.json()["id"], second.json()["id"]}
    deadline = time.time() + 10
    rows_by_id = {}
    while time.time() < deadline:
        rows = c.get(f"/paas/api/v1/projects/{pid}/deployments", headers=ADMIN).json()
        rows_by_id = {d["id"]: d for d in rows if d["id"] in ids}
        if all(d["status"] != "building" for d in rows_by_id.values()):
            break
        time.sleep(0.1)
    statuses = sorted(d["status"] for d in rows_by_id.values())
    # 하나는 성공, 겹친 하나는 락에 걸려 failed (에러 메시지 명시)
    assert statuses == ["failed", "running"], rows_by_id
    failed = next(d for d in rows_by_id.values() if d["status"] == "failed")
    assert "in progress" in failed["error"]
