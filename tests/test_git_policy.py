"""기업용 거버넌스 — PAAS_GIT_INTERNAL_ONLY 시 프로젝트 git_url을 사내 Gitea로 한정."""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.git_policy import enforce_internal_git_url

ADMIN = {"x-api-key": "test-admin-key"}


def _client(monkeypatch, gitea_url: str = "https://git.example.com", internal_only: str = "true"):
    monkeypatch.setenv("PAAS_GIT_INTERNAL_ONLY", internal_only)
    if gitea_url:
        monkeypatch.setenv("PAAS_GITEA_URL", gitea_url)
    get_settings.cache_clear()
    return TestClient(create_app())


def test_git_internal_only_defaults_to_true(monkeypatch, fresh_settings):
    # conftest.py는 정책과 무관한 테스트를 위해 세션 전체에 false를 깔아두므로,
    # 그 env를 제거해야 config.py의 실제 기본값(true)을 검증할 수 있다.
    monkeypatch.delenv("PAAS_GIT_INTERNAL_ONLY", raising=False)
    assert get_settings().git_internal_only is True


def test_any_host_allowed_when_explicitly_disabled(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_GIT_INTERNAL_ONLY", "false")
    get_settings.cache_clear()
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/projects", json={
        "name": "ext-app", "type": "python", "git_url": "https://github.com/org/repo",
    }, headers=ADMIN)
    assert r.status_code == 201


def test_external_host_rejected_when_enabled(monkeypatch, fresh_settings):
    c = _client(monkeypatch)
    r = c.post("/paas/api/v1/projects", json={
        "name": "ext-app", "type": "python", "git_url": "https://github.com/org/repo",
    }, headers=ADMIN)
    assert r.status_code == 422
    assert "git.example.com" in r.text


def test_internal_host_allowed_when_enabled(monkeypatch, fresh_settings):
    c = _client(monkeypatch)
    r = c.post("/paas/api/v1/projects", json={
        "name": "int-app", "type": "python",
        "git_url": "https://git.example.com/org/repo",
    }, headers=ADMIN)
    assert r.status_code == 201


def test_ssh_scp_form_host_matches(monkeypatch, fresh_settings):
    c = _client(monkeypatch)
    r = c.post("/paas/api/v1/projects", json={
        "name": "ssh-app", "type": "python",
        "git_url": "git@git.example.com:org/repo.git",
    }, headers=ADMIN)
    assert r.status_code == 201


def test_ssh_scp_form_wrong_host_rejected(monkeypatch, fresh_settings):
    c = _client(monkeypatch)
    r = c.post("/paas/api/v1/projects", json={
        "name": "ssh-bad", "type": "python", "git_url": "git@github.com:org/repo.git",
    }, headers=ADMIN)
    assert r.status_code == 422


def test_enabled_without_gitea_url_gives_clear_error(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_GIT_INTERNAL_ONLY", "true")
    monkeypatch.delenv("PAAS_GITEA_URL", raising=False)
    get_settings.cache_clear()
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/projects", json={
        "name": "misconfigured", "type": "python", "git_url": "https://git.example.com/org/repo",
    }, headers=ADMIN)
    assert r.status_code == 503
    assert "PAAS_GITEA_URL" in r.text


def test_helper_noop_when_disabled(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_GIT_INTERNAL_ONLY", "false")
    get_settings.cache_clear()
    enforce_internal_git_url("https://anywhere.example.com/x")  # 예외 없이 통과해야 함
