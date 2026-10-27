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


# --- 주요 점유 항목 — 누가 CPU·메모리·디스크를 쓰는가(services/monitor) ---

class _FakePsutil:
    """psutil 대역 — 프로세스마다 (이름, 작업 폴더 또는 예외, 두 번째 CPU 값, RSS)."""

    class Error(Exception):
        pass

    class AccessDenied(Error):
        pass

    class NoSuchProcess(Error):
        pass

    def __init__(self, procs, partitions=()):
        self.procs, self.partitions = procs, list(partitions)

    def process_iter(self, attrs):
        fake = self

        class P:
            def __init__(self, pid, name, cwd, cpu, rss):
                self.pid, self.info, self._cwd, self._cpu, self._rss, self._n = pid, {"name": name}, cwd, cpu, rss, 0

            def cpu_percent(self, interval):
                self._n += 1
                if self._cpu is None and self._n > 1:
                    raise fake.NoSuchProcess()
                return 0.0 if self._n == 1 else self._cpu

            def memory_info(self):
                return type("M", (), {"rss": self._rss})()

            def cwd(self):
                if isinstance(self._cwd, Exception):
                    raise self._cwd
                return str(self._cwd)

        return [P(*p) for p in self.procs]

    def cpu_percent(self, interval=None):
        return 37.5

    def cpu_count(self):
        return 4

    def virtual_memory(self):
        return type("V", (), {"total": 16, "used": 8, "percent": 50.0})()

    def disk_usage(self, path):
        if path == "Z:\\":
            raise OSError("not ready")
        return type("D", (), {"total": 100, "used": 91 if path == "D:\\" else 40,
                              "percent": 91.0 if path == "D:\\" else 40.0})()

    def disk_partitions(self, all=False):
        return [type("P", (), p)() for p in self.partitions]


def test_top_consumers_are_grouped_by_who_uses_them(monkeypatch, fresh_settings, tmp_path):
    """같은 앱의 node·python 여러 개는 그 앱 하나로 — 배포 폴더(work_dir/{프로젝트})로 안다. 플랫폼은 저장소
    폴더로, 그 밖은 프로세스 이름으로. CPU는 코어 수로 나눠 전체 대비다. 유휴 프로세스(pid 0)는 뺀다."""
    import sys
    from app.services import monitor

    work, repo = tmp_path / "repo" / "data" / "workspaces", tmp_path / "repo"
    (work / "supplier-pool" / "frontend").mkdir(parents=True)
    (work / "supplier-pool~dev").mkdir()
    monkeypatch.setenv("PAAS_WORK_DIR", str(work))
    monkeypatch.setenv("PAAS_REPO_ROOT", str(repo))
    get_settings.cache_clear()
    fake = _FakePsutil([
        (0, "System Idle Process", RuntimeError("x"), 300.0, 0),
        (11, "node.exe", work / "supplier-pool" / "frontend", 80.0, 300),
        (12, "python.exe", work / "supplier-pool", 40.0, 200),
        (13, "node.exe", work / "supplier-pool~dev", 8.0, 100),
        (14, "python.exe", repo, 20.0, 900),
        (15, "MsMpEng.exe", _FakePsutil.AccessDenied(), 12.0, 50),
        (16, "gone.exe", repo, None, 10),
        (17, "idle.exe", tmp_path, 0.0, 1),
    ], partitions=[{"mountpoint": "C:\\", "fstype": "NTFS", "opts": "rw"},
                   {"mountpoint": "D:\\", "fstype": "NTFS", "opts": "rw"},
                   {"mountpoint": "E:\\", "fstype": "", "opts": "cdrom"},
                   {"mountpoint": "Z:\\", "fstype": "NTFS", "opts": "rw"}])
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(monitor, "SAMPLE_SECONDS", 0)
    monkeypatch.setattr(monitor, "folder_sizes", lambda: {"items": [], "measured_at": None, "measuring": True})

    data = monitor.snapshot()
    top = data["top"]
    assert data["cpu_percent"] == 37.5
    assert top["cpu"][0] == {"kind": "app", "label": "supplier-pool", "cpu": 30.0, "memory": 500, "count": 2}
    assert [(r["label"], r["cpu"]) for r in top["cpu"]] == [
        ("supplier-pool", 30.0), ("플랫폼", 5.0), ("MsMpEng.exe", 3.0), ("supplier-pool~dev", 2.0)]
    assert [r["label"] for r in top["memory"]][:2] == ["플랫폼", "supplier-pool"]
    assert all(r["label"] != "System Idle Process" for r in top["memory"])
    # 드라이브는 사용률 높은 것부터 — 빈 드라이브(CD)·준비 안 된 드라이브는 뺀다
    assert [d["mount"] for d in top["disks"]] == ["D:\\", "C:\\"]


def test_folder_sizes_are_measured_in_the_background_and_cached(monkeypatch, fresh_settings, tmp_path):
    """폴더 크기는 재는 데 오래 걸린다 — 첫 요청은 기다리지 않고 "재는 중", 다음부터 잰 값(앱 폴더마다)."""
    import time
    from app.services import monitor

    work = tmp_path / "ws"
    (work / "big-app").mkdir(parents=True)
    (work / "big-app" / "a.bin").write_bytes(b"x" * 5000)
    (work / "small-app").mkdir()
    (work / "small-app" / "b.txt").write_bytes(b"y" * 10)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "x.log").write_bytes(b"z" * 700)
    monkeypatch.setenv("PAAS_WORK_DIR", str(work))
    monkeypatch.setenv("PAAS_BUILD_LOG_DIR", str(logs))
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "none"))
    get_settings.cache_clear()
    monkeypatch.setattr(monitor, "_folders", {"at": 0.0, "items": [], "measured_at": None, "running": False})

    first = monitor.folder_sizes()
    assert first["measuring"] is True and first["items"] == []
    for _ in range(100):
        if not monitor._folders["running"]:
            break
        time.sleep(0.05)
    out = monitor.folder_sizes()
    assert out["measuring"] is False and out["measured_at"]
    sizes = [(i["kind"], i["label"], i["bytes"]) for i in out["items"]]
    assert sizes[:3] == [("app", "big-app", 5000), ("platform", "빌드·앱 로그", 700), ("app", "small-app", 10)]
    assert all(i["complete"] for i in out["items"])
    # 상한을 넘으면 다 재지 못했다고 표시한다
    monkeypatch.setattr(monitor, "MAX_ENTRIES", 0)
    assert monitor._du(work) == (0, False)
