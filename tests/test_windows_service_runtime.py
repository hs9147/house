"""windows_service 런타임 — Docker 없이 nssm으로 Windows Service 블루-그린 배포."""
import subprocess

import pytest

from app.config import get_settings
from app.models import BuildProfile
from app.services.runtime import windows_service_runtime as wsr
from app.services.runtime.base import RuntimeSpec
from app.services.runtime.windows_service_runtime import WindowsServiceError, WindowsServiceRuntime


def _spec(project_name="shop") -> RuntimeSpec:
    return RuntimeSpec(project_name, "", 8000, BuildProfile.release, "shop.apps.test")


class _FakeServices:
    """sc query/nssm install/remove를 흉내내는 상태 저장소."""

    def __init__(self):
        self.installed: set[str] = set()
        self.calls: list[list[str]] = []

    def run(self, args, **kwargs):
        self.calls.append(args)
        cmd, sub = args[0], args[1]
        if cmd == "sc" and sub == "query":
            name = args[2]
            if name in self.installed:
                return _Result(0, "STATE : RUNNING")
            return _Result(1, "")
        if "nssm" in cmd:
            if sub == "install":
                self.installed.add(args[2])
            elif sub == "remove":
                self.installed.discard(args[2])
            return _Result(0, "")
        return _Result(0, "")


class _Result:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


@pytest.fixture
def env(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("PAAS_BUILD_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PAAS_PORT_RANGE_START", "9100")
    monkeypatch.setenv("PAAS_PORT_RANGE_END", "9199")
    get_settings.cache_clear()
    settings = get_settings()
    workdir = settings.work_dir / "shop"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "start.cmd").write_text("echo start\n", encoding="utf-8")

    fake = _FakeServices()
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(wsr.WindowsServiceRuntime, "_wait_healthy", lambda self, *a, **kw: True)
    return fake


def test_start_requires_start_script(tmp_path, monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "work"))
    get_settings.cache_clear()
    with pytest.raises(WindowsServiceError, match="start.cmd"):
        WindowsServiceRuntime().start(_spec())


def test_start_registers_first_slot_a(env):
    endpoint = WindowsServiceRuntime().start(_spec())
    assert endpoint.host == "127.0.0.1"
    assert 9100 <= endpoint.port <= 9199
    assert "paas-shop-a" in env.installed
    install_calls = [c for c in env.calls if c[1] == "install"]
    assert install_calls and install_calls[0][2] == "paas-shop-a"


def test_start_passes_host_env_for_loopback_only_binding(env):
    """앱이 HOST를 지키면 방화벽 없이도 외부에서 직접 접근되지 않는다 — 단일 외부 포트(프록시)
    강제의 일부(defense-in-depth, 완전한 보장은 아님 — 클래스 docstring 참고)."""
    WindowsServiceRuntime().start(_spec())
    set_env_calls = [c for c in env.calls if c[1] == "set" and c[3] == "AppEnvironmentExtra"]
    assert set_env_calls
    assert "HOST=127.0.0.1" in set_env_calls[0][4]


def test_start_blue_green_switches_slot_and_tears_down_old(env):
    WindowsServiceRuntime().start(_spec())
    assert "paas-shop-a" in env.installed

    WindowsServiceRuntime().start(_spec())
    assert "paas-shop-b" in env.installed
    assert "paas-shop-a" not in env.installed  # 구 슬롯 정리됨


def test_stop_removes_all_slots(env):
    WindowsServiceRuntime().start(_spec())
    assert env.installed
    WindowsServiceRuntime().stop("shop", BuildProfile.release)
    assert not env.installed


def test_status_reports_running_then_stopped(env):
    assert WindowsServiceRuntime().status("shop", BuildProfile.release) == "stopped"
    WindowsServiceRuntime().start(_spec())
    assert WindowsServiceRuntime().status("shop", BuildProfile.release) == "running"


def test_health_check_failure_tears_down_and_raises(env, monkeypatch):
    monkeypatch.setattr(wsr.WindowsServiceRuntime, "_wait_healthy", lambda self, *a, **kw: False)
    with pytest.raises(WindowsServiceError, match="health check failed"):
        WindowsServiceRuntime().start(_spec())
    assert not env.installed  # 실패한 신규 슬롯도 정리됨


def test_missing_nssm_binary_raises_clear_error(tmp_path, monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "work"))
    get_settings.cache_clear()
    workdir = get_settings().work_dir / "shop"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "start.cmd").write_text("echo start\n", encoding="utf-8")

    def boom(args, **kw):
        if args[0] == get_settings().nssm_path:
            raise FileNotFoundError("no such file")
        return _Result(1, "")  # sc query: 서비스 없음

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(WindowsServiceError, match="nssm"):
        WindowsServiceRuntime().start(_spec())
