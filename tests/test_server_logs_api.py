"""서버 로그 조회 — 실행 경로 하위 logs/의 .txt 파일만 나열·열람한다.

배포 빌드 로그(PAAS_BUILD_LOG_DIR)와는 다른 것이다 — 그쪽은 배포 레코드마다
따로 보여준다(api/projects.py의 deployment_build_log).
"""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app

ADMIN = {"x-api-key": "test-admin-key"}


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("PAAS_REPO_ROOT", str(tmp_path))
    get_settings.cache_clear()
    return TestClient(create_app())


def test_lists_txt_files_under_logs_newest_first(monkeypatch, tmp_path, fresh_settings):
    logs = tmp_path / "logs"
    (logs / "sub").mkdir(parents=True)
    (logs / "old.txt").write_text("old", encoding="utf-8")
    (logs / "sub" / "new.txt").write_text("new", encoding="utf-8")
    import os
    os.utime(logs / "old.txt", (1_000_000, 1_000_000))

    c = _client(monkeypatch, tmp_path)
    body = c.get("/paas/api/v1/system/server-logs", headers=ADMIN).json()
    assert body["log_dir"] == str(logs.resolve())
    names = [f["relative_path"] for f in body["files"]]
    assert names == ["sub/new.txt", "old.txt"]


def test_missing_logs_dir_is_created_and_empty(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)
    body = c.get("/paas/api/v1/system/server-logs", headers=ADMIN).json()
    assert body["files"] == []
    assert (tmp_path / "logs").is_dir()


def test_reads_tail_of_selected_file(monkeypatch, tmp_path, fresh_settings):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "app.txt").write_text("\n".join(str(i) for i in range(100)), encoding="utf-8")

    c = _client(monkeypatch, tmp_path)
    r = c.get("/paas/api/v1/system/server-logs/content",
              params={"filename": "app.txt", "tail_lines": 3}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["content"].splitlines() == ["97", "98", "99"]


def test_rejects_escape_outside_logs_dir(monkeypatch, tmp_path, fresh_settings):
    (tmp_path / "logs").mkdir()
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    c = _client(monkeypatch, tmp_path)
    r = c.get("/paas/api/v1/system/server-logs/content",
              params={"filename": "../secret.txt"}, headers=ADMIN)
    assert r.status_code == 403, r.text


def test_rejects_sibling_directory_with_shared_prefix(monkeypatch, tmp_path, fresh_settings):
    """문자열 startswith로 막으면 logs-old/ 같은 형제 디렉터리가 통과한다."""
    (tmp_path / "logs").mkdir()
    sibling = tmp_path / "logs-old"
    sibling.mkdir()
    (sibling / "leak.txt").write_text("leak", encoding="utf-8")
    c = _client(monkeypatch, tmp_path)
    r = c.get("/paas/api/v1/system/server-logs/content",
              params={"filename": "../logs-old/leak.txt"}, headers=ADMIN)
    assert r.status_code == 403, r.text


def test_rejects_non_txt(monkeypatch, tmp_path, fresh_settings):
    """목록은 .txt만 보여주므로 읽기도 같은 범위여야 한다."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "app.env").write_text("SECRET=1", encoding="utf-8")
    c = _client(monkeypatch, tmp_path)
    r = c.get("/paas/api/v1/system/server-logs/content",
              params={"filename": "app.env"}, headers=ADMIN)
    assert r.status_code == 403, r.text


def test_requires_admin(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)
    assert c.get("/paas/api/v1/system/server-logs").status_code in (401, 403)
