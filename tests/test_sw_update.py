"""SW 업데이트 엔드포인트 — git pull 후 Windows 서비스 재시작 스케줄(실제 실행은 목킹)."""
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
            # 스케줄된 스크립트가 git pull과 Restart-Service를 포함하는지 확인
            script = mock_popen.call_args.args[0][-1]
            assert "git pull" in script
            assert "Restart-Service -Name 'paas'" in script
            assert "Restart-Service -Name 'paas-console'" in script
            # 환경설정은 sw-update의 책임이 아니다(프로젝트 배포 파이프라인에서 처리)
            assert "pip install" not in script
            assert "npm install" not in script
    finally:
        app.dependency_overrides.clear()
