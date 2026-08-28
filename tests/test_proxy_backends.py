"""리버스프록시 백엔드 — Caddy(기본)/IIS/Apache 서브패스 라우팅 + redirect/rewrite 반영."""
import subprocess

from app.config import get_settings
from app.models import BuildProfile
from app.services import proxy
from app.services.proxy.apache_proxy import ApacheProxy
from app.services.proxy.base import PathRoute, RedirectSpec
from app.services.proxy.caddy_proxy import CaddyProxy
from app.services.proxy.iis_proxy import IISProxy
from app.services.runtime.base import Endpoint

ENDPOINT = Endpoint(host="127.0.0.1", port=8123)
REDIRECTS = [
    RedirectSpec(from_path="/old", to_path="/new", kind="redirect", status_code=301),
    RedirectSpec(from_path="/internal", to_path="/v2/internal", kind="rewrite"),
]

BACKEND_ENDPOINT = Endpoint(host="127.0.0.1", port=8001)
FRONTEND_ENDPOINT = Endpoint(host="127.0.0.1", port=8002)


def _composite_routes(base_prefix: str) -> list[PathRoute]:
    return [
        PathRoute(path_prefix=base_prefix + "api/", endpoint=BACKEND_ENDPOINT),
        PathRoute(path_prefix=base_prefix, endpoint=FRONTEND_ENDPOINT),
    ]


def test_get_proxy_selects_backend(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_PROXY_BACKEND", "iis")
    get_settings.cache_clear()
    assert isinstance(proxy.get_proxy(), IISProxy)

    monkeypatch.setenv("PAAS_PROXY_BACKEND", "apache")
    get_settings.cache_clear()
    assert isinstance(proxy.get_proxy(), ApacheProxy)

    monkeypatch.setenv("PAAS_PROXY_BACKEND", "caddy")
    get_settings.cache_clear()
    assert isinstance(proxy.get_proxy(), CaddyProxy)


def test_domain_for_is_shared_base_domain_by_default(fresh_settings):
    """모든 배포 URL은 base_domain 서브패스(Sub Path)로 통일한다 — 프로젝트별 커스텀
    도메인은 없다(입력만 받고 버리던 필드였고, 세 백엔드의 전용 사이트 분기는 그래서
    한 번도 실행되지 않았다)."""
    assert proxy.domain_for("shop", BuildProfile.release) == "apps.test"
    assert proxy.domain_for("shop", BuildProfile.development) == "apps.test"


def test_path_prefix_for_org_and_legacy_and_dev(fresh_settings):
    assert proxy.path_prefix_for("acme", "shop", BuildProfile.release) == "/apps/acme/shop/"
    assert proxy.path_prefix_for("acme", "shop", BuildProfile.development) == "/apps/acme/shop/dev/"
    assert proxy.path_prefix_for(None, "shop", BuildProfile.release) == "/apps/_/shop/"


def test_domain_and_path_prefix_unaffected_on_enterprise_tier(monkeypatch, fresh_settings):
    """2차(K8s)는 서브패스 라우팅 대상이 아니다 — 프로젝트당 서브도메인 1개 그대로."""
    monkeypatch.setenv("PAAS_TIER", "enterprise")
    get_settings.cache_clear()
    assert proxy.domain_for("shop", BuildProfile.release) == "shop.apps.test"
    assert proxy.domain_for("shop", BuildProfile.development) == "shop-dev.apps.test"
    assert proxy.path_prefix_for("acme", "shop", BuildProfile.release) == "/"


def test_caddy_configure_shared_writes_handle_path_snippet_and_base_site(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_CADDY_SITES_DIR", str(tmp_path))
    monkeypatch.setenv("PAAS_BASE_DOMAIN", "apps.test")
    get_settings.cache_clear()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))

    CaddyProxy().configure("shop", BuildProfile.release, "apps.test", "/acme/shop/", ENDPOINT, REDIRECTS)

    snippet = (tmp_path / "handles" / "shop.caddy").read_text(encoding="utf-8")
    assert "handle_path /acme/shop/* {" in snippet
    assert "reverse_proxy 127.0.0.1:8123" in snippet
    assert "redir /old /new 301" in snippet
    assert "rewrite /internal /v2/internal" in snippet

    base_site = (tmp_path / "_base.caddy").read_text(encoding="utf-8")
    assert "apps.test {" in base_site
    assert "import" in base_site and "handles" in base_site


