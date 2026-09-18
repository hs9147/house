import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.security import require_admin
from app.services import selfrestart

client = TestClient(app, raise_server_exceptions=False)


def test_restart_backend_service_permissions():
    resp = client.post("/paas/api/v1/system/restart")
    assert resp.status_code in (401, 403)


def _post_restart():
    """관리자로 /system/restart를 호출하고 (응답, 분리 실행된 스크립트)를 돌려준다.

    이 엔드포인트는 2.5초 뒤 os._exit(0)으로 자기 프로세스를 죽이는 타이머를 건다
    (포트를 비워 주기 위해). 테스트에서 진짜로 걸리게 두면 그 2.5초 뒤에 터져
    **pytest 프로세스가 통째로 죽는다** — 전체 스위트를 돌릴 때 이 파일 다음 테스트들이
    실행 중이라, 요약도 실패도 못 남기고 종료 코드 0으로 끝나 "전부 통과"처럼 보였다.
    """
    mock_admin = MagicMock()
    mock_admin.name = "admin"
    app.dependency_overrides[require_admin] = lambda: mock_admin
    app.dependency_overrides[get_db] = lambda: MagicMock()
    try:
        with patch("app.services.powershell_daemon.run_detached_script") as run_detached, \
                patch("app.audit.record"), patch("threading.Timer") as mock_timer:
            resp = client.post("/paas/api/v1/system/restart")
            assert run_detached.called
            # 타이머를 거는 것 자체는 이 엔드포인트의 약속이므로 걸렸는지는 확인한다
            assert mock_timer.call_args.args[0] == 2.5
            assert mock_timer.return_value.start.called
            return resp, run_detached.call_args.args[0]
    finally:
        app.dependency_overrides.clear()


def test_restart_uses_the_port_it_is_actually_serving():
    """회귀: 재시작 스크립트가 포트를 추측하면 백엔드가 다른 자리에서 살아난다.

    예전에는 8000이 박혀 있었다 — 플랫폼은 7000에서 서비스하므로(Dockerfile·배포가이드·
    콘솔 프록시), 이 엔드포인트를 부르면 현재 프로세스는 죽고 새 프로세스는 8000에 떠서
    콘솔·IIS가 보는 자리에서 백엔드가 사라졌다.
    """
    resp, script = _post_restart()
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "restarting"

    # TestClient가 만든 요청 소켓의 포트가 그대로 쓰여야 한다(scope["server"]).
    port = data["port"]
    assert isinstance(port, int) and port > 0
    assert f"-Port {port}" in script
    assert "8000" not in script


def test_restart_never_kills_whoever_holds_the_port():
    """회귀: 예전 스크립트는 포트를 점유한 프로세스를 무조건 Stop-Process -Force 했다.

    그 포트를 쓰는 것이 우리가 아닐 수 있다 — 자기 프로세스는 엔드포인트가 os._exit로
    직접 빠지므로 남의 프로세스를 죽일 이유가 없다.
    """
    _resp, script = _post_restart()
    assert "Stop-Process" not in script
    assert "Get-NetTCPConnection" not in script


def test_restart_runs_the_same_helper_the_terminal_uses():
    """정책은 infra/restart-paas.ps1 한 곳에 있다.

    예전에는 엔드포인트가 PowerShell을 따로 생성해 정책이 두 벌이었고, 그쪽에는 로그가
    없어 분리된 프로세스가 실패해도 알 수 없었다. -Now는 "이미 분리된 프로세스 안이니
    다시 분리하지 말라"는 뜻이다.
    """
    _resp, script = _post_restart()
    assert "restart-paas.ps1" in script
    assert "-Now" in script
    # 서비스명은 헬퍼에 그대로 넘어가야 한다(설치 환경마다 다르다)
    assert "-Service @('paas','paas-console')" in script


def test_restart_passes_the_configured_host_and_this_interpreter():
    from app.config import get_settings

    _resp, script = _post_restart()
    assert f"-BindHost '{get_settings().bind_host}'" in script
    # "어느 venv인가"의 유일하게 확실한 답 — 지금 도는 프로세스의 인터프리터
    assert f"-Python '{sys.executable}'" in script


def test_helper_script_covers_both_restart_modes():
    """헬퍼가 서비스 재시작과 uvicorn 재기동 두 경로를 모두 들고 있어야 한다."""
    helper = Path(__file__).resolve().parent.parent / selfrestart.HELPER_SCRIPT
    body = helper.read_text(encoding="utf-8")
    assert "Restart-Service" in body  # 서비스로 등록된 설치본(nssm)
    assert "uvicorn app.main:app" in body  # 등록되지 않은 설치본(로컬 개발)
    # 콘솔이 읽을 수 있게 로그를 남긴다 — 분리된 프로세스는 stdout이 어디에도 닿지 않는다
    assert "restart-paas.log" in body
    # infra/*.ps1은 ASCII만 쓴다(cp949 환경에서 깨지면 정작 장애 때 못 읽는다)
    assert body.isascii()


# --- selfrestart 서비스 단위 — 플랫폼별 스크립트 생성 ---


def _script(**kwargs):
    base = {"services": ["paas"], "host": "127.0.0.1", "port": 7000,
            "repo_root": "C:/paas", "python_exe": "C:/py/python.exe"}
    return selfrestart.restart_script(**{**base, **kwargs})


def test_windows_script_quotes_paths_with_apostrophes():
    script = _script(repo_root="C:/it's here")
    assert "'C:/it''s here'" in script


def test_posix_script_uses_systemctl(monkeypatch):
    """리눅스 확장 — 유닛이 있으면 systemctl, 없으면 uvicorn 직접 기동."""
    monkeypatch.setattr(selfrestart.sys, "platform", "linux")
    script = _script(services=["paas", "paas-console"])
    assert "systemctl restart" in script
    assert "Restart-Service" not in script
    assert "uvicorn app.main:app --host 127.0.0.1 --port 7000" in script
    assert "paas paas-console" in script  # 안전한 이름은 shlex가 그대로 둔다


def test_posix_script_does_not_let_names_break_out_of_the_shell(monkeypatch):
    monkeypatch.setattr(selfrestart.sys, "platform", "linux")
    script = _script(services=["paas; rm -rf /"])
    assert "rm -rf" in script  # 이름 자체는 실려 있지만
    assert "'paas; rm -rf /'" in script  # 한 낱말로 인용돼 명령이 되지 않는다


def test_bound_port_reads_the_socket_not_the_host_header():
    # IIS 뒤에서 공개 도메인으로 들어와도 실제 바인딩 포트가 나와야 한다
    assert selfrestart.bound_port({"server": ("127.0.0.1", 7000)}) == 7000
    # 유닉스 소켓(--uds) 기동은 포트가 없다
    assert selfrestart.bound_port({"server": None}) is None
    assert selfrestart.bound_port({}) is None
