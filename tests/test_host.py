"""운영환경(OS) 감지·기능 매트릭스·GPU 가드 검증."""
import pytest

from app.config import get_settings
from app.models import BuildProfile
from app.services import host
from app.services.runtime.base import RuntimeSpec
from app.services.runtime.docker_runtime import DockerRuntime


@pytest.mark.parametrize(
    ("system", "expected"),
    [("Linux", "linux"), ("Darwin", "macos"), ("Windows", "windows")],
)
def test_detect_host_os(monkeypatch, system, expected):
    monkeypatch.setattr(host._platform, "system", lambda: system)
    assert host.detect_host_os() == expected


def test_host_os_override_beats_detection(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_HOST_OS", "macos")
    get_settings.cache_clear()
    monkeypatch.setattr(host._platform, "system", lambda: "Linux")
    assert host.get_host_caps().os == "macos"


def test_capability_matrix(monkeypatch, fresh_settings):
    for os_name, gpu in [("linux", True), ("macos", False), ("windows", True)]:
        monkeypatch.setenv("PAAS_HOST_OS", os_name)
        get_settings.cache_clear()
        caps = host.get_host_caps()
        assert caps.os == os_name
        assert caps.gpu_supported is gpu
        assert caps.docker_hint


def test_force_gpu_escape_hatch(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_HOST_OS", "macos")
    get_settings.cache_clear()
    assert host.gpu_allowed() is False
    monkeypatch.setenv("PAAS_FORCE_GPU", "true")
    get_settings.cache_clear()
    assert host.gpu_allowed() is True


def test_docker_runtime_rejects_gpu_on_macos(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_HOST_OS", "macos")
    get_settings.cache_clear()
    spec = RuntimeSpec("llm-app", "llm-app:abc", 8000, BuildProfile.release, "x.apps.test", gpu=True)
    with pytest.raises(RuntimeError, match="GPU 컨테이너를 지원하지 않습니다"):
        DockerRuntime().start(spec)  # docker 클라이언트 생성 전에 조기 실패해야 함


def test_health_exposes_host_os(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_HOST_OS", "windows")
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import create_app

    body = TestClient(create_app()).get("/paas/health").json()
    assert body["host_os"] == "windows"
    assert "features" in body
    assert body["gitea_url"] is None  # 미설정 시 콘솔이 메뉴를 숨길 수 있도록 null


def test_health_exposes_gitea_url_when_configured(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import create_app

    body = TestClient(create_app()).get("/paas/health").json()
    assert body["gitea_url"] == "https://git.example.com"
