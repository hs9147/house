"""서버구성 시각화 + redirect/rewrite 규칙 CRUD."""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import deployer, gitea

ADMIN = {"x-api-key": "test-admin-key"}


class _FakeRuntime:
    def status(self, *a):
        return "running"


def _client() -> TestClient:
    return TestClient(create_app())


def _create_project(c: TestClient, name="shop-web") -> int:
    return c.post("/paas/api/v1/projects", json={
        "name": name, "type": "react", "git_url": "https://git.example.com/x",
    }, headers=ADMIN).json()["id"]


def test_server_config_defaults(monkeypatch, fresh_settings):
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    c = _client()
    pid = _create_project(c)
    r = c.get("/paas/api/v1/server-config", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runtime_backend"] == "docker"
    assert body["proxy_backend"] == "caddy"
    profiles = {s["profile"] for s in body["sites"] if s["project_id"] == pid}
    assert profiles == {"release", "development"}
    release = next(s for s in body["sites"] if s["project_id"] == pid and s["profile"] == "release")
    assert release["domain"] == "apps.test"
    assert release["path_prefix"] == "/apps/_/shop-web/"  # organization_id 없는 프로젝트 — 조직 자리는 "_"
    assert release["status"] == "running"
    assert release["redirect_count"] == 0
    assert release["redirects"] == []


def test_server_config_path_prefix_uses_organization_name(monkeypatch, fresh_settings):
    """조직 소속 프로젝트는 서브패스에 조직 이름이 들어간다 — /_/{project}/가 아니라
    /{조직}/{프로젝트}/ (services/proxy/__init__.py의 path_prefix_for)."""
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    get_settings.cache_clear()
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: type(
        "R", (), {"status_code": 201, "text": "",
                  "json": lambda self=None: {"clone_url": "https://git.example.com/acme/api.git"}}
    )())

    c = _client()
    org_id = c.post("/paas/api/v1/orgs", json={"name": "acme"}, headers=ADMIN).json()["id"]
    pid = c.post("/paas/api/v1/projects", json={
        "name": "shop", "type": "react", "organization_id": org_id,
    }, headers=ADMIN).json()["id"]

    body = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    release = next(s for s in body["sites"] if s["project_id"] == pid and s["profile"] == "release")
    dev = next(s for s in body["sites"] if s["project_id"] == pid and s["profile"] == "development")
    assert release["path_prefix"] == "/apps/acme/shop/"
    assert dev["path_prefix"] == "/apps/acme/shop/dev/"


def test_server_config_reflects_backend_settings(monkeypatch, fresh_settings):
    from app.config import get_settings

    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    monkeypatch.setenv("PAAS_PROXY_BACKEND", "iis")
    get_settings.cache_clear()
    c = _client()
    body = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    assert body["runtime_backend"] == "windows_service"
    assert body["proxy_backend"] == "iis"


