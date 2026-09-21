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


def test_health_exposes_base_domain(monkeypatch, fresh_settings):
    """콘솔이 배포 도메인을 base url 포함 full url로 보여주려면 base_domain이 필요하다."""
    monkeypatch.setenv("PAAS_BASE_DOMAIN", "deploy.example.com")
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import create_app

    body = TestClient(create_app()).get("/paas/health").json()
    assert body["base_domain"] == "deploy.example.com"


# --- 시스템 상태는 OS 능력이 아니라 실제 구성을 말해야 한다(services/monitor) ---


def test_runtime_name_follows_tier_before_backend(monkeypatch, fresh_settings):
    """enterprise 티어는 PAAS_RUNTIME_BACKEND와 무관하게 k8s다 — 화면이 이걸 놓치면
    실제로 도는 런타임과 다른 것을 말한다."""
    from app.services import deployer

    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    get_settings.cache_clear()
    assert deployer.runtime_name() == "windows_service"

    monkeypatch.setenv("PAAS_TIER", "enterprise")
    get_settings.cache_clear()
    assert deployer.runtime_name() == "k8s"


def test_status_reports_the_configured_runtime_and_proxy(monkeypatch, fresh_settings):
    """회귀: Windows면 런타임·프록시와 무관하게 늘 Docker 안내와 'GPU 지원'이 떴다.

    windows_service 런타임은 정의상 Docker를 쓰지 않고(nssm 네이티브 프로세스), GPU
    배정이라는 개념도 없다 — RuntimeSpec.gpu를 읽지도 않는다. 그런데도 Docker 라이선스
    안내와 GPU 지원이 표시돼, 바로 아래 "GPU 없음"과 자기모순을 이뤘다.
    """
    from app.services import monitor

    monkeypatch.setenv("PAAS_HOST_OS", "windows")
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    monkeypatch.setenv("PAAS_PROXY_BACKEND", "iis")
    get_settings.cache_clear()

    data = monitor.snapshot()
    assert data["host_os"] == "windows"
    assert data["runtime_backend"] == "windows_service"
    assert data["proxy_backend"] == "iis"
    assert data["gpu_supported"] is False  # 이 런타임은 GPU를 배정하지 않는다
    assert data["docker_hint"] == ""  # Docker를 쓰지 않으니 라이선스 안내도 없다


def test_status_keeps_docker_hint_and_gpu_for_the_docker_runtime(monkeypatch, fresh_settings):
    from app.services import monitor

    monkeypatch.setenv("PAAS_HOST_OS", "windows")
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "docker")
    get_settings.cache_clear()

    data = monitor.snapshot()
    assert data["runtime_backend"] == "docker"
    assert data["gpu_supported"] is True
    assert "Docker Desktop" in data["docker_hint"]


def test_status_gpu_still_respects_the_os_ceiling(monkeypatch, fresh_settings):
    """OS 능력은 여전히 상한이다 — macOS는 Docker 런타임이어도 GPU 컨테이너를 못 쓴다."""
    from app.services import monitor

    monkeypatch.setenv("PAAS_HOST_OS", "macos")
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "docker")
    get_settings.cache_clear()
    assert monitor.snapshot()["gpu_supported"] is False


def test_preflight_reports_powershell_separately(monkeypatch, fresh_settings):
    """PTY와 powershell.exe는 다른 신호다 — 화면이 못 하는 일의 버튼을 감추려면 둘을
    따로 알아야 한다. pywinpty가 없어도 PowerShell은 있을 수 있고, 그 반대도 있다."""
    import shutil

    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setattr(shutil, "which", lambda name: "C:/ps.exe")
    body = TestClient(create_app()).get(
        "/paas/api/v1/system/terminal/preflight", headers={"x-api-key": "test-admin-key"},
    ).json()
    assert body["powershell"] is True
    assert "ok" in body and "reason" in body  # PTY 신호는 그대로 남는다

    monkeypatch.setattr(shutil, "which", lambda name: None)
    body = TestClient(create_app()).get(
        "/paas/api/v1/system/terminal/preflight", headers={"x-api-key": "test-admin-key"},
    ).json()
    assert body["powershell"] is False