def test_iis_configure_shared_writes_fragment_and_regenerates_base(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_IIS_SITES_ROOT", str(tmp_path / "sites"))
    monkeypatch.setenv("PAAS_IIS_APPCMD_PATH", "appcmd.exe")
    monkeypatch.setenv("PAAS_BASE_DOMAIN", "apps.test")
    get_settings.cache_clear()

    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda args, **kw: (calls.append(args), _Ok())[1],
    )

    IISProxy().configure("shop", BuildProfile.release, "apps.test", "/acme/shop/", ENDPOINT, REDIRECTS)

    fragment = (tmp_path / "sites" / "apps" / "shop" / "route.xml").read_text(encoding="utf-8")
    # release 규칙은 dev/ 조각을 뺀다 — 아래 test_release_rule_does_not_swallow_dev 참고
    assert 'match url="^acme/shop/(?!dev/)(.*)"' in fragment
    assert "http://127.0.0.1:8123/{R:1}" in fragment
    assert 'match url="^acme/shop/old$"' in fragment  # redirect가 조직/프로젝트 접두사를 명시적으로 반영

    base_config = (tmp_path / "sites" / "_base" / "web.config").read_text(encoding="utf-8")
    assert "acme/shop" in base_config
    assert any("_base" in a for call in calls for a in call)


def test_iis_configure_raises_clear_error_when_arr_not_installed(monkeypatch, tmp_path, fresh_settings):
    """ARR 미설치 시 URL Rewrite 규칙은 매칭되지만 응답이 안 오는(502/무응답) 상태로
    조용히 배포가 "성공"하면 안 된다 — appcmd가 실패하면 바로 명확한 에러로 드러난다."""
    monkeypatch.setenv("PAAS_IIS_SITES_ROOT", str(tmp_path / "sites"))
    monkeypatch.setenv("PAAS_IIS_APPCMD_PATH", "appcmd.exe")
    get_settings.cache_clear()

    def fake_run(args, **kw):
        if args[1:3] == ["set", "config"]:
            return _Fail()
        return _Ok()

    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        IISProxy().configure("shop", BuildProfile.release, "shop.example.com", "/", ENDPOINT, [])
        raised = False
    except Exception as e:
        raised = True
        assert "ARR" in str(e)
    assert raised


def test_iis_regenerate_base_preserves_foreign_web_config_content(monkeypatch, tmp_path, fresh_settings):
    """공유(_base) 사이트도 마찬가지 — 조각 파일 재합성이 기존 파일을 통째로
    덮어쓰지 않고 관리 블록만 갈아끼운다."""
    monkeypatch.setenv("PAAS_IIS_SITES_ROOT", str(tmp_path / "sites"))
    monkeypatch.setenv("PAAS_IIS_APPCMD_PATH", "appcmd.exe")
    monkeypatch.setenv("PAAS_BASE_DOMAIN", "apps.test")
    get_settings.cache_clear()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Ok())

    base_dir = tmp_path / "sites" / "_base"
    base_dir.mkdir(parents=True)
    (base_dir / "web.config").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<configuration>\n"
        "  <system.webServer>\n"
        '    <httpErrors errorMode="Custom" />\n'
        "    <rewrite>\n"
        "      <rules>\n"
        '        <rule name="hand-written" />\n'
        "      </rules>\n"
        "    </rewrite>\n"
        "  </system.webServer>\n"
        "</configuration>\n",
        encoding="utf-8",
    )

    IISProxy().configure("shop", BuildProfile.release, "apps.test", "/acme/shop/", ENDPOINT, [])

    web_config = (base_dir / "web.config").read_text(encoding="utf-8")
    assert 'errorMode="Custom"' in web_config
    assert 'name="hand-written"' in web_config
    assert "paas:managed:begin" in web_config
    assert "acme/shop" in web_config


