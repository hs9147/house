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
        with patch("subprocess.Popen") as mock_popen, patch("app.audit.record"):
            resp = client.post("/paas/api/v1/system/restart")
            assert resp.status_code == 200, f"Error: {resp.status_code} - {resp.text}"
            data = resp.json()
            assert data["status"] == "restarting"
            assert "안전하게 재기동됩니다" in data["message"]
            assert mock_popen.called
    finally:
        app.dependency_overrides.clear()
