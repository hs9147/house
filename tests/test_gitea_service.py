"""Gitea REST API 클라이언트 — 멱등 생성, 설정 누락/실패 경로."""
import pytest

from app.config import get_settings
from app.services import gitea


class _Res:
    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status_code = status
        self._body = body or {}
        self.text = text or str(body)

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _configured(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_ensure_org_created(monkeypatch):
    calls = []
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: (calls.append((url, kw)), _Res(201))[1])
    gitea.ensure_org("shop-team")
    url, kw = calls[0]
    assert url == "https://git.example.com/api/v1/orgs"
    assert kw["headers"]["Authorization"] == "token tok-123"
    assert kw["json"]["username"] == "shop-team"


def test_ensure_org_already_exists_is_idempotent(monkeypatch):
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: _Res(422))
    gitea.ensure_org("shop-team")  # 예외 없이 통과해야 함


def test_ensure_org_other_error_raises(monkeypatch):
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: _Res(500, text="boom"))
    with pytest.raises(gitea.GiteaError, match="500"):
        gitea.ensure_org("shop-team")


def test_ensure_repo_created_returns_clone_url(monkeypatch):
    monkeypatch.setattr(
        gitea.httpx, "post",
        lambda url, **kw: _Res(201, {"clone_url": "https://git.example.com/shop-team/api.git"}),
    )
    url = gitea.ensure_repo("shop-team", "api")
    assert url == "https://git.example.com/shop-team/api.git"


def test_ensure_repo_conflict_reuses_existing(monkeypatch):
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: _Res(409))
    monkeypatch.setattr(
        gitea.httpx, "get",
        lambda url, **kw: _Res(200, {"clone_url": "https://git.example.com/shop-team/api.git"}),
    )
    url = gitea.ensure_repo("shop-team", "api")
    assert url == "https://git.example.com/shop-team/api.git"


def test_not_configured_raises_specific_error(monkeypatch, fresh_settings):
    monkeypatch.delenv("PAAS_GITEA_URL", raising=False)
    get_settings.cache_clear()
    with pytest.raises(gitea.GiteaNotConfigured):
        gitea.ensure_org("x")


def test_ensure_repo_auto_init_false_for_upload(monkeypatch):
    calls = []
    monkeypatch.setattr(
        gitea.httpx, "post",
        lambda url, **kw: (calls.append(kw), _Res(201, {"clone_url": "https://git.example.com/x/y.git"}))[1],
    )
    gitea.ensure_repo("x", "y", auto_init=False)
    assert calls[0]["json"]["auto_init"] is False


def test_ensure_webhook_skips_without_public_url(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    get_settings.cache_clear()
    called = []
    monkeypatch.setattr(gitea.httpx, "get", lambda *a, **kw: called.append(1))
    gitea.ensure_webhook("shop-team", "api")  # public url 미설정 — 조용히 건너뜀
    assert called == []


def test_ensure_webhook_registers_when_absent(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", "https://paas.example.com")
    monkeypatch.setenv("PAAS_WEBHOOK_SECRET", "whsecret")
    get_settings.cache_clear()
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: _Res(200, []))
    posts = []
    monkeypatch.setattr(
        gitea.httpx, "post", lambda url, **kw: (posts.append((url, kw)), _Res(201))[1]
    )
    gitea.ensure_webhook("shop-team", "api")
    url, kw = posts[0]
    assert url == "https://git.example.com/api/v1/repos/shop-team/api/hooks"
    assert kw["json"]["config"]["url"] == "https://paas.example.com/paas/webhooks/git"
    assert kw["json"]["config"]["secret"] == "whsecret"


def test_list_orgs_paginates_until_short_page(monkeypatch):
    pages = {
        1: [{"username": f"org-{i}"} for i in range(50)],
        2: [{"username": "org-50"}],
    }
    calls = []

    def fake_get(url, **kw):
        page = kw["params"]["page"]
        calls.append(page)
        return _Res(200, pages.get(page, []))

    monkeypatch.setattr(gitea.httpx, "get", fake_get)
    orgs = gitea.list_orgs()
    assert len(orgs) == 51
    assert calls == [1, 2]


def test_list_org_repos_stops_on_empty_page(monkeypatch):
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: _Res(200, []))
    assert gitea.list_org_repos("acme") == []


def test_list_orgs_error_raises(monkeypatch):
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: _Res(500, text="boom"))
    with pytest.raises(gitea.GiteaError, match="500"):
        gitea.list_orgs()


def test_ensure_webhook_idempotent_when_already_registered(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", "https://paas.example.com")
    get_settings.cache_clear()
    monkeypatch.setattr(
        gitea.httpx, "get",
        lambda url, **kw: _Res(200, [{"config": {"url": "https://paas.example.com/paas/webhooks/git"}}]),
    )
    posted = []
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: posted.append(1))
    gitea.ensure_webhook("shop-team", "api")
    assert posted == []
