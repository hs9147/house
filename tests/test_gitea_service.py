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


def test_ensure_repo_uses_http_clone_url_not_ssh(monkeypatch):
    """Gitea 응답에는 clone_url(http)과 ssh_url이 함께 온다 — 반드시 http 쪽을 써야 한다.

    SSH 포트를 못 여는 환경(80만 허용 등)에서도 플랫폼의 clone/fetch/push가 그대로
    동작해야 하기 때문이다. 인증도 SSH 키가 아니라 토큰을 http 헤더로 넣어 처리한다
    (services/git_auth.py).
    """
    body = {
        "clone_url": "https://git.example.com/shop-team/api.git",
        "ssh_url": "git@git.example.com:shop-team/api.git",
    }
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: _Res(201, body))
    assert gitea.ensure_repo("shop-team", "api") == body["clone_url"]

    # 이미 있는 리포를 재사용하는 경로(409 → 조회)도 마찬가지다.
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: _Res(409))
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: _Res(200, body))
    assert gitea.ensure_repo("shop-team", "api") == body["clone_url"]


def test_ensure_repo_created_as_public(monkeypatch):
    calls = []
    monkeypatch.setattr(
        gitea.httpx, "post",
        lambda url, **kw: (calls.append(kw), _Res(201, {"clone_url": "https://git.example.com/x/y.git"}))[1],
    )
    gitea.ensure_repo("x", "y")
    assert calls[0]["json"]["private"] is False


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


def test_repo_slug_parses_internal_repo_only():
    assert gitea.repo_slug("https://git.example.com/shop-team/api.git") == ("shop-team", "api")
    assert gitea.repo_slug("https://git.example.com/shop-team/api") == ("shop-team", "api")
    # 사내 Gitea가 아니거나 owner/repo 형태가 아니면 API를 쓸 수 없다
    assert gitea.repo_slug("https://github.com/shop-team/api.git") is None
    assert gitea.repo_slug("https://git.example.com/api") is None


def test_ensure_pull_request_created(monkeypatch):
    calls = []
    monkeypatch.setattr(
        gitea.httpx, "post",
        lambda url, **kw: (calls.append((url, kw)), _Res(201, {"number": 3}))[1],
    )
    pr = gitea.ensure_pull_request("shop-team", "api", "paas/plan-1", "main", "제목", "본문")
    url, kw = calls[0]
    assert url == "https://git.example.com/api/v1/repos/shop-team/api/pulls"
    assert kw["json"] == {"head": "paas/plan-1", "base": "main", "title": "제목", "body": "본문"}
    assert pr["number"] == 3


def test_ensure_pull_request_reuses_open_pr_on_conflict(monkeypatch):
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: _Res(409))
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: _Res(200, [
        {"number": 1, "head": {"ref": "other"}, "base": {"ref": "main"}},
        {"number": 9, "head": {"ref": "paas/plan-1"}, "base": {"ref": "main"}},
    ]))
    pr = gitea.ensure_pull_request("shop-team", "api", "paas/plan-1", "main", "제목")
    assert pr["number"] == 9


def test_ensure_pull_request_without_commits_is_not_a_failure(monkeypatch):
    """409인데 열린 PR도 없으면 남는 해석은 '반영할 커밋이 없다'다 — 오류가 아니다."""
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: _Res(409, text="no commits between"))
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: _Res(200, []))
    with pytest.raises(gitea.GiteaNothingToMerge):
        gitea.ensure_pull_request("shop-team", "api", "paas/plan-1", "main", "제목")


def test_ensure_pull_request_error_raises(monkeypatch):
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: _Res(500, text="boom"))
    with pytest.raises(gitea.GiteaError, match="500"):
        gitea.ensure_pull_request("shop-team", "api", "b", "main", "제목")


def test_merge_pull_request_success_and_not_mergeable(monkeypatch):
    calls = []
    monkeypatch.setattr(
        gitea.httpx, "post", lambda url, **kw: (calls.append(url), _Res(200))[1]
    )
    assert gitea.merge_pull_request("shop-team", "api", 3) is True
    assert calls[0] == "https://git.example.com/api/v1/repos/shop-team/api/pulls/3/merge"

    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: _Res(405, text="conflict"))
    assert gitea.merge_pull_request("shop-team", "api", 3) is False


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


# ─── 조직 소속(팀) — 개발자 push 권한 ────────────────────────────────────────
# 리포는 public이지만 push에는 쓰기 권한이 필요하다. SSO 자동 등록으로 갓 만들어진
# 계정은 아무 조직에도 없어 clone은 되고 push만 403이 난다.

def test_find_username_by_email_requires_exact_match(monkeypatch):
    """검색은 부분 일치라 비슷한 이메일이 함께 온다 — 정확히 같은 것만 골라야 엉뚱한
    사람에게 쓰기 권한이 나가지 않는다."""
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: _Res(200, {"data": [
        {"login": "alice-old", "email": "alice@other.com"},
        {"login": "alice", "email": "Alice@cho-fam.com"},
    ]}))
    assert gitea.find_username_by_email("alice@cho-fam.com") == "alice"


