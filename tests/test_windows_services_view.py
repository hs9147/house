"""서버구성의 "등록된 Windows Service" — 화면에서 찌꺼기·고아 서비스를 볼 수 있어야 한다.

status()는 예상 이름을 조회할 뿐이라, 배포가 중간에 끊겨 남은 슬롯이나 프로젝트를
지운 뒤 남은 서비스는 드러나지 않는다. 그 둘이 배포를 막는 원인이다.
"""
import subprocess

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services.runtime import windows_service_runtime as wsr

ADMIN = {"x-api-key": "test-admin-key"}

SC_OUTPUT = """
SERVICE_NAME: Dhcp
DISPLAY_NAME: DHCP Client
        STATE              : 4  RUNNING

SERVICE_NAME: paas-shop-api-a
DISPLAY_NAME: paas-shop-api-a
        STATE              : 4  RUNNING

SERVICE_NAME: paas-shop-api-b
DISPLAY_NAME: paas-shop-api-b
        STATE              : 1  STOPPED

SERVICE_NAME: paas-gone-a
DISPLAY_NAME: paas-gone-a
        STATE              : 1  STOPPED
"""


class _Res:
    returncode = 0
    stdout = SC_OUTPUT
    stderr = ""


def _client(monkeypatch):
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    get_settings.cache_clear()
    c = TestClient(create_app())
    c.post("/paas/api/v1/projects", json={
        "name": "shop-api", "type": "python", "git_url": "https://git.example.com/o/r.git",
        "branch": "main",
    }, headers=ADMIN)
    return c


def test_lists_registered_services_and_flags_duplicate_slot(monkeypatch, fresh_settings):
    c = _client(monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Res())

    body = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    by_name = {s["name"]: s for s in body["windows_services"]}

    # paas- 접두어가 아닌 시스템 서비스는 섞이지 않는다.
    assert "Dhcp" not in by_name

    # 하이픈이 든 프로젝트 이름도 정확히 맞춘다 — 이름을 역산하면 여기서 틀린다.
    assert by_name["paas-shop-api-a"]["project_name"] == "shop-api"
    assert by_name["paas-shop-api-a"]["slot"] == "a"
    assert by_name["paas-shop-api-a"]["state"] == "running"
    assert by_name["paas-shop-api-b"]["state"] == "stopped"

    # 두 슬롯이 모두 남아 있으면 다음 배포를 막던 상태다 — 양쪽 다 표시한다.
    assert by_name["paas-shop-api-a"]["duplicate_slot"] is True
    assert by_name["paas-shop-api-b"]["duplicate_slot"] is True

    # 지워진 프로젝트의 잔여 서비스는 프로젝트를 못 맞춘 채로 드러난다.
    assert by_name["paas-gone-a"]["project_name"] is None


def test_empty_when_runtime_is_not_windows_service(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "docker")
    get_settings.cache_clear()
    c = TestClient(create_app())
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Res())
    body = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    assert body["windows_services"] == []


def test_sc_unavailable_is_not_an_error(monkeypatch, fresh_settings):
    """sc가 없거나 실패해도 서버구성 화면 전체가 죽으면 안 된다."""
    c = _client(monkeypatch)

    def _boom(*a, **kw):
        raise FileNotFoundError("sc")

    monkeypatch.setattr(subprocess, "run", _boom)
    r = c.get("/paas/api/v1/server-config", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["windows_services"] == []
