"""SW 업데이트 엔드포인트 — git pull → 콘솔 빌드 → 서비스 재시작(실제 실행은 목킹)."""
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.security import require_admin

client = TestClient(app, raise_server_exceptions=False)


def test_sw_update_requires_admin():
    resp = client.post("/paas/api/v1/system/sw-update")
    assert resp.status_code in (401, 403)


def test_sw_update_schedules_git_pull_and_restart():
    mock_admin = MagicMock()
    mock_admin.name = "admin"
    app.dependency_overrides[require_admin] = lambda: mock_admin
    app.dependency_overrides[get_db] = lambda: MagicMock()
    try:
        with patch("subprocess.Popen") as mock_popen, patch("app.audit.record"):
            resp = client.post("/paas/api/v1/system/sw-update")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["status"] == "updating"
            assert data["services"] == ["paas", "paas-console"]
            assert mock_popen.called
            # 스케줄된 스크립트가 git pull과 Restart-Service를 포함하는지 확인
            script = mock_popen.call_args.args[0][-1]
            assert "git pull" in script
            assert "Restart-Service -Name 'paas'" in script
            assert "Restart-Service -Name 'paas-console'" in script
            # 콘솔은 플랫폼 자신이라 배포 파이프라인이 없다 — 여기서 빌드하지 않으면
            # 의존성이 늘었을 때 예전 dist가 조용히 계속 서빙된다(실제로 겪었다).
            assert "npm install" in script
            assert "npm run build" in script
            # 콘솔 소스나 npm이 없는 설치본에서는 건너뛴다
            assert "Test-Path" in script and "Get-Command npm" in script
            # 빌드가 실패해도 서비스 재시작까지는 간다 — 대신 실패를 말한다
            assert script.index("npm run build") < script.index("Restart-Service")
            assert "콘솔 빌드 실패" in script
            # 분리된 프로세스라 stdout이 어디에도 닿지 않는다 — 로그로 남겨야 읽을 수 있다
            assert "Start-Transcript" in script and "sw-update.log" in script
            # 파이썬 의존성은 여전히 건드리지 않는다(가상환경 위치가 설치본마다 다르다)
            assert "pip install" not in script
    finally:
        app.dependency_overrides.clear()


def test_sw_update_script_is_valid_powershell(tmp_path):
    """이 스크립트는 문자열로 조립돼 분리된 프로세스에서 돌아간다 — 문법이 틀리면
    아무 일도 일어나지 않고 그 사실조차 드러나지 않는다. PowerShell 파서로 확인한다."""
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if pwsh is None:
        pytest.skip("PowerShell 없음")

    mock_admin = MagicMock()
    mock_admin.name = "admin"
    app.dependency_overrides[require_admin] = lambda: mock_admin
    app.dependency_overrides[get_db] = lambda: MagicMock()
    try:
        with patch("subprocess.Popen") as mock_popen, patch("app.audit.record"):
            client.post("/paas/api/v1/system/sw-update")
            script = mock_popen.call_args.args[0][-1]
    finally:
        app.dependency_overrides.clear()

    script_file = tmp_path / "sw-update.ps1"
    script_file.write_text(script, encoding="utf-8")
    check = (
        "$e = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseInput("
        f"(Get-Content -Raw '{script_file}'), [ref]$null, [ref]$e) | Out-Null; "
        "if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }"
    )
    done = subprocess.run([pwsh, "-NoProfile", "-Command", check],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
