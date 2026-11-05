"""대시보드 「시스템 상태」의 백엔드 인스턴스 — 인스턴스 하나로 버티는지 보는 숫자(services/monitor.backend_instance)."""
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import monitor, worksession

ADMIN = {"x-api-key": "test-admin-key"}


def test_status_carries_the_backend_instance():
    c = TestClient(create_app())
    body = c.get("/paas/status", headers=ADMIN).json()
    b = body["backend"]
    assert b["instances"] == 1 and b["pid"] > 0
    assert b["request_threads"]["limit"] >= 1 and 0 <= b["request_threads"]["busy"] <= b["request_threads"]["limit"]
    assert set(b["background"]) == {"analysis", "tasks"} and isinstance(b["scheduler"], bool)
    assert "cpu_percent" in body or "system" in body                # 시스템 스냅샷도 그대로 실린다


def test_running_llm_turns_are_counted():
    worksession._progress_start(9001, "a@lge.com")
    worksession._progress_start(9002, "b@lge.com")
    try:
        assert monitor.backend_instance()["llm_turns"] >= 2
        worksession._progress_end(9001)
        before = monitor.backend_instance()["llm_turns"]
        worksession._progress_end(9002)
        assert monitor.backend_instance()["llm_turns"] == before - 1
    finally:
        worksession._PROGRESS.pop(9001, None)
        worksession._PROGRESS.pop(9002, None)
