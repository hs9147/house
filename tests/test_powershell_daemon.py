"""상주 PowerShell 데몬 — 세션 유지·종료코드 캡처, /exec 호출 간 세션 유지.

powershell.exe가 있는 환경(주로 Windows)에서만 실제 실행을 검증한다.
"""
import shutil

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ADMIN = {"x-api-key": "test-admin-key"}

_no_powershell = shutil.which("powershell.exe") is None
skip_no_ps = pytest.mark.skipif(_no_powershell, reason="powershell.exe 미존재 (비-Windows)")


@skip_no_ps
def test_session_state_persists_across_commands():
    from app.services.powershell_daemon import PowerShellDaemon

    d = PowerShellDaemon()
    try:
        d.run("$paas_test_var = 4242")
        res = d.run("Write-Output $paas_test_var")
        assert "4242" in res.output  # 같은 프로세스라 변수가 유지된다
    finally:
        d.stop()
    assert d.alive is False


@skip_no_ps
def test_returncode_captured():
    from app.services.powershell_daemon import PowerShellDaemon

    d = PowerShellDaemon()
    try:
        res = d.run("cmd /c exit 5")
        assert res.returncode == 5
    finally:
        d.stop()


@skip_no_ps
def test_exec_endpoint_shares_session(monkeypatch):
    from app.services import powershell_daemon

    monkeypatch.setattr(powershell_daemon, "_shared", None)
    c = TestClient(create_app())
    try:
        r1 = c.post("/paas/api/v1/system/powershell/exec",
                    json={"command": "$paas_ep_var = 'hello-daemon'"}, headers=ADMIN)
        assert r1.status_code == 200, r1.text
        r2 = c.post("/paas/api/v1/system/powershell/exec",
                    json={"command": "Write-Output $paas_ep_var"}, headers=ADMIN)
        assert r2.status_code == 200, r2.text
        assert "hello-daemon" in r2.json()["output"]  # 호출 간 세션 유지
    finally:
        powershell_daemon.shutdown_shared()


def test_exec_requires_command_field():
    """빈 command는 400 (powershell 없이도 검증되는 입력 검사)."""
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/system/powershell/exec", json={"command": "   "}, headers=ADMIN)
    assert r.status_code == 400