def test_iis_splice_creates_missing_rewrite_and_rules_containers():
    """<rewrite>/<rules>가 아직 없어도(예: <system.webServer>만 있는 파일) 있는
    구조만 감싸서 새로 만들고, 재실행하면 마커를 찾아 멱등하게 갈아끼운다."""
    from app.services.proxy.iis_proxy import _splice_managed_rules

    existing = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<configuration>\n"
        "  <system.webServer>\n"
        '    <httpErrors errorMode="Custom" />\n'
        "  </system.webServer>\n"
        "</configuration>\n"
    )
    result = _splice_managed_rules(existing, '<rule name="x" />\n')
    assert '<httpErrors errorMode="Custom" />' in result
    assert "<rewrite>" in result and "<rules>" in result
    assert 'name="x"' in result

    result2 = _splice_managed_rules(result, '<rule name="x" />\n')
    assert result == result2


def test_iis_splice_builds_full_structure_from_bare_configuration():
    from app.services.proxy.iis_proxy import _SKELETON, _splice_managed_rules

    result = _splice_managed_rules(_SKELETON, '<rule name="x" />\n')
    assert "<system.webServer>" in result and "<rewrite>" in result and "<rules>" in result
    assert 'name="x"' in result


def test_iis_splice_raises_on_malformed_config_without_configuration_element():
    from app.services.proxy.iis_proxy import _splice_managed_rules

    try:
        _splice_managed_rules("not xml at all", "<rule />\n")
        raised = False
    except Exception:
        raised = True
    assert raised


def test_iis_remove_shrinks_managed_block_without_touching_foreign_content(monkeypatch, tmp_path, fresh_settings):
    """배포 제거(remove)도 관리 블록만 다시 계산해서 갈아끼운다 — 플랫폼이 모르는
    기존 규칙은 추가 때와 마찬가지로 그대로 남는다."""
    monkeypatch.setenv("PAAS_IIS_SITES_ROOT", str(tmp_path / "sites"))
    monkeypatch.setenv("PAAS_IIS_APPCMD_PATH", "appcmd.exe")
    monkeypatch.setenv("PAAS_BASE_DOMAIN", "apps.test")
    get_settings.cache_clear()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Ok())

    base_dir = tmp_path / "sites" / "_base"
    base_dir.mkdir(parents=True)
    (base_dir / "web.config").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<configuration>\n"
        "  <system.webServer>\n"
        '    <httpErrors errorMode="Custom" />\n'
        "    <rewrite>\n"
        "      <rules>\n"
        '        <rule name="hand-written" />\n'
        "      </rules>\n"
        "    </rewrite>\n"
        "  </system.webServer>\n"
        "</configuration>\n",
        encoding="utf-8",
    )

    IISProxy().configure("shop", BuildProfile.release, "apps.test", "/acme/shop/", ENDPOINT, [])
    after_add = (base_dir / "web.config").read_text(encoding="utf-8")
    assert "acme/shop" in after_add

    IISProxy().remove("shop", BuildProfile.release)
    after_remove = (base_dir / "web.config").read_text(encoding="utf-8")
    assert "acme/shop" not in after_remove
    assert 'errorMode="Custom"' in after_remove
    assert 'name="hand-written"' in after_remove


