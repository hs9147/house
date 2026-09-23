"""배포 취소 — **실제로** 취소한다(돌고 있는 설치 프로세스를 끝낸다).

설치(npm ci·pip install)는 배포 시간의 대부분이다. subprocess.run 안에서는 취소를 알 방법이
없어서, "취소"를 눌러도 설치가 다 끝난 뒤에 멈추면 그것은 취소가 아니다. 그래서 Popen으로
띄우고 1초마다 취소 여부를 보고, 요청을 받으면 프로세스를 트리째 끝낸다.
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import BuildProfile, Deployment, DeploymentStatus, Project, ProjectType
from app.services import build as build_module
from app.services import deployer

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


def test_run_cancellable_kills_the_process_when_cancel_is_requested(tmp_path):
    """오래 도는 프로세스를 중간에 끊는다 — 끝날 때까지 기다리면 취소가 아니다."""
    flag = {"cancel": False}
    log = open(tmp_path / "log.txt", "w", encoding="utf-8")
    started = time.monotonic()

    def should_cancel():
        # 첫 확인에서 바로 취소한다(테스트가 프로세스의 수명을 기다리지 않는다).
        flag["cancel"] = True
        return True

    try:
        with pytest.raises(build_module.BuildCancelled):
            build_module.run_cancellable(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=tmp_path, log=log, timeout=120, should_cancel=should_cancel,
            )
    finally:
        log.close()
    assert flag["cancel"]
    # 60초를 기다리지 않았다.
    assert time.monotonic() - started < 20
    assert "취소" in (tmp_path / "log.txt").read_text(encoding="utf-8")


def test_run_cancellable_returns_normally_when_not_cancelled(tmp_path):
    log = open(tmp_path / "ok.txt", "w", encoding="utf-8")
    try:
        done = build_module.run_cancellable(
            [sys.executable, "-c", "print('hi')"],
            cwd=tmp_path, log=log, timeout=60, should_cancel=lambda: False,
        )
    finally:
        log.close()
    assert done.returncode == 0


def test_install_stops_at_cancel_and_the_record_says_cancelled(monkeypatch, tmp_path,
                                                               fresh_settings):
    """취소는 실패가 아니다 — 레코드에 "취소됨"으로 적어 실패 목록에 섞이지 않게 한다."""
    create_app()
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    get_settings.cache_clear()
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    db = SessionLocal()
    try:
        project = Project(name="cancel-me", type=ProjectType.python,
                          git_url="https://git.example.com/x")
        db.add(project)
        db.commit()
        db.refresh(project)
        record = Deployment(project_id=project.id, git_sha="", image_tag="",
                            profile=BuildProfile.release, status=DeploymentStatus.building)
        db.add(record)
        db.commit()
        db.refresh(record)

        monkeypatch.setattr(deployer, "checkout", lambda p, git_sha=None: (tmp_path, "a" * 40))

        # 설치가 시작되면 취소를 요청한다 — 그 안에서 끊기는지 본다.
        def fake_install(workdir, log_path, base_path=None, build=True, should_cancel=None):
            deployer.request_cancel(record.id)
            if should_cancel and should_cancel():
                raise build_module.BuildCancelled("사용자가 배포를 취소했습니다.")
            raise AssertionError("취소 신호가 설치 단계로 전달되지 않았다")

        monkeypatch.setattr(deployer, "install_dependencies", fake_install)

        class _MustNotStart:
            """상태 조회는 배포 전 검사(다른 프로필 충돌)가 부르므로 허용하고, **기동만** 막는다."""

            def start(self, spec):
                raise AssertionError("취소됐는데 런타임을 띄웠다")

            def stop(self, *a): ...
            def status(self, project_name, profile): return "stopped"
            def logs(self, *a, **kw): return ""

        monkeypatch.setattr(deployer, "get_runtime", lambda: _MustNotStart())

        with pytest.raises(deployer.DeployCancelled):
            deployer.deploy_sync(db, project, BuildProfile.release, record=record)

        db.refresh(record)
        assert record.status == DeploymentStatus.failed
        assert record.error.startswith("취소됨")
        assert record.finished_at is not None
    finally:
        deployer.clear_cancel(record.id if "record" in dir() else None)
        db.close()
        get_settings.cache_clear()


def test_cancel_endpoint_rejects_finished_deployments_and_closes_orphans(monkeypatch,
                                                                        tmp_path,
                                                                        fresh_settings):
    """끝난 배포를 "취소"하면 아무 일도 없는데 사용자는 취소한 줄 안다 — 409로 말한다.

    반대로 파이프라인이 이 프로세스에 없으면(플랫폼 재시작) 요청이 닿을 곳이 없으므로 레코드를
    지금 닫는다. 그러지 않으면 화면이 영원히 "빌드 중"이다.
    """
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path))
    get_settings.cache_clear()
    c = TestClient(create_app())
    pid = c.post(f"{API}/projects", json={
        "name": "cancel-api", "type": "python", "git_url": "https://git.example.com/o/c",
    }, headers=ADMIN).json()["id"]

    db = SessionLocal()
    try:
        running = Deployment(project_id=pid, git_sha="", image_tag="",
                             profile=BuildProfile.release, status=DeploymentStatus.building)
        finished = Deployment(project_id=pid, git_sha="", image_tag="",
                              profile=BuildProfile.release, status=DeploymentStatus.running)
        db.add_all([running, finished])
        db.commit()
        running_id, finished_id = running.id, finished.id
    finally:
        db.close()

    bad = c.post(f"{API}/projects/{pid}/deployments/{finished_id}/cancel", headers=ADMIN)
    assert bad.status_code == 409

    res = c.post(f"{API}/projects/{pid}/deployments/{running_id}/cancel", headers=ADMIN)
    assert res.status_code == 200, res.text
    assert res.json()["pipeline_running"] is False
    db = SessionLocal()
    try:
        row = db.get(Deployment, running_id)
        assert row.status == DeploymentStatus.failed
        assert "취소됨" in row.error
    finally:
        db.close()

    assert c.post(f"{API}/projects/{pid}/deployments/999999/cancel",
                  headers=ADMIN).status_code == 404
