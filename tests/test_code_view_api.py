"""코드 확인 화면 — 읽기 전용 파일 트리/내용 조회 (수정 엔드포인트는 없음)."""
import subprocess

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app

ADMIN = {"x-api-key": "test-admin-key"}


def _repo_with_files(tmp_path):
    repo = tmp_path / "src-repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "app.py").write_text("print('hi')\n")
    (repo / "README.md").write_text("# hi\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=repo, check=True,
    )
    return repo


def _client_with_project(monkeypatch, fresh_settings, tmp_path):
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    repo = _repo_with_files(tmp_path)
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/projects", json={
        "name": "code-view", "type": "python", "git_url": str(repo),
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    return c, r.json()["id"]


def test_project_files_lists_tree(monkeypatch, fresh_settings, tmp_path):
    c, pid = _client_with_project(monkeypatch, fresh_settings, tmp_path)
    r = c.get(f"/paas/api/v1/projects/{pid}/files", headers=ADMIN)
    assert r.status_code == 200
    assert set(r.json()["files"]) == {"app.py", "README.md"}


def test_project_file_content_returns_text(monkeypatch, fresh_settings, tmp_path):
    c, pid = _client_with_project(monkeypatch, fresh_settings, tmp_path)
    r = c.get(f"/paas/api/v1/projects/{pid}/files/content", params={"path": "app.py"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == {"path": "app.py", "content": "print('hi')\n"}


def test_project_file_content_rejects_path_escape(monkeypatch, fresh_settings, tmp_path):
    c, pid = _client_with_project(monkeypatch, fresh_settings, tmp_path)
    r = c.get(f"/paas/api/v1/projects/{pid}/files/content", params={"path": "../../../etc/passwd"}, headers=ADMIN)
    assert r.status_code == 404


def test_project_file_content_missing_file(monkeypatch, fresh_settings, tmp_path):
    c, pid = _client_with_project(monkeypatch, fresh_settings, tmp_path)
    r = c.get(f"/paas/api/v1/projects/{pid}/files/content", params={"path": "nope.py"}, headers=ADMIN)
    assert r.status_code == 404


def test_project_files_unknown_project_404(monkeypatch, fresh_settings, tmp_path):
    c, _pid = _client_with_project(monkeypatch, fresh_settings, tmp_path)
    assert c.get("/paas/api/v1/projects/999999/files", headers=ADMIN).status_code == 404
