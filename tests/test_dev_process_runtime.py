"""개발 배포 런타임 — dev 서버를 맨 프로세스로 띄운다(nssm·docker 없음)."""
import os

import pytest

from app.config import get_settings
from app.models import BuildProfile
from app.services import deployer
from app.services.runtime.base import RuntimeSpec


def test_development_routes_to_the_dev_process_runtime(monkeypatch, fresh_settings):
    """개발 배포는 서비스로 감싸지 않는다 — dev 서버는 이미 HMR로 파일 변경을 반영한다."""
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    monkeypatch.setenv("PAAS_TIER", "small")
    get_settings.cache_clear()
    monkeypatch.setattr(os, "name", "nt")

    assert deployer.uses_dev_process(BuildProfile.development) is True
    # 운영은 검증된 길(nssm)을 그대로 쓴다
    assert deployer.uses_dev_process(BuildProfile.release) is False


def test_enterprise_keeps_the_cluster_runtime(monkeypatch, fresh_settings):
    """k8s에서 개발 배포는 클러스터 안에서 돈다 — 플랫폼 호스트의 프로세스가 아니다."""
    monkeypatch.setenv("PAAS_TIER", "enterprise")
    get_settings.cache_clear()
    monkeypatch.setattr(os, "name", "nt")
    assert deployer.uses_dev_process(BuildProfile.development) is False


def test_non_windows_keeps_the_existing_path(monkeypatch, fresh_settings):
    """실행 스크립트가 start.cmd라 윈도우 전용이다."""
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    get_settings.cache_clear()
    monkeypatch.setattr(os, "name", "posix")
    assert deployer.uses_dev_process(BuildProfile.development) is False


def test_routing_sends_each_profile_to_its_own_runtime(monkeypatch, fresh_settings):
    """회귀 방지: 기동과 조회가 다른 런타임을 보면 화면이 '없는 서비스'를 stopped라고 한다.

    get_runtime()에 프로필 인자를 두는 대신 래퍼가 라우팅하므로, 호출부가 프로필을 실어
    나르지 않아도 start·stop·status·logs가 **같은** 런타임으로 간다.
    """
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    get_settings.cache_clear()
    monkeypatch.setattr(os, "name", "nt")

    calls: list[tuple[str, str]] = []

    class _Fake:
        def __init__(self, label):
            self.label = label

        def start(self, spec):
            calls.append((self.label, "start"))

        def stop(self, name, profile):
            calls.append((self.label, "stop"))

        def status(self, name, profile):
            calls.append((self.label, "status"))
            return "running"

        def logs(self, name, profile, tail=200):
            calls.append((self.label, "logs"))
            return ""

    runtime = deployer.ProfileRoutingRuntime()
    monkeypatch.setattr(runtime, "_for",
                        lambda profile: _Fake("dev" if profile == BuildProfile.development
                                              else "prod"))

    runtime.start(RuntimeSpec("p", "", 0, BuildProfile.development, ""))
    runtime.stop("p", BuildProfile.development)
    runtime.status("p", BuildProfile.development)
    runtime.logs("p", BuildProfile.development)
    runtime.start(RuntimeSpec("p", "", 0, BuildProfile.release, ""))

    assert calls == [
        ("dev", "start"), ("dev", "stop"), ("dev", "status"), ("dev", "logs"),
        ("prod", "start"),
    ]


def test_start_requires_the_generated_script(monkeypatch, fresh_settings, tmp_path):
    """start.cmd가 없으면 무엇이 없는지 말하고 멈춘다 — 실행 명령을 여기서 새로 만들지
    않는다(타입별 규칙이 두 곳에 있으면 갈라진다)."""
    from app.services.runtime.dev_process_runtime import DevProcessError, DevProcessRuntime

    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "ws"))
    get_settings.cache_clear()
    spec = RuntimeSpec("demo", "", 0, BuildProfile.development, "", host_port=8123)
    with pytest.raises(DevProcessError, match="start.cmd"):
        DevProcessRuntime().start(spec)


def test_start_requires_an_allocated_port(monkeypatch, fresh_settings, tmp_path):
    """포트를 런타임이 직접 고르면 동시 배포가 같은 포트를 집는다(대장은 services/ports)."""
    from app.services.runtime.dev_process_runtime import DevProcessError, DevProcessRuntime

    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "ws"))
    get_settings.cache_clear()
    workdir = tmp_path / "ws" / "demo"
    workdir.mkdir(parents=True)
    (workdir / "start.cmd").write_text("@echo off\n", encoding="utf-8")
    spec = RuntimeSpec("demo", "", 0, BuildProfile.development, "", host_port=None)
    with pytest.raises(DevProcessError, match="호스트 포트"):
        DevProcessRuntime().start(spec)


def test_status_is_stopped_without_a_live_pid(monkeypatch, fresh_settings, tmp_path):
    """이 런타임은 크래시를 복구하지 않는다 — 죽었는데 running이라고 말하면 더 나쁘다."""
    from app.services.runtime import dev_process_runtime as dev

    monkeypatch.setenv("PAAS_BUILD_LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    runtime = dev.DevProcessRuntime()
    assert runtime.status("demo", BuildProfile.development) == "stopped"

    # 죽은 PID가 적힌 파일이 남아 있는 것이 정상 상황이다
    unit = RuntimeSpec("demo", "", 0, BuildProfile.development, "").unit_name
    dev._pid_path(unit).write_text("999999", encoding="utf-8")
    monkeypatch.setattr(dev, "_pid_alive", lambda pid: False)
    assert runtime.status("demo", BuildProfile.development) == "stopped"

    monkeypatch.setattr(dev, "_pid_alive", lambda pid: True)
    assert runtime.status("demo", BuildProfile.development) == "running"


def test_stop_removes_the_pid_file_even_if_the_process_is_gone(
    monkeypatch, fresh_settings, tmp_path,
):
    """PID 파일이 남으면 다음 status가 죽은 프로세스를 살아 있다고 볼 여지가 생긴다."""
    from app.services.runtime import dev_process_runtime as dev

    monkeypatch.setenv("PAAS_BUILD_LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    unit = RuntimeSpec("demo", "", 0, BuildProfile.development, "").unit_name
    dev._pid_path(unit).write_text("999999", encoding="utf-8")
    monkeypatch.setattr(dev, "_pid_alive", lambda pid: False)

    dev.DevProcessRuntime().stop("demo", BuildProfile.development)
    assert not dev._pid_path(unit).exists()


def test_creation_flags_do_not_detach_the_console(monkeypatch):
    """회귀: DETACHED_PROCESS로 띄우면 cmd.exe가 아무것도 실행하지 않고 죽을 수 있다
    (powershell_daemon.run_detached_script에서 같은 함정을 겪었다). breakaway는 paas가
    Job에 묶인 설치본에서 dev 서버가 paas 재시작에 따라 죽지 않게 한다."""
    from app.services.runtime import dev_process_runtime as dev

    monkeypatch.setattr(os, "name", "nt")
    flags = dev._creation_flags()
    assert not (flags & 0x00000008)  # DETACHED_PROCESS
    assert flags & 0x08000000        # CREATE_NO_WINDOW
    assert flags & 0x01000000        # CREATE_BREAKAWAY_FROM_JOB

    monkeypatch.setattr(os, "name", "posix")
    assert dev._creation_flags() == 0
