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
                if args[2] in self.installed:
                    return _Result(1, "service already exists")  # 실제 nssm 동작
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
    assert endpoint.host == wsr.UPSTREAM_HOST == "localhost"
    assert 9100 <= endpoint.port <= 9199
    assert "paas-shop-a" in env.installed
    install_calls = [c for c in env.calls if c[1] == "install"]
    assert install_calls and install_calls[0][2] == "paas-shop-a"


def test_start_passes_host_env_for_loopback_only_binding(env):
    """앱이 HOST를 지키면 방화벽 없이도 외부에서 직접 접근되지 않는다 — 단일 외부 포트(프록시)
    강제의 일부(defense-in-depth, 완전한 보장은 아님 — 클래스 docstring 참고).

    프록시가 붙는 이름(Endpoint.host)과 앱이 듣는 이름(HOST)은 반드시 같아야 한다 —
    한쪽만 localhost로 두면 Windows에서 ::1로 먼저 풀려 어긋나고 502가 난다.
    """
    endpoint = WindowsServiceRuntime().start(_spec())
    set_env_calls = [c for c in env.calls if c[1] == "set" and c[3] == "AppEnvironmentExtra"]
    assert set_env_calls
    assert f"HOST={wsr.UPSTREAM_HOST}" in set_env_calls[0][4]
    assert endpoint.host == wsr.UPSTREAM_HOST


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


def test_start_clears_leftover_service_in_target_slot(env):
    """이전 배포가 중간에 끊겨 두 슬롯이 모두 남은 상태에서도 배포가 돼야 한다.

    치우지 않으면 nssm install이 "이미 있음"으로 실패하고, 사람이 손으로 지울 때까지
    이후 모든 배포가 같은 자리에서 막힌다 — 한 번의 사고가 영구 고장이 된다.
    """
    WindowsServiceRuntime().start(_spec())          # a 슬롯 사용 중
    env.installed.add("paas-shop-b")                # 실패한 배포가 남긴 찌꺼기

    WindowsServiceRuntime().start(_spec())

    assert env.installed == {"paas-shop-b"}         # b로 교체되고 구 슬롯 a는 정리됨
    removes = [c[2] for c in env.calls if c[1] == "remove"]
    assert removes.count("paas-shop-b") == 1        # 설치 전에 찌꺼기를 치웠다
    assert "paas-shop-a" in removes


def test_every_sc_and_nssm_call_has_a_timeout(env, monkeypatch):
    """요청 경로에서 불리는 호출이라 타임아웃이 없으면 멈춘 sc/nssm 하나가 Starlette
    스레드풀 슬롯을 영원히 잡는다. 슬롯이 마르면 같은 풀을 쓰는 동기 엔드포인트가 전부
    대기하고, PowerShell 콘솔(POST /system/powershell/exec)이 "명령어 실행 중"에서
    멈춘다 — 실제로 겪은 증상이라 구조로 고정한다.
    """
    seen: list[tuple[list, dict]] = []
    inner = env.run

    def _record(args, **kwargs):
        seen.append((args, kwargs))
        return inner(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _record)

    runtime = WindowsServiceRuntime()
    runtime.start(_spec())
    runtime.status("shop", BuildProfile.release)
    runtime.stop("shop", BuildProfile.release)
    wsr.list_registered_services()

    assert seen, "아무 호출도 관찰되지 않았다"
    missing = [args for args, kwargs in seen if kwargs.get("timeout") is None]
    assert not missing, f"타임아웃 없는 호출: {missing}"


def test_hung_sc_does_not_block_status(env, monkeypatch):
    """멈춘 sc는 예외로 끝나야 한다 — 그 자리에서 계속 기다리면 안 된다."""
    def _hang(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", _hang)
    assert WindowsServiceRuntime().status("shop", BuildProfile.release) == "stopped"
    assert wsr.list_registered_services() == []


def test_start_passes_profile_and_base_path(env):
    """start.cmd가 dev 서버로 띄울지 판단하고, dev 서버에 줄 base를 얻는 경로다."""
    spec = RuntimeSpec("shop", "", 8000, BuildProfile.development, "shop.apps.test")
    spec.base_path = "/apps/org/shop/dev/"
    WindowsServiceRuntime().start(spec)
    env_arg = [c for c in env.calls if c[1] == "set" and c[3] == "AppEnvironmentExtra"][0][4]
    assert "PAAS_PROFILE=development" in env_arg
    assert "PAAS_BASE_PATH=/apps/org/shop/dev/" in env_arg
