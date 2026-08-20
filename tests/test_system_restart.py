from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.security import require_admin
from app.db import get_db

client = TestClient(app, raise_server_exceptions=False)


def test_restart_backend_service_permissions():
    resp = client.post("/paas/api/v1/system/restart")
    assert resp.status_code in (401, 403)


def test_restart_backend_service_success():
    mock_admin = MagicMock()
    mock_admin.name = "admin"
    mock_db = MagicMock()

    app.dependency_overrides[require_admin] = lambda: mock_admin
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        # 이 엔드포인트는 2.5초 뒤 os._exit(0)으로 자기 프로세스를 죽이는 타이머를 건다
        # (포트를 비워 주기 위해). 테스트에서 진짜로 걸리게 두면 그 2.5초 뒤에 터져
        # **pytest 프로세스가 통째로 죽는다** — 전체 스위트를 돌릴 때 이 파일 다음 테스트들이
        # 실행 중이라, 요약도 실패도 못 남기고 종료 코드 0으로 끝나 "전부 통과"처럼 보였다.
        with patch("subprocess.Popen") as mock_popen, patch("app.audit.record"), \
                patch("threading.Timer") as mock_timer:
            resp = client.post("/paas/api/v1/system/restart")
            assert resp.status_code == 200, f"Error: {resp.status_code} - {resp.text}"
            data = resp.json()
            assert data["status"] == "restarting"
            assert "안전하게 재기동됩니다" in data["message"]
            assert mock_popen.called
            # 타이머를 거는 것 자체는 이 엔드포인트의 약속이므로 걸렸는지는 확인한다
            assert mock_timer.call_args.args[0] == 2.5
            assert mock_timer.return_value.start.called
    finally:
        app.dependency_overrides.clear()
