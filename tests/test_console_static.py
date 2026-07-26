"""콘솔 정적 마운트 — dist 유무와 무관하게 API가 동일하게 기동하는지 검증."""
from fastapi.testclient import TestClient

from app.main import create_app


def test_boots_without_dist(monkeypatch, tmp_path):
    monkeypatch.setenv("PAAS_CONSOLE_DIST", str(tmp_path / "no-such-dir"))
    c = TestClient(create_app())
    assert c.get("/paas/health").status_code == 200
    assert c.get("/console/").status_code == 404


def test_serves_console_when_dist_exists(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>PaaS 콘솔</title>", encoding="utf-8")
    monkeypatch.setenv("PAAS_CONSOLE_DIST", str(dist))
    c = TestClient(create_app())
    assert c.get("/paas/health").status_code == 200
    res = c.get("/console/")
    assert res.status_code == 200
    assert "PaaS 콘솔" in res.text
