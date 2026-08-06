"""배포가 building 상태로 진행 중일 때도 지금까지의 빌드/설치 로그를 조회할 수
있는지 검증 — GET .../deployments/{id}/build-log. build_log_path는 실제 빌드를
시작하기 전에 미리 레코드에 커밋되므로(services/build.py의 *_log_path 헬퍼,
services/deployer.py의 커밋 순서 참고) 이 조회가 가능하다."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import create_app
from app.models import BuildProfile, Deployment, DeploymentStatus, Project, ProjectType
from app.services import deployer
from app.services.build import BuildResult, docker_build_log_path
from app.services.runtime.base import Endpoint

ADMIN = {"x-api-key": "test-admin-key"}


def _create_project(db: SessionLocal, name: str) -> Project:
    project = Project(name=name, type=ProjectType.python, git_url="https://git.example.com/x")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


# ---- services/deployer.py: docker 분기도 build_image 호출 전에 경로를 커밋한다 ----

def test_deploy_sync_docker_branch_commits_build_log_path_before_build_starts(monkeypatch, tmp_path):
    create_app()
    monkeypatch.setattr(deployer, "checkout", lambda project, git_sha=None: (tmp_path, "e" * 40))
    monkeypatch.setattr(deployer, "get_runtime", lambda: type(
        "R", (), {"start": lambda self, spec: Endpoint(host="127.0.0.1", port=9200)},
    )())
    monkeypatch.setattr(deployer.proxy, "configure", lambda *a, **kw: None)

    db = SessionLocal()
    try:
        project = _create_project(db, "docker-order-app")
        project_id = project.id
        observed = {}

        def fake_build_image(project, workdir, sha, profile):
            reader = SessionLocal()
            try:
                rec = reader.query(Deployment).filter_by(project_id=project_id).one()
                observed["build_log_path"] = rec.build_log_path
                observed["status"] = rec.status
            finally:
                reader.close()
            return BuildResult(
                image_tag=f"{project.name}:{sha[:12]}", internal_port=8000,
                log_path=Path("/tmp/fake.log"), profile=profile,
            )
        monkeypatch.setattr(deployer, "build_image", fake_build_image)

        record = deployer.deploy_sync(db, project, BuildProfile.release)

        assert observed["status"] == DeploymentStatus.building
        expected = str(docker_build_log_path(project.name, "e" * 40, BuildProfile.release))
        assert observed["build_log_path"] == expected
        assert record.build_log_path == expected
    finally:
        db.close()


# ---- API: GET /{project_id}/deployments/{deployment_id}/build-log ----

def test_build_log_endpoint_empty_before_path_is_set():
    c = TestClient(create_app())
    db = SessionLocal()
    try:
        project = _create_project(db, "buildlog-app1")
        record = Deployment(
            project_id=project.id, git_sha="a" * 40, image_tag="", profile=BuildProfile.release,
            status=DeploymentStatus.building,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        pid, did = project.id, record.id
    finally:
        db.close()

    r = c.get(f"/paas/api/v1/projects/{pid}/deployments/{did}/build-log", headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == {"content": "", "done": False}


def test_build_log_endpoint_empty_when_file_not_yet_created(tmp_path):
    c = TestClient(create_app())
    db = SessionLocal()
    try:
        project = _create_project(db, "buildlog-app2")
        log_path = tmp_path / "not-yet.log"
        record = Deployment(
            project_id=project.id, git_sha="a" * 40, image_tag="", profile=BuildProfile.release,
            status=DeploymentStatus.building, build_log_path=str(log_path),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        pid, did = project.id, record.id
    finally:
        db.close()

    r = c.get(f"/paas/api/v1/projects/{pid}/deployments/{did}/build-log", headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == {"content": "", "done": False}


def test_build_log_endpoint_returns_tail_while_building(tmp_path):
    log_path = tmp_path / "progress.log"
    log_path.write_text("\n".join(f"line {i}" for i in range(300)) + "\n", encoding="utf-8")

    c = TestClient(create_app())
    db = SessionLocal()
    try:
        project = _create_project(db, "buildlog-app3")
        record = Deployment(
            project_id=project.id, git_sha="a" * 40, image_tag="", profile=BuildProfile.release,
            status=DeploymentStatus.building, build_log_path=str(log_path),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        pid, did = project.id, record.id
    finally:
        db.close()

    r = c.get(f"/paas/api/v1/projects/{pid}/deployments/{did}/build-log",
              params={"tail": 5}, headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["done"] is False
    assert body["content"].splitlines() == [f"line {i}" for i in range(295, 300)]


def test_build_log_endpoint_done_true_once_terminal(tmp_path):
    log_path = tmp_path / "finished.log"
    log_path.write_text("all done\n", encoding="utf-8")

    c = TestClient(create_app())
    db = SessionLocal()
    try:
        project = _create_project(db, "buildlog-app4")
        record = Deployment(
            project_id=project.id, git_sha="a" * 40, image_tag="buildlog-app4:abc",
            profile=BuildProfile.release, status=DeploymentStatus.running, build_log_path=str(log_path),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        pid, did = project.id, record.id
    finally:
        db.close()

    r = c.get(f"/paas/api/v1/projects/{pid}/deployments/{did}/build-log", headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == {"content": "all done", "done": True}


def test_build_log_endpoint_404_when_deployment_belongs_to_other_project():
    c = TestClient(create_app())
    db = SessionLocal()
    try:
        project_a = _create_project(db, "buildlog-app5")
        project_b = _create_project(db, "buildlog-app6")
        record = Deployment(
            project_id=project_a.id, git_sha="a" * 40, image_tag="", profile=BuildProfile.release,
            status=DeploymentStatus.building,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        pid_b, did = project_b.id, record.id
    finally:
        db.close()

    r = c.get(f"/paas/api/v1/projects/{pid_b}/deployments/{did}/build-log", headers=ADMIN)
    assert r.status_code == 404


def test_build_log_endpoint_404_when_deployment_missing():
    c = TestClient(create_app())
    db = SessionLocal()
    try:
        project = _create_project(db, "buildlog-app7")
        pid = project.id
    finally:
        db.close()

    r = c.get(f"/paas/api/v1/projects/{pid}/deployments/999999/build-log", headers=ADMIN)
    assert r.status_code == 404
