"""사내 Gitea private 리포에 대한 git 인증 헤더 주입."""
from app.config import get_settings
from app.services.git_auth import auth_args


def test_gitea_host_gets_auth_header(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    get_settings.cache_clear()
    args = auth_args("https://git.example.com/shop-team/api.git")
    assert args == ["-c", "http.extraHeader=Authorization: token tok-123"]


def test_other_host_gets_no_auth(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    get_settings.cache_clear()
    assert auth_args("https://github.com/org/repo.git") == []


def test_not_configured_gets_no_auth(monkeypatch, fresh_settings):
    monkeypatch.delenv("PAAS_GITEA_URL", raising=False)
    monkeypatch.delenv("PAAS_GITEA_API_TOKEN", raising=False)
    get_settings.cache_clear()
    assert auth_args("https://git.example.com/shop-team/api.git") == []
