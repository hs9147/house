"""release와 development 동시 배포 차단.

1차(small)는 두 프로필이 같은 도메인을 쓰고 경로만 다르다(/apps/{조직}/{프로젝트}/ 와
그 아래 /dev/). 프록시 규칙은 접두사 매칭이라 release 규칙이 dev 경로까지 함께 잡고,
둘이 동시에 떠 있으면 어느 쪽이 응답할지 규칙 순서에 달린다 — 주소가 충돌한다.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.models import BuildProfile
from app.services import deployer

ADMIN = {"x-api-key": "test-admin-key"}


class _Runtime:
    def __init__(self, running: set):
        self.running = running

    def status(self, project_name, profile):
        return "running" if profile in self.running else "stopped"


def _project(c) -> dict:
    return c.post("/paas/api/v1/projects", json={
        "name": "shop", "type": "python", "git_url": "https://git.example.com/o/r.git",
        "branch": "main",
    }, headers=ADMIN).json()


def test_deploy_is_blocked_when_the_other_profile_is_running(monkeypatch, fresh_settings):
    get_settings.cache_clear()
    c = TestClient(create_app())
    project_id = _project(c)["id"]
    monkeypatch.setattr(deployer, "get_runtime", lambda: _Runtime({BuildProfile.release}))

    r = c.post(f"/paas/api/v1/projects/{project_id}/deploy",
               json={"profile": "development"}, headers=ADMIN)
    assert r.status_code == 409, r.text
    # 무엇을 해야 하는지가 메시지에 있어야 한다 — 중지 후 재배포.
    assert "중지" in r.json()["detail"]
    assert "release" in r.json()["detail"]


def test_same_profile_redeploy_is_allowed(monkeypatch, fresh_settings):
    """자기 자신을 다시 배포하는 것은 막지 않는다 — 그건 정상적인 재배포다."""
    get_settings.cache_clear()
    c = TestClient(create_app())
    project = _project(c)
    monkeypatch.setattr(deployer, "get_runtime", lambda: _Runtime({BuildProfile.release}))
    deployer.assert_no_profile_conflict(
        type("P", (), {"name": project["name"], "id": project["id"]})(), BuildProfile.release,
    )  # 예외가 나지 않아야 한다


def test_no_conflict_when_nothing_is_running(monkeypatch, fresh_settings):
    get_settings.cache_clear()
    c = TestClient(create_app())
    project_id = _project(c)["id"]
    monkeypatch.setattr(deployer, "get_runtime", lambda: _Runtime(set()))
    monkeypatch.setattr(deployer, "deploy_queued",
                        lambda db, p, prof, sha=None: type("D", (), {
                            "id": 1, "project_id": p.id, "git_sha": "", "image_tag": "",
                            "profile": prof, "status": "building", "component": None,
                            "created_at": None, "finished_at": None, "error": None,
                            "host_port": None, "internal_port": None, "build_log_path": None,
                        })())
    r = c.post(f"/paas/api/v1/projects/{project_id}/deploy",
               json={"profile": "development"}, headers=ADMIN)
    assert r.status_code != 409, r.text


def test_enterprise_tier_has_no_conflict(monkeypatch, fresh_settings):
    """2차는 프로필마다 도메인이 갈리므로 경로가 겹치지 않는다."""
    monkeypatch.setenv("PAAS_TIER", "enterprise")
    get_settings.cache_clear()
    monkeypatch.setattr(deployer, "get_runtime", lambda: _Runtime({BuildProfile.release}))
    deployer.assert_no_profile_conflict(
        type("P", (), {"name": "shop", "id": 1})(), BuildProfile.development,
    )  # 예외 없음


def test_unknown_runtime_state_does_not_block(monkeypatch, fresh_settings):
    """상태를 못 읽는 것을 충돌로 처리하면, 런타임 조회가 잠깐 실패했다는 이유로
    멀쩡한 배포가 막힌다. 이 가드는 주소 충돌을 줄이려는 것이지 안전 필수 불변식이 아니다."""
    class _Broken:
        def status(self, *a):
            raise RuntimeError("docker 없음")

    get_settings.cache_clear()
    monkeypatch.setattr(deployer, "get_runtime", lambda: _Broken())
    deployer.assert_no_profile_conflict(
        type("P", (), {"name": "shop", "id": 1})(), BuildProfile.development,
    )  # 예외 없이 통과해야 한다