def test_server_config_tolerates_runtime_errors(monkeypatch, fresh_settings):
    """런타임 상태 조회가 실패(예: docker SDK 미설치)해도 전체 화면이 500으로 죽지 않는다."""
    class _BrokenRuntime:
        def status(self, *a):
            raise RuntimeError("docker SDK가 설치되지 않았습니다")

    monkeypatch.setattr(deployer, "get_runtime", lambda: _BrokenRuntime())
    c = _client()
    _create_project(c)
    r = c.get("/paas/api/v1/server-config", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert all(s["status"].startswith("unknown") for s in r.json()["sites"])


def test_server_config_composite_project_shows_components(monkeypatch, fresh_settings):
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    c = _client()
    pid = c.post("/paas/api/v1/projects", json={
        "name": "shop-app", "type": "composite", "git_url": "https://git.example.com/x",
    }, headers=ADMIN).json()["id"]

    body = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    release = next(s for s in body["sites"] if s["project_id"] == pid and s["profile"] == "release")
    assert release["status"] == "running"  # backend/frontend 둘 다 running이면 요약도 running
    assert {c["name"] for c in release["components"]} == {"backend", "frontend"}
    assert all(c["status"] == "running" for c in release["components"])

    # 일반 프로젝트는 components가 없다(None)
    other_pid = _create_project(c, "shop-plain")
    body = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    plain = next(s for s in body["sites"] if s["project_id"] == other_pid)
    assert plain.get("components") is None


def test_server_config_composite_partial_status_summarized_as_partial(monkeypatch, fresh_settings):
    class _MixedRuntime:
        def status(self, name, *a):
            return "failed" if name.endswith("-frontend") else "running"

    monkeypatch.setattr(deployer, "get_runtime", lambda: _MixedRuntime())
    c = _client()
    pid = c.post("/paas/api/v1/projects", json={
        "name": "shop-mixed", "type": "composite", "git_url": "https://git.example.com/x",
    }, headers=ADMIN).json()["id"]

    body = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    release = next(s for s in body["sites"] if s["project_id"] == pid and s["profile"] == "release")
    assert release["status"] == "partial"
    statuses = {c["name"]: c["status"] for c in release["components"]}
    assert statuses == {"backend": "running", "frontend": "failed"}


def test_server_config_in_proxy_reflects_iis_web_config(monkeypatch, fresh_settings, tmp_path):
    """windows_service(IIS) 구성에서 in_proxy는 web.config(routes/)에 실제 라우팅
    조각이 있는 사이트만 True — 배포 전(조각 없음) 프로필은 False."""
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    monkeypatch.setenv("PAAS_PROXY_BACKEND", "iis")
    monkeypatch.setenv("PAAS_IIS_SITES_ROOT", str(tmp_path))
    get_settings.cache_clear()

    c = _client()
    pid = _create_project(c, "shop-web")
    # release 프로필의 라우팅 조각만 web.config(routes/)에 존재하도록 만든다.
    routes = tmp_path / "_base" / "routes"
    routes.mkdir(parents=True)
    (routes / "shop-web.xml").write_text("<!-- rule -->", encoding="utf-8")

    body = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    release = next(s for s in body["sites"] if s["project_id"] == pid and s["profile"] == "release")
    dev = next(s for s in body["sites"] if s["project_id"] == pid and s["profile"] == "development")
    assert release["in_proxy"] is True   # routes/shop-web.xml 존재
    assert dev["in_proxy"] is False       # shop-web-dev.xml 없음


def test_server_config_lists_unregistered_web_config_routes(monkeypatch, fresh_settings, tmp_path):
    """web.config에는 있으나 DB 프로젝트로 등록되지 않은 항목을 이름·rewrite 주소로
    별도 표시한다(등록된 프로젝트는 unregistered에 나오지 않는다)."""
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    monkeypatch.setenv("PAAS_PROXY_BACKEND", "iis")
    monkeypatch.setenv("PAAS_IIS_SITES_ROOT", str(tmp_path))
    get_settings.cache_clear()

    c = _client()
    _create_project(c, "shop-web")  # 등록된 프로젝트
    routes = tmp_path / "_base" / "routes"
    routes.mkdir(parents=True)
    # 등록 프로젝트의 조각(unregistered에 나오면 안 됨)
    (routes / "shop-web.xml").write_text(
        '<rule name="shop-web-path-0"><match url="^apps/_/shop-web/(.*)" />'
        '<action type="Rewrite" url="http://127.0.0.1:8101/{R:1}" /></rule>',
        encoding="utf-8",
    )
    # 미등록 항목 — 프로젝트로 등록되지 않은 legacy 라우트
    (routes / "legacy-portal.xml").write_text(
        '<rule name="legacy-portal-path-0"><match url="^legacy/(.*)" />'
        '<action type="Rewrite" url="http://127.0.0.1:9000/{R:1}" /></rule>',
        encoding="utf-8",
    )

    body = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    names = {u["name"] for u in body["unregistered"]}
    assert names == {"legacy-portal"}  # 등록된 shop-web은 제외
    legacy = next(u for u in body["unregistered"] if u["name"] == "legacy-portal")
    assert legacy["rewrite_targets"] == ["http://127.0.0.1:9000/{R:1}"]


def test_server_config_unregistered_empty_for_caddy(monkeypatch, fresh_settings):
    """caddy 백엔드는 설정을 추적하지 않으므로 unregistered는 빈 목록이다."""
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    c = _client()
    _create_project(c)
    body = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    assert body["unregistered"] == []


def test_server_config_in_proxy_none_for_caddy(monkeypatch, fresh_settings):
    """caddy 백엔드는 설정 멤버십을 추적하지 않으므로 in_proxy=None(프런트는 기존처럼
    상태로만 연결을 판단)."""
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    c = _client()
    pid = _create_project(c)
    body = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    site = next(s for s in body["sites"] if s["project_id"] == pid)
    assert site["in_proxy"] is None


def test_redirect_crud_flow(fresh_settings):
    c = _client()
    pid = _create_project(c)

    r = c.post(f"/paas/api/v1/projects/{pid}/redirects", json={
        "from_path": "/old", "to_path": "/new", "kind": "redirect", "status_code": 301,
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["project_id"] == pid
    assert rule["kind"] == "redirect"

    listing = c.get(f"/paas/api/v1/projects/{pid}/redirects", headers=ADMIN).json()
    assert len(listing) == 1

    server_cfg = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    release = next(s for s in server_cfg["sites"] if s["project_id"] == pid and s["profile"] == "release")
    assert release["redirect_count"] == 1
    assert release["redirects"] == [
        {"from_path": "/old", "to_path": "/new", "kind": "redirect", "status_code": 301},
    ]
    # 프로필과 무관하게 동일 규칙이 반영된다(RedirectRule은 profile로 구분되지 않음)
    dev = next(s for s in server_cfg["sites"] if s["project_id"] == pid and s["profile"] == "development")
    assert dev["redirects"] == release["redirects"]

    assert c.delete(f"/paas/api/v1/redirects/{rule['id']}", headers=ADMIN).status_code == 204
    assert c.get(f"/paas/api/v1/projects/{pid}/redirects", headers=ADMIN).json() == []

    server_cfg_after = c.get("/paas/api/v1/server-config", headers=ADMIN).json()
    release_after = next(
        s for s in server_cfg_after["sites"] if s["project_id"] == pid and s["profile"] == "release"
    )
    assert release_after["redirect_count"] == 0
    assert release_after["redirects"] == []


def test_redirect_defaults_kind_and_status(fresh_settings):
    c = _client()
    pid = _create_project(c)
    r = c.post(f"/paas/api/v1/projects/{pid}/redirects", json={
        "from_path": "/a", "to_path": "/b",
    }, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["kind"] == "redirect"
    assert r.json()["status_code"] == 302


def test_redirect_rewrite_kind(fresh_settings):
    c = _client()
    pid = _create_project(c)
    r = c.post(f"/paas/api/v1/projects/{pid}/redirects", json={
        "from_path": "/internal", "to_path": "/v2/internal", "kind": "rewrite",
    }, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["kind"] == "rewrite"


def test_redirect_unknown_project_404(fresh_settings):
    c = _client()
    r = c.post("/paas/api/v1/projects/999999/redirects", json={"from_path": "/a", "to_path": "/b"}, headers=ADMIN)
    assert r.status_code == 404
    assert c.get("/paas/api/v1/projects/999999/redirects", headers=ADMIN).status_code == 404


def test_delete_unknown_redirect_404(fresh_settings):
    c = _client()
    assert c.delete("/paas/api/v1/redirects/999999", headers=ADMIN).status_code == 404