def test_iis_configure_raises_on_add_failure(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_IIS_SITES_ROOT", str(tmp_path / "sites"))
    get_settings.cache_clear()

    def fake_run(args, **kw):
        if "add" in args:
            return _Fail()
        return _Ok()


def test_iis_parse_real_world_sample_web_config(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_IIS_SITES_ROOT", str(tmp_path / "sites"))
    get_settings.cache_clear()

    sample_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <system.webServer>
    <webSocket enabled="true" />
    <rewrite>
      <rules>
        <clear />
        <rule name="negowith" enabled="true" stopProcessing="true">
            <match url="^negowith/?" />
            <action type="Rewrite" url="http://localhost:8090/{R:0}" />
        </rule>
        <rule name="negowith/supplier" stopProcessing="true">
            <match url="^negowith/supplier(/.*)?" />
            <action type="Rewrite" url="http://localhost:8501/{R:0}" />
        </rule>
        <rule name="corekeeper/newsupplier" stopProcessing="true">
            <match url="^corekeeper/newsupplier(/.*)?" />
            <action type="Rewrite" url="http://localhost:8512/{R:0}" />
        </rule>
        <rule name="gemini" enabled="true" stopProcessing="true">
            <match url="^gemini/?" />
            <action type="Redirect" url="https://vertexaisearch.cloud.google.com/home" />
        </rule>
        <rule name="codingagent" stopProcessing="true">
            <match url="^codingagent(/.*)?" />
            <action type="Rewrite" url="http://localhost:8520/{R:0}" />
        </rule>
      </rules>
    </rewrite>
  </system.webServer>
</configuration>'''

    (tmp_path / "sites").mkdir(parents=True)
    (tmp_path / "sites" / "web.config").write_text(sample_xml, encoding="utf-8")

    routes = IISProxy().configured_routes()
    routes_dict = dict(routes)

    assert "negowith" in routes_dict
    assert "http://localhost:8090/{R:0}" in routes_dict["negowith"]
    assert "http://localhost:8501/{R:0}" in routes_dict["negowith"]

    assert "corekeeper" in routes_dict
    assert "http://localhost:8512/{R:0}" in routes_dict["corekeeper"]

    assert "gemini" in routes_dict
    assert "https://vertexaisearch.cloud.google.com/home" in routes_dict["gemini"]

    assert "codingagent" in routes_dict
    assert "http://localhost:8520/{R:0}" in routes_dict["codingagent"]


def test_apache_configure_shared_writes_handle_fragment(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_APACHE_SITES_DIR", str(tmp_path))
    monkeypatch.setenv("PAAS_BASE_DOMAIN", "apps.test")
    get_settings.cache_clear()
    reload_calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda args, **kw: (reload_calls.append(args), _Ok())[1],
    )

    ApacheProxy().configure("shop", BuildProfile.release, "apps.test", "/acme/shop/", ENDPOINT, REDIRECTS)

    fragment = (tmp_path / "handles" / "shop.conf").read_text(encoding="utf-8")
    assert "ProxyPass /acme/shop/ http://127.0.0.1:8123/" in fragment
    assert "Redirect 301 /acme/shop/old /acme/shop/new" in fragment
    assert reload_calls

    base_conf = (tmp_path / "_base.conf").read_text(encoding="utf-8")
    assert "ServerName apps.test" in base_conf
    assert "IncludeOptional" in base_conf and "handles" in base_conf
    assert not (tmp_path / "shop.conf").exists()


def test_apache_reload_missing_binary_is_silent(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_APACHE_SITES_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    ApacheProxy().configure("shop", BuildProfile.release, "shop.example.com", "/", ENDPOINT, [])  # 예외 없이 통과


def test_apache_remove_deletes_dedicated_and_shared_fragment(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_APACHE_SITES_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Ok())

    ApacheProxy().configure("shop", BuildProfile.release, "apps.test", "/acme/shop/", ENDPOINT, [])
    fragment = tmp_path / "handles" / "shop.conf"
    assert fragment.exists()

    ApacheProxy().remove("shop", BuildProfile.release)
    assert not fragment.exists()


def test_caddy_configure_paths_splits_by_prefix_shared(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_CADDY_SITES_DIR", str(tmp_path))
    monkeypatch.setenv("PAAS_BASE_DOMAIN", "apps.test")
    get_settings.cache_clear()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))

    CaddyProxy().configure_paths(
        "shop", BuildProfile.release, "apps.test", _composite_routes("/acme/shop/"), [],
    )
    content = (tmp_path / "handles" / "shop.caddy").read_text(encoding="utf-8")
    assert "handle_path /acme/shop/api/* {" in content
    assert "reverse_proxy 127.0.0.1:8001" in content
    assert "reverse_proxy 127.0.0.1:8002" in content
    # 구체적 경로(api)가 캐치올(/acme/shop/*)보다 먼저 와야 한다
    assert content.index("handle_path /acme/shop/api/*") < content.index("reverse_proxy 127.0.0.1:8002")


def test_iis_configure_paths_routes_prefix_before_catchall_shared(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_IIS_SITES_ROOT", str(tmp_path / "sites"))
    monkeypatch.setenv("PAAS_IIS_APPCMD_PATH", "appcmd.exe")
    monkeypatch.setenv("PAAS_BASE_DOMAIN", "apps.test")
    get_settings.cache_clear()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Ok())

    IISProxy().configure_paths(
        "shop", BuildProfile.release, "apps.test", _composite_routes("/acme/shop/"), [],
    )
    fragment = (tmp_path / "sites" / "apps" / "shop" / "route.xml").read_text(encoding="utf-8")
    assert 'match url="^acme/shop/api/(.*)"' in fragment
    assert "http://127.0.0.1:8001/{R:1}" in fragment  # backend (prefix rule)
    assert "http://127.0.0.1:8002/{R:1}" in fragment  # frontend (catch-all)
    assert fragment.index('name="shop-path-0"') < fragment.index('name="shop-path-1"')


def test_apache_configure_paths_proxies_prefix_before_root_shared(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_APACHE_SITES_DIR", str(tmp_path))
    monkeypatch.setenv("PAAS_BASE_DOMAIN", "apps.test")
    get_settings.cache_clear()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Ok())

    ApacheProxy().configure_paths(
        "shop", BuildProfile.release, "apps.test", _composite_routes("/acme/shop/"), [],
    )
    fragment = (tmp_path / "handles" / "shop.conf").read_text(encoding="utf-8")
    assert "ProxyPass /acme/shop/api/ http://127.0.0.1:8001/" in fragment
    assert "ProxyPass /acme/shop/ http://127.0.0.1:8002/" in fragment
    assert fragment.index("ProxyPass /acme/shop/api/") < fragment.index("ProxyPass /acme/shop/ http")


def test_iis_splice_managed_rules_places_before_default_rule():
    from app.services.proxy.iis_proxy import _splice_managed_rules
    sample_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<configuration>\n'
        '  <system.webServer>\n'
        '    <rewrite>\n'
        '      <rules>\n'
        '        <rule name="git" stopProcessing="true">\n'
        '          <match url="^git/?(.*)$" />\n'
        '          <action type="Rewrite" url="http://localhost:3000/{R:1}" />\n'
        '        </rule>\n'
        '        <rule name="default" stopProcessing="true">\n'
        '          <match url="(.*)" />\n'
        '          <action type="Rewrite" url="http://localhost:3000/{R:0}" />\n'
        '        </rule>\n'
        '      </rules>\n'
        '    </rewrite>\n'
        '  </system.webServer>\n'
        '</configuration>\n'
    )
    rule_blocks = '<rule name="paas-chatbot"><match url="^apps/chatbot/(.*)" /><action type="Rewrite" url="http://127.0.0.1:8100/{R:1}" /></rule>\n'
    result = _splice_managed_rules(sample_xml, rule_blocks)
    assert '<!-- paas:managed:begin -->' in result
    assert result.index('<!-- paas:managed:begin -->') < result.index('name="default"')


class _Ok:
    returncode = 0
    stdout = ""
    stderr = ""


class _Fail:
    returncode = 1
    stdout = ""
    stderr = "boom"


def test_iis_example_web_config_survives_platform_splice():
    """infra/gitea/web.config.example의 수동 규칙(/gitea·/paas)이 플랫폼 배포 후에도
    남아 있어야 한다 — 그 파일이 "마커 밖은 안 건드린다"를 전제로 안내하고 있고,
    실제로 깨지면 서브패스 라우팅이 통째로 사라져 원인 찾기가 매우 어렵다."""
    import xml.dom.minidom
    from pathlib import Path

    from app.services.proxy.iis_proxy import MANAGED_BEGIN, MANAGED_END, _splice_managed_rules

    example = Path(__file__).resolve().parent.parent / "infra" / "gitea" / "web.config.example"
    original = example.read_text(encoding="utf-8")
    xml.dom.minidom.parseString(original)  # 예시 자체가 유효한 XML이어야 한다

    spliced = _splice_managed_rules(original, '        <rule name="app-path-0" />\n')
    xml.dom.minidom.parseString(spliced)

    for rule in ('name="gitea"', 'name="paas"', 'name="paas-console"', 'name="gitea-root-slash"'):
        assert rule in spliced, f"수동 규칙 {rule}이 사라졌다"
    assert 'maxAllowedContentLength' in spliced  # git push 크기 제한도 마커 밖이다

    # Gitea는 서브패스를 스스로 벗기지 않으므로 프록시가 벗겨야 한다 — 타겟에 /gitea를
    # 다시 붙이면 Gitea가 모르는 경로가 돼 전부 404다. 반대로 플랫폼은 라우터를 /paas
    # 아래에 등록하므로(main.PAAS_PREFIX) 벗기면 안 된다. 한 파일에 규칙이 반대로 들어
    # 있어 "통일"하고 싶어지는 자리라 여기서 고정한다.
    assert 'url="http://localhost:3000/{R:1}"' in original
    assert "3000/gitea/" not in original
    assert 'url="http://localhost:7000/paas/{R:1}"' in original

    # preConditions는 outboundRules 안에서만 유효하다 — <rewrite> 바로 아래 두면
    # 설정 파싱이 통째로 실패해 사이트 전 경로가 500.19가 된다.
    assert original.count("<preConditions>") == 1
    assert original.index("<outboundRules>") < original.index("<preConditions>")
    assert spliced.count(MANAGED_BEGIN) == 1 and spliced.count(MANAGED_END) == 1
    assert 'name="app-path-0"' in spliced.split(MANAGED_BEGIN)[1].split(MANAGED_END)[0]

    # 두 번째 배포가 첫 번째 블록을 갈아끼우고 수동 규칙은 그대로 둔다.
    again = _splice_managed_rules(spliced, '        <rule name="app-path-1" />\n')
    assert 'name="app-path-0"' not in again and 'name="app-path-1"' in again
    assert 'name="gitea"' in again and 'name="paas"' in again


def test_dev_route_is_not_stripped_by_any_backend(monkeypatch, tmp_path, fresh_settings):
    """Vite dev 서버는 자기 공개 경로(base)가 붙은 요청만 받는다 — 접두사를 벗기면
    /@vite/client 같은 요청이 어긋나 화면이 뜨지 않는다. 빌드본은 반대로 벗겨야 한다
    (HTML에 전체 경로가 박혀 있고 서버는 루트에서 서빙한다)."""
    from app.services.proxy.base import PathRoute
    from app.services.proxy.caddy_proxy import _path_block
    from app.services.proxy.iis_proxy import _rewrite_target
    from app.services.runtime.base import Endpoint

    ep = Endpoint(host="localhost", port=8123)
    kept = PathRoute(path_prefix="/apps/org/shop/dev/", endpoint=ep, strip_prefix=False)
    stripped = PathRoute(path_prefix="/apps/org/shop/", endpoint=ep)

    # Caddy: handle_path는 벗기고, handle은 그대로 넘긴다.
    assert "handle_path" in _path_block(stripped, [])
    assert "handle_path" not in _path_block(kept, [])
    assert "handle /apps/org/shop/dev/*" in _path_block(kept, [])

    # IIS: {R:1}은 캡처만, {R:0}은 매칭 전체(접두사 포함)를 넘긴다.
    assert _rewrite_target(stripped).endswith("/{R:1}")
    assert _rewrite_target(kept).endswith("/{R:0}")


def test_apache_keeps_prefix_by_repeating_it_on_the_upstream(fresh_settings):
    """mod_proxy의 ProxyPass는 지정한 접두사를 스스로 벗긴다 — 벗기지 않으려면
    업스트림 쪽에도 같은 경로를 붙여야 결과적으로 원래 경로가 그대로 전달된다."""
    from app.services.proxy.apache_proxy import _path_directives
    from app.services.proxy.base import PathRoute
    from app.services.runtime.base import Endpoint

    ep = Endpoint(host="localhost", port=8123)
    kept = _path_directives([PathRoute("/apps/org/shop/dev/", ep, strip_prefix=False)])
    stripped = _path_directives([PathRoute("/apps/org/shop/", ep)])
    assert "http://localhost:8123/apps/org/shop/dev/" in kept
    assert "http://localhost:8123/\n" in stripped or "http://localhost:8123/ " in stripped



# --- release와 dev가 섞이지 않는가 ---

def _iis_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PAAS_IIS_SITES_ROOT", str(tmp_path / "sites"))
    monkeypatch.setenv("PAAS_IIS_APPCMD_PATH", "appcmd.exe")
    monkeypatch.setenv("PAAS_BASE_DOMAIN", "apps.test")
    get_settings.cache_clear()
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: _Ok())


def _composed_rules(tmp_path) -> list[tuple[str, str, str]]:
    """합성된 web.config에서 (규칙 이름, match 패턴, 타겟)을 **파일에 적힌 순서대로**."""
    import re

    xml = (tmp_path / "sites" / "_base" / "web.config").read_text(encoding="utf-8")
    return re.findall(
        r'<rule name="([^"]+)".*?<match url="([^"]+)".*?<action[^>]*url="([^"]+)"', xml, re.S)


def _route(rules, url: str) -> str:
    """IIS URL Rewrite처럼 위에서부터 보고 첫 매칭에서 멈춘다(stopProcessing="true")."""
    import re

    for name, pattern, target in rules:
        m = re.match(pattern, url)
        if m:
            return target.replace("{R:1}", m.group(1) if m.groups() else "")
    return "(매칭 없음)"


def test_release_rule_does_not_swallow_dev(monkeypatch, tmp_path, fresh_settings):
    """**dev 경로가 release 경로 안에 있다**(/apps/_/shop/ ⊃ /apps/_/shop/dev/).

    두 규칙을 겹친 채로 두면 어느 쪽이 먼저 놓이느냐가 라우팅을 정하는데, 그 순서를
    정하는 것은 조각 파일 정렬이라는 눈에 안 보이는 성질이었다 — 그리고 실제로 release가
    앞에 놓여 dev 요청이 전부 release 업스트림으로 갔다. 패턴을 서로소로 만들어 순서에
    기대지 않는다.
    """
    _iis_env(monkeypatch, tmp_path)
    release = proxy.path_prefix_for(None, "shop", BuildProfile.release)
    dev = proxy.path_prefix_for(None, "shop", BuildProfile.development)
    assert dev.startswith(release)   # 이 포함관계가 문제의 뿌리다

    IISProxy().configure("shop", BuildProfile.release, "apps.test", release,
                         Endpoint(host="127.0.0.1", port=8001), [])
    IISProxy().configure("shop", BuildProfile.development, "apps.test", dev,
                         Endpoint(host="127.0.0.1", port=8002), [])

    rules = _composed_rules(tmp_path)
    assert _route(rules, "apps/_/shop/index.html") == "http://127.0.0.1:8001/index.html"
    assert _route(rules, "apps/_/shop/dev/index.html") == "http://127.0.0.1:8002/index.html"
    # 조각 경계까지 본다 — "dev"로 시작하는 파일 이름은 dev 배포가 아니다
    assert _route(rules, "apps/_/shop/devtools.js") == "http://127.0.0.1:8001/devtools.js"

    # 그리고 **순서가 뒤바뀌어도** 같은 답이 나온다(그것이 서로소의 뜻이다)
    assert _route(list(reversed(rules)), "apps/_/shop/dev/a") == "http://127.0.0.1:8002/a"
    assert _route(list(reversed(rules)), "apps/_/shop/a") == "http://127.0.0.1:8001/a"


def test_dev_fragment_itself_is_not_guarded(monkeypatch, tmp_path, fresh_settings):
    """선읽기는 release 쪽에만 붙는다 — dev 규칙에 붙으면 /dev/dev/... 를 스스로 막는다."""
    _iis_env(monkeypatch, tmp_path)
    dev = proxy.path_prefix_for(None, "shop", BuildProfile.development)
    IISProxy().configure("shop", BuildProfile.development, "apps.test", dev,
                         Endpoint(host="127.0.0.1", port=8002), [])
    from app.services.proxy.base import site_name

    frag_dir = site_name("shop", BuildProfile.development)
    fragment = (tmp_path / "sites" / "apps" / frag_dir / "route.xml").read_text(encoding="utf-8")
    assert 'match url="^apps/_/shop/dev/(.*)"' in fragment
    assert "(?!" not in fragment


def test_composite_subpaths_are_not_guarded(monkeypatch, tmp_path, fresh_settings):
    """dev를 삼킬 수 있는 것은 프로젝트 루트 규칙 하나뿐이다.

    /api/ 같은 하위 경로에까지 선읽기를 붙이면 앱이 실제로 가진 /api/dev/... 경로를
    플랫폼이 막아 버린다.
    """
    _iis_env(monkeypatch, tmp_path)
    base = proxy.path_prefix_for(None, "shop", BuildProfile.release)
    IISProxy().configure_paths("shop", BuildProfile.release, "apps.test",
                               _composite_routes(base), [])

    rules = _composed_rules(tmp_path)
    assert _route(rules, "apps/_/shop/api/dev/report") == "http://127.0.0.1:8001/dev/report"
    assert _route(rules, "apps/_/shop/dev/index.html") == "(매칭 없음)"   # dev 배포가 받는다



# --- site_name이 단사인가 ---

def test_site_name_cannot_collide_between_a_project_and_another_projects_dev(fresh_settings):
    """프로젝트 이름 규칙(^[a-z0-9][a-z0-9-]{1,40}$)에 하이픈이 있다.

    접미사가 "-dev"이던 때는 프로젝트 shop의 dev 배포와 프로젝트 shop-dev의 release
    배포가 **같은 이름**이 됐다 — 조각 파일 하나를 공유해서 뒤에 배포한 쪽이 앞엣것의
    라우트를 덮어쓰고, remove()가 남의 것을 지웠다.
    """
    from app.services.proxy.base import site_name

    names = {
        site_name("shop", BuildProfile.development),
        site_name("shop-dev", BuildProfile.release),
        site_name("shop-dev", BuildProfile.development),
        site_name("shop", BuildProfile.release),
    }
    assert len(names) == 4


def test_two_projects_that_used_to_collide_keep_separate_fragments(
        monkeypatch, tmp_path, fresh_settings):
    _iis_env(monkeypatch, tmp_path)
    for project, profile, port in (("shop", BuildProfile.development, 8002),
                                   ("shop-dev", BuildProfile.release, 9001)):
        IISProxy().configure(
            project, profile, "apps.test",
            proxy.path_prefix_for(None, project, profile),
            Endpoint(host="127.0.0.1", port=port), [])

    rules = _composed_rules(tmp_path)
    assert _route(rules, "apps/_/shop/dev/a") == "http://127.0.0.1:8002/a"
    assert _route(rules, "apps/_/shop-dev/a") == "http://127.0.0.1:9001/a"

    # remove가 남의 조각을 지우지 않는다
    IISProxy().remove("shop-dev", BuildProfile.release)
    rules = _composed_rules(tmp_path)
    assert _route(rules, "apps/_/shop/dev/a") == "http://127.0.0.1:8002/a"
    assert _route(rules, "apps/_/shop-dev/a") == "(매칭 없음)"


def test_dev_suffix_keeps_apache_glob_ordering(fresh_settings):
    """Apache는 IncludeOptional handles/*.conf의 **글롭 순서**가 곧 ProxyPass 우선순위다.

    dev 경로가 release 경로 안에 있으므로 dev 조각이 먼저 읽혀야 한다. 접미사 문자의
    ASCII가 '.'(46)보다 크면 그 순서가 뒤집혀 dev가 release로 새어 들어간다 —
    '_'(95)나 '~'(126)로 바꾸면 그렇게 된다. 눈에 안 보이는 제약이라 여기에 못 박아 둔다.
    """
    from app.services.proxy.base import site_name

    release = f"{site_name('shop', BuildProfile.release)}.conf"
    dev = f"{site_name('shop', BuildProfile.development)}.conf"
    assert sorted([release, dev])[0] == dev
