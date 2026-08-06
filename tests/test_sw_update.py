"""SW 업데이트 엔드포인트 — git pull → 환경설정(pip/npm install) → 서비스 재시작 스케줄
(실제 실행은 목킹)."""
from unittest.mock import MagicMock, patch

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
            # 스케줄된 스크립트가 git pull → 환경설정 → Restart-Service 순서를 포함하는지 확인
            script = mock_popen.call_args.args[0][-1]
            assert "git pull" in script
            assert "pip install" in script and "requirements.txt" in script
            assert "npm install" in script and "npm run build" in script
            assert "Restart-Service -Name 'paas'" in script
            assert "Restart-Service -Name 'paas-console'" in script

            pull_idx = script.index("git pull")
            pip_idx = script.index("pip install")
            npm_idx = script.index("npm install")
            restart_idx = script.index("Restart-Service")
            assert pull_idx < pip_idx < restart_idx  # pip install은 pull 후, 재시작 전
            assert pull_idx < npm_idx < restart_idx  # npm install도 pull 후, 재시작 전
    finally:
        app.dependency_overrides.clear()
