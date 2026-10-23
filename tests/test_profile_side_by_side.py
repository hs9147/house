"""release와 development는 동시에 떠 있을 수 있다.

예전에는 development 경로가 release 안(/{프로젝트}/dev/)이라 release 규칙이 dev 요청까지 잡아 둘을 동시에
띄우지 못하게 막았다. 지금은 경로가 형제(~dev)이고 포트·유닛 이름이 프로필마다 따로라 그 이유가 없다. 남은
겹침은 배포 폴더였다 — 같은 폴더면 한쪽 배포가 다른 쪽이 실행 중인 폴더를 다른 커밋으로 되돌리고, start.cmd를
덮어써 release 서비스가 재시작될 때 development 스크립트로 뜬다. 그래서 폴더도 프로필마다 따로다.
"""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.models import BuildProfile
from app.services import build, deployer
from app.services.runtime.base import RuntimeSpec

ADMIN = {"x-api-key": "test-admin-key"}


class _Runtime:
    def __init__(self, running: set):
        self.running = running

    def status(self, project_name, profile):
        return "running" if profile in self.running else "stopped"


def test_development_deploys_while_release_is_running(monkeypatch, fresh_settings):
    get_settings.cache_clear()
    c = TestClient(create_app())
    project_id = c.post("/paas/api/v1/projects", json={
        "name": "shop", "type": "python", "git_url": "https://git.example.com/o/r.git", "branch": "main",
    }, headers=ADMIN).json()["id"]
    monkeypatch.setattr(deployer, "get_runtime", lambda: _Runtime({BuildProfile.release}))
    monkeypatch.setattr(deployer, "deploy_queued",
                        lambda db, p, prof, sha=None: type("D", (), {
                            "id": 1, "project_id": p.id, "git_sha": "", "image_tag": "",
                            "profile": prof, "status": "building", "component": None,
                            "created_at": None, "finished_at": None, "error": None,
                            "host_port": None, "internal_port": None, "build_log_path": None,
                        })())
    r = c.post(f"/paas/api/v1/projects/{project_id}/deploy", json={"profile": "development"}, headers=ADMIN)
    # release가 떠 있어도 막지 않는다(예전에는 409 「release 프로필이 이미 떠 있습니다」)
    assert r.status_code != 409, r.text
    assert "이미 떠 있습니다" not in r.text


def test_each_profile_has_its_own_deploy_folder(fresh_settings):
    """폴더 이름은 공개 경로의 규칙(~dev)과 같다 — 런타임도 그 폴더에서 start.cmd를 찾는다."""
    release = build.deploy_dir("shop", BuildProfile.release)
    dev = build.deploy_dir("shop", BuildProfile.development)
    assert release.name == "shop" and dev.name == "shop~dev" and release.parent == dev.parent

    from app.services.runtime.dev_process_runtime import DevProcessRuntime, DevProcessError

    spec = RuntimeSpec(project_name="shop", image_tag="", internal_port=0,
                       profile=BuildProfile.development, domain="x")
    spec.host_port = 18999
    try:
        DevProcessRuntime().start(spec)
    except DevProcessError as e:
        assert "shop~dev" in str(e)   # development는 ~dev 폴더의 start.cmd를 찾는다