def test_find_username_by_email_missing_returns_none(monkeypatch):
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: _Res(200, {"data": []}))
    assert gitea.find_username_by_email("nobody@cho-fam.com") is None


def test_set_org_membership_adds_to_existing_team(monkeypatch):
    calls = []
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: (
        _Res(200, {"data": [{"login": "alice", "email": "alice@cho-fam.com"}]})
        if "/users/search" in url else _Res(200, [{"id": 7, "name": gitea.WRITE_TEAM_NAME}])
    ))
    monkeypatch.setattr(gitea.httpx, "put", lambda url, **kw: (calls.append(url), _Res(204))[1])
    assert gitea.set_org_membership("shop-team", "alice@cho-fam.com", True) is True
    assert calls == ["https://git.example.com/api/v1/teams/7/members/alice"]


def test_set_org_membership_creates_team_when_absent(monkeypatch):
    posted = []
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: (
        _Res(200, {"data": [{"login": "alice", "email": "alice@cho-fam.com"}]})
        if "/users/search" in url else _Res(200, [{"id": 1, "name": "Owners"}])
    ))
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: (posted.append(kw["json"]), _Res(201, {"id": 9}))[1])
    monkeypatch.setattr(gitea.httpx, "put", lambda url, **kw: _Res(204))
    assert gitea.set_org_membership("shop-team", "alice@cho-fam.com", True) is True
    body = posted[0]
    assert body["permission"] == "write"
    # units가 비면 아무 권한 없는 팀이 만들어져 push가 그대로 막힌다.
    assert "repo.code" in body["units"]
    # 나중에 만들어지는 리포까지 포함돼야 프로젝트를 만들 때마다 팀에 다시 안 붙인다.
    assert body["includes_all_repositories"] is True


def test_set_org_membership_creates_gitea_account_when_missing(monkeypatch):
    """아직 SSO 로그인을 한 번도 안 한 사용자에게 배지를 줘도 바로 붙어야 한다 —
    안 그러면 "먼저 Gitea에 로그인한 뒤 배지를 다시 주라"는 순서를 사람이 기억해야 한다."""
    posted = []
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: (
        _Res(200, {"data": []}) if "/users/search" in url
        else _Res(200, [{"id": 7, "name": gitea.WRITE_TEAM_NAME}])
    ))
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: (
        posted.append((url, kw["json"])), _Res(201, {"login": "new"}))[1])
    added = []
    monkeypatch.setattr(gitea.httpx, "put", lambda url, **kw: (added.append(url), _Res(204))[1])

    assert gitea.set_org_membership("shop-team", "new@cho-fam.com", True, full_name="새 사람") is True
    url, body = posted[0]
    assert url == "https://git.example.com/api/v1/admin/users"
    assert body["email"] == "new@cho-fam.com" and body["username"] == "new"
    # 이 계정은 SSO로만 들어온다 — 아무도 모르는 비밀번호로 로그인 화면을 띄우면 안 된다.
    assert body["must_change_password"] is False
    assert body["password"] and body["password"] != "new@cho-fam.com"
    assert added == ["https://git.example.com/api/v1/teams/7/members/new"]


def test_set_org_membership_remove_does_not_create_account(monkeypatch):
    """뺄 때는 없는 계정을 만들 이유가 없다."""
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: _Res(200, {"data": []}))
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: pytest.fail("계정을 만들면 안 된다"))
    assert gitea.set_org_membership("shop-team", "new@cho-fam.com", False) is False


def test_ensure_user_reuses_existing_account(monkeypatch):
    """이미 있으면 만들지 않는다 — 같은 사람의 계정이 둘 생기면 권한이 갈라진다."""
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: _Res(200, {"data": [
        {"login": "alice", "email": "alice@cho-fam.com"}]}))
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: pytest.fail("이미 있는데 또 만들었다"))
    assert gitea.ensure_user("alice@cho-fam.com") == "alice"


def test_ensure_user_reports_non_admin_token_clearly(monkeypatch):
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: _Res(200, {"data": []}))
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: _Res(403, text="forbidden"))
    with pytest.raises(gitea.GiteaError, match="관리자 계정의 토큰"):
        gitea.ensure_user("new@cho-fam.com")


def test_username_strips_characters_gitea_rejects(monkeypatch):
    """@가 들어간 이름은 Gitea가 거부한다. +·공백 등도 마찬가지."""
    assert gitea._username_for("hong.gil-dong+tag@cho-fam.com") == "hong.gil-dong-tag"
    assert "@" not in gitea._username_for("a@b.com")


def test_set_org_membership_remove_tolerates_already_absent(monkeypatch):
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: (
        _Res(200, {"data": [{"login": "alice", "email": "alice@cho-fam.com"}]})
        if "/users/search" in url else _Res(200, [{"id": 7, "name": gitea.WRITE_TEAM_NAME}])
    ))
    monkeypatch.setattr(gitea.httpx, "delete", lambda url, **kw: _Res(404))
    assert gitea.set_org_membership("shop-team", "alice@cho-fam.com", False) is True
