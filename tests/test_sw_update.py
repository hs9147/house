"""SW 업데이트 엔드포인트 — 코드 갱신 → 의존성 설치 → 콘솔 빌드 → 재시작(실행은 목킹)."""
import shutil
import subprocess
import sys
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
            # 스케줄된 스크립트가 코드 갱신과 Restart-Service를 포함하는지 확인
            script = mock_popen.call_args.args[0][-1]
            # `git pull`을 그냥 부르면 upstream이 없는 체크아웃에서 fetch만 하고 끝난다
            # (실측: 서버는 master, 원격은 main). fetch + ff-only로 못 박는다.
            assert "git fetch origin" in script
            assert "merge --ff-only" in script
            assert "origin/main" in script
            assert "Restart-Service -Name 'paas'" in script
            assert "Restart-Service -Name 'paas-console'" in script
            # 콘솔은 플랫폼 자신이라 배포 파이프라인이 없다 — 여기서 빌드하지 않으면
            # 의존성이 늘었을 때 예전 dist가 조용히 계속 서빙된다(실제로 겪었다).
            # npm ci는 쓰지 않는다 — node_modules를 통째로 지우는데, 콘솔 서비스(vite)가 떠 있으면
            # 윈도우가 esbuild.exe를 잠가 반쯤 지워진 채 남는다. install 뒤 lockfile을 되돌린다:
            # 서버의 npm이 다시 쓴 lockfile이 남으면 다음 업데이트의 fast-forward가 막힌다(실측).
            assert "npm install;" in script and "npm ci" not in script
            install = script.index("npm install;")
            assert install < script.index("git checkout -- package-lock.json") < script.index("npm run build")
            assert "npm run build" in script
            # 콘솔 소스나 npm이 없는 설치본에서는 건너뛴다
            assert "Test-Path" in script and "Get-Command npm" in script
            # 빌드가 실패해도 서비스 재시작까지는 간다 — 대신 실패를 말한다
            assert script.index("npm run build") < script.index("Restart-Service")
            assert "콘솔 빌드 실패" in script
            # 분리된 프로세스라 stdout이 어디에도 닿지 않는다 — 로그로 남겨야 읽을 수 있다
            assert "Start-Transcript" in script and "sw-update.log" in script
            # 파이썬 의존성도 설치한다. "어느 venv인가"는 sys.executable로 답이 정해진다 —
            # 지금 도는 프로세스의 인터프리터가 곧 서비스가 쓰는 인터프리터다.
            assert "-m pip install -r " in script
            assert sys.executable.replace("'", "''") in script
            # 재시작 전에 설치해야 새 의존성이 다음 기동에 반영된다
            assert script.index("-m pip install") < script.index("Restart-Service")
            # 실패해도 재시작까지는 간다 — 대신 실패를 말한다(적재 중인 .pyd는 못 덮는다)
            assert "pip install 실패" in script
            # **스키마도 올린다.** 코드만 당기면 컬럼이 늘어난 화면이 500으로 죽는다 —
            # 기동 경고를 읽고 alembic을 손으로 돌리는 일은 잊힌다(실측: 세 컬럼이 빠져 있었다).
            assert "alembic upgrade head" in script
            assert script.index("alembic upgrade head") < script.index("Restart-Service")
            assert "alembic 실패" in script
            # 코드 갱신에 실패하면 **멈춘다** — 갱신 전 파일로 설치·재시작하면 "업데이트한
            # 줄 알았는데 아무것도 바뀌지 않은" 상태가 된다(실측).
            assert "fast-forward 불가" in script
            assert script.index("merge --ff-only") < script.index("-m pip install")
            # 지난 npm이 남긴 lockfile 변경은 merge 전에 되돌린다 — 그것 하나만(사람이
            # 고친 파일까지 덮어쓰면 안 된다).
            assert (script.index("git checkout -- console/package-lock.json")
                    < script.index("merge --ff-only"))
            assert "git checkout -- ." not in script and "reset --hard" not in script
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
    # -Encoding UTF8 — Windows PowerShell 5.1은 BOM 없는 파일을 시스템 코드 페이지(한글
    # 윈도우는 CP949)로 읽어 한글 안내문을 깨뜨리고, 깨진 바이트가 따옴표를 삼켜 멀쩡한
    # 스크립트를 문법 오류로 보고했다. 실제 실행은 -Command 인자라 파일 인코딩을 거치지 않는다.
    check = (
        "$e = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseInput("
        f"(Get-Content -Raw -Encoding UTF8 '{script_file}'), [ref]$null, [ref]$e) | Out-Null; "
        "if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }"
    )
    done = subprocess.run([pwsh, "-NoProfile", "-Command", check],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
