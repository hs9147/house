"""처리하지 못한 예외도 까닭을 돌려준다(app.main의 전역 처리기) — 본문이 「Internal Server Error」뿐이면 화면에는 「HTTP 500」만
뜨고 어느 요청이 왜 실패했는지 알 수 없었다."""
from fastapi.testclient import TestClient

from app.main import create_app


def test_unhandled_errors_carry_their_type_message_and_path(monkeypatch):
    from app.services import ontology_status

    app = create_app()
    monkeypatch.setattr(ontology_status, "overview", lambda stores: (_ for _ in ()).throw(ZeroDivisionError("division by zero")))
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/paas/api/v1/ontology/overview", headers={"x-api-key": "test-admin-key"})
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "ZeroDivisionError" in detail and "division by zero" in detail and "/ontology/overview" in detail
