"""1차(small) 리버스프록시 대안 — IIS (URL Rewrite 모듈 사용, Windows 전용).

배포 URL은 서브패스 기반이다: 프로젝트들은 기본적으로 base_domain 하나를 공유하고
/{조직}/{프로젝트}/ 경로로 구분된다(services/proxy/__init__.py의 path_prefix_for).
IIS URL Rewrite는 Caddy의 "import 디렉터리 글롭"이나 Apache의 IncludeOptional 같은
다중 파일 결합 수단이 없어, 프로젝트별로 규칙 조각(XML fragment) 파일 하나를
routes/ 아래 쓰고, 배포/제거 때마다 그 조각들을 모아 base 사이트의 web.config
하나로 다시 합성한다(_regenerate_base_web_config). 조각 파일명은 site_name만으로
정해지므로(조직 정보 불필요) remove()가 project_name/profile만으로도 정확히
지울 수 있다 — 기존 인터페이스를 그대로 유지한다.

release 배포에 커스텀 도메인(project.domain)을 지정한 예외만 기존처럼 독립된
사이트(자기 physicalPath·바인딩)를 그대로 쓴다.

web.config는 플랫폼이 전부 새로 쓰는 게 아니라, 기존 파일에 플랫폼이 정의하지 않은
부분(다른 IIS 기능 설정, 운영자가 직접 추가한 규칙 등)이 있을 수 있다는 전제로
읽기-수정-쓰기를 한다 — 플랫폼이 관리하는 규칙만 paas:managed 마커 사이에 넣고
갈아끼우고, 마커 밖은 절대 건드리지 않는다(_splice_managed_rules). 같은 기존
파일·같은 배포 상태를 다시 넣으면 항상 같은 바이트를 낸다(결정적, 멱등).
"""
import re
import subprocess

from ...config import get_settings
from ...models import BuildProfile
from ..runtime.base import Endpoint
from .base import PathRoute, RedirectSpec, ReverseProxy, site_name

REDIRECT_TYPES = {301: "Permanent", 302: "Found", 303: "SeeOther", 307: "Temporary"}

BASE_SITE_NAME = "_base"

MANAGED_BEGIN = "<!-- paas:managed:begin -->"
MANAGED_END = "<!-- paas:managed:end -->"
_SKELETON = '<?xml version="1.0" encoding="UTF-8"?>\n<configuration>\n</configuration>\n'

# 마커가 아직 없을 때(최초 생성 또는 플랫폼 도입 전부터 있던 파일) 규칙을 끼워 넣을
# 자리를 안쪽 컨테이너부터 바깥쪽 순서로 찾는다 — 있는 태그를 최대한 재사용하고,
# 없는 상위 구조만 새로 감싼다.
_FALLBACK_ANCHORS = (
    ("</rules>", lambda body: body),
    ("</rewrite>", lambda body: f"<rules>\n{body}</rules>\n"),
    ("</system.webServer>", lambda body: f"<rewrite>\n<rules>\n{body}</rules>\n</rewrite>\n"),
    ("</configuration>", lambda body: f"<system.webServer>\n<rewrite>\n<rules>\n{body}</rules>\n</rewrite>\n</system.webServer>\n"),
)


def _splice_managed_rules(existing_text: str, rule_blocks: str) -> str:
    """플랫폼이 관리하는 규칙만 paas:managed 마커 사이에 넣고 갈아끼운다 — 마커 밖의
    내용(플랫폼이 정의하지 않은 기존 web.config 구조)은 위치·내용 그대로 보존한다.
    마커가 없으면(최초 생성 또는 플랫폼 도입 전 파일) 가장 안쪽에 있는 기존 컨테이너
    닫는 태그(rules→rewrite→system.webServer→configuration 순) 바로 앞에 새로
    만든다. 같은 existing_text·rule_blocks 입력에는 항상 같은 결과를 낸다(재실행해도
    마커를 다시 찾아 같은 자리를 갈아끼우므로 멱등)."""
    managed_block = f"{MANAGED_BEGIN}\n{rule_blocks}{MANAGED_END}\n"

    begin_idx = existing_text.find(MANAGED_BEGIN)
    end_idx = existing_text.find(MANAGED_END)
    if begin_idx != -1 and end_idx != -1:
        return existing_text[:begin_idx] + managed_block + existing_text[end_idx + len(MANAGED_END):].lstrip("\n")

    for close_tag, wrap in _FALLBACK_ANCHORS:
        close_idx = existing_text.find(close_tag)
        if close_idx != -1:
            return existing_text[:close_idx] + wrap(managed_block) + existing_text[close_idx:]

    raise IISError(
        "web.config에 <configuration> 요소가 없어 규칙을 넣을 위치를 찾지 못했습니다 "
        "— 파일이 올바른 IIS 설정 XML인지 확인하세요."
    )


def _read_existing_or_skeleton(path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else _SKELETON


_REWRITE_TARGET_RE = re.compile(r'<action\s+type="Rewrite"\s+url="([^"]+)"', re.IGNORECASE)


def _rewrite_targets(xml: str) -> list[str]:
    """web.config 조각에서 Rewrite 액션의 타겟 URL을 순서·중복 유지 없이 뽑는다
    (플랫폼이 쓴 형식 기준 — 중복 제거하고 첫 등장 순서 유지)."""
    seen: list[str] = []
    for url in _REWRITE_TARGET_RE.findall(xml):
        if url not in seen:
            seen.append(url)
    return seen


class IISError(RuntimeError):
    pass


def _prefixed(path_prefix: str, sub_path: str) -> str:
    """redirect/rewrite의 from_path/to_path는 프로젝트 자신의 경로 기준이므로,
    공유 사이트(site 루트 기준 규칙만 지원)에서는 조직/프로젝트 접두사를 명시적으로
    붙여야 한다 — Caddy의 handle_path 자동 경로 스트리핑과 동일한 의미를 낸다."""
    return path_prefix.rstrip("/") + "/" + sub_path.lstrip("/")


def _rule_xml(name: str, match_url: str, r: RedirectSpec) -> str:
    if r.kind == "redirect":
        redirect_type = REDIRECT_TYPES.get(r.status_code, "Found")
        return (
            f'        <rule name="{name}" stopProcessing="true">\n'
            f'          <match url="^{match_url}$" />\n'
            f'          <action type="Redirect" url="{r.to_path}" redirectType="{redirect_type}" />\n'
            f'        </rule>\n'
        )
    return (
        f'        <rule name="{name}" stopProcessing="true">\n'
        f'          <match url="^{match_url}$" />\n'
        f'          <action type="Rewrite" url="{r.to_path}" />\n'
        f'        </rule>\n'
    )


def _path_rule_xml(name: str, route: PathRoute) -> str:
    prefix = route.path_prefix.strip("/")
    proxy_target = f"http://{route.endpoint.host}:{route.endpoint.port}/{{R:1}}"
    return (
        f'        <rule name="{name}" stopProcessing="true">\n'
        f'          <match url="^{prefix}/(.*)" />\n'
        f'          <action type="Rewrite" url="{proxy_target}" />\n'
        f'        </rule>\n'
    )


def _rule_blocks_for_paths(routes: list[PathRoute], redirects: list[RedirectSpec]) -> str:
    """비루트(prefix) 규칙을 먼저 매칭시키고, "/"는 캐치올로 마지막에 둔다 —
    매칭된 접두사는 업스트림에 전달되기 전에 제거된다(handle_path/ProxyPass와 동일 규약).
    문서 전체가 아니라 <rule> 블록들만 반환한다 — 나머지 web.config 구조는
    _splice_managed_rules가 기존 파일(또는 최소 골격) 기준으로 채운다."""
    rule_blocks = "".join(
        _rule_xml(f"redirect-{i}", r.from_path.lstrip("/"), r) for i, r in enumerate(redirects)
    )
    non_root = [r for r in routes if r.path_prefix not in ("/", "")]
    root = next((r for r in routes if r.path_prefix in ("/", "")), routes[-1])
    path_blocks = "".join(_path_rule_xml(f"path-{i}", r) for i, r in enumerate(non_root))
    proxy_target = f"http://{root.endpoint.host}:{root.endpoint.port}/{{R:1}}"
    return (
        f"{rule_blocks}"
        f"{path_blocks}"
        '        <rule name="reverse-proxy" stopProcessing="true">\n'
        '          <match url="(.*)" />\n'
        f'          <action type="Rewrite" url="{proxy_target}" />\n'
        "        </rule>\n"
    )


def _is_shared(domain: str) -> bool:
    return domain == get_settings().base_domain


def _routes_dir():
    settings = get_settings()
    d = settings.iis_sites_root / BASE_SITE_NAME / "routes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _route_fragment_file(project_name: str, profile: BuildProfile):
    return _routes_dir() / f"{site_name(project_name, profile)}.xml"


def _build_shared_fragment(
    frag_key: str, routes: list[PathRoute], redirects: list[RedirectSpec],
) -> str:
    ordered = sorted(routes, key=lambda r: r.path_prefix in ("/", ""))
    blocks = []
    for i, r in enumerate(ordered):
        prefix = r.path_prefix.strip("/")
        proxy_target = f"http://{r.endpoint.host}:{r.endpoint.port}/{{R:1}}"
        blocks.append(
            f'        <rule name="{frag_key}-path-{i}" stopProcessing="true">\n'
            f'          <match url="^{prefix}/(.*)" />\n'
            f'          <action type="Rewrite" url="{proxy_target}" />\n'
            f'        </rule>\n'
        )
    # redirect는 가장 넓게 매칭하는(마지막) 라우트의 경로를 기준으로 접두사를 붙인다.
    root_prefix = ordered[-1].path_prefix
    redirect_blocks = "".join(
        _rule_xml(f"{frag_key}-redirect-{i}", _prefixed(root_prefix, r.from_path).lstrip("/"), r)
        for i, r in enumerate(redirects)
    )
    return redirect_blocks + "".join(blocks)


def _regenerate_base_web_config() -> None:
    settings = get_settings()
    routes_dir = _routes_dir()
    fragments = sorted(routes_dir.glob("*.xml"))
    rule_blocks = "".join(f.read_text(encoding="utf-8") for f in fragments)
    site_dir = settings.iis_sites_root / BASE_SITE_NAME
    site_dir.mkdir(parents=True, exist_ok=True)
    config_path = site_dir / "web.config"
    existing = _read_existing_or_skeleton(config_path)
    config_path.write_text(_splice_managed_rules(existing, rule_blocks), encoding="utf-8")


def _ensure_arr_proxy_enabled() -> None:
    """URL Rewrite 규칙의 절대 URL(http://127.0.0.1:{port}/...) target은 URL Rewrite
    모듈 자체가 아니라 ARR(Application Request Routing)의 프록시 기능이 있어야 실제로
    전달된다 — 이 서버 레벨 설정이 꺼진 채로(또는 ARR 미설치로) 사이트를 만들면 규칙은
    매칭되는데 응답이 안 오는(502/무응답) 상태로 조용히 배포가 "성공"해 버리므로, 베이스
    사이트 최초 생성 시점에 명시적으로 켜고 실패하면 바로 에러로 드러낸다."""
def _ensure_arr_proxy_enabled() -> None:
    appcmd = get_settings().iis_appcmd_path
    try:
        proc = subprocess.run(
            [appcmd, "set", "config", "-section:system.webServer/proxy", "/enabled:True", "/commit:apphost"],
            capture_output=True, text=True,
        )
    except FileNotFoundError as e:
        raise IISError(
            f"[WinError 2] appcmd 실행 파일을 찾을 수 없습니다 (설정: PAAS_IIS_APPCMD_PATH={appcmd}): {e}"
        ) from e
    if proc.returncode != 0:
        raise IISError(
            "ARR(Application Request Routing) 프록시 기능을 켜지 못했습니다 — ARR이 설치돼 "
            "있는지 확인하세요(https://www.iis.net/downloads/microsoft/application-request-routing): "
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )


def _ensure_base_site() -> None:
    settings = get_settings()
    site_dir = settings.iis_sites_root / BASE_SITE_NAME
    site_dir.mkdir(parents=True, exist_ok=True)
    if not (site_dir / "web.config").exists():
        _regenerate_base_web_config()
    try:
        proc = subprocess.run(
            [settings.iis_appcmd_path, "list", "site", f"/name:{BASE_SITE_NAME}"],
            capture_output=True, text=True,
        )
    except FileNotFoundError as e:
        raise IISError(
            f"[WinError 2] appcmd 실행 파일을 찾을 수 없습니다 (설정: PAAS_IIS_APPCMD_PATH={settings.iis_appcmd_path}): {e}"
        ) from e
    if proc.returncode == 0 and proc.stdout.strip():
        return
    _ensure_arr_proxy_enabled()
    _run_appcmd(
        "add", "site",
        f"/name:{BASE_SITE_NAME}", f"/physicalPath:{site_dir}",
        f"/bindings:http/*:80:{settings.base_domain}",
    )


def _run_appcmd(*args: str) -> None:
    appcmd = get_settings().iis_appcmd_path
    try:
        proc = subprocess.run([appcmd, *args], capture_output=True, text=True)
    except FileNotFoundError as e:
        raise IISError(
            f"[WinError 2] appcmd 실행 파일을 찾을 수 없습니다 (설정: PAAS_IIS_APPCMD_PATH={appcmd}): {e}"
        ) from e
    if proc.returncode != 0:
        raise IISError(
            f"appcmd {args[0]} 실패 (IIS 미설치 시 PAAS_IIS_APPCMD_PATH 확인): "
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )


class IISProxy(ReverseProxy):
    def configure(self, project_name, profile: BuildProfile, domain, path_prefix,
                  endpoint: Endpoint, redirects: list[RedirectSpec]) -> None:
        self.configure_paths(
            project_name, profile, domain, [PathRoute(path_prefix=path_prefix, endpoint=endpoint)],
            redirects,
        )

    def configure_paths(self, project_name, profile: BuildProfile, domain,
                         routes: list[PathRoute], redirects: list[RedirectSpec]) -> None:
        if _is_shared(domain):
            frag_key = site_name(project_name, profile)
            _route_fragment_file(project_name, profile).write_text(
                _build_shared_fragment(frag_key, routes, redirects), encoding="utf-8",
            )
            _regenerate_base_web_config()
            _ensure_base_site()
            return

        settings = get_settings()
        name = site_name(project_name, profile)
        site_dir = settings.iis_sites_root / name
        site_dir.mkdir(parents=True, exist_ok=True)
        config_path = site_dir / "web.config"
        # 이 사이트는 도메인 이름 그대로라 운영자가 이미 다른 용도로 web.config를
        # 손봐 뒀을 수 있다 — 있으면 읽어서 플랫폼 규칙만 갈아끼운다(전부 새로 쓰지 않음).
        existing = _read_existing_or_skeleton(config_path)
        rule_blocks = _rule_blocks_for_paths(routes, redirects)
        config_path.write_text(_splice_managed_rules(existing, rule_blocks), encoding="utf-8")

        _ensure_arr_proxy_enabled()
        try:
            subprocess.run(
                [settings.iis_appcmd_path, "delete", "site", f"/site.name:{name}"],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            pass
        _run_appcmd(
            "add", "site",
            f"/name:{name}", f"/physicalPath:{site_dir}", f"/bindings:http/*:80:{domain}",
        )

    def remove(self, project_name, profile: BuildProfile) -> None:
        settings = get_settings()
        # 공유 모드 조각 파일 — 있으면 지우고 base web.config를 다시 합성한다.
        frag = _route_fragment_file(project_name, profile)
        if frag.exists():
            frag.unlink()
            _regenerate_base_web_config()

        # 전용 모드(커스텀 도메인) 사이트 — 없으면 조용히 넘어간다.
        name = site_name(project_name, profile)
        try:
            subprocess.run(
                [settings.iis_appcmd_path, "delete", "site", f"/site.name:{name}"],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            pass

    def configured_routes(self) -> list[tuple[str, list[str]]]:
        """web.config에 구성된 (site_name, rewrite 타겟 URL 목록) 목록.

        공유 모드는 routes/ 아래 조각 파일(파일 stem = site_name), 커스텀 도메인
        전용 사이트는 web.config를 가진 iis_sites_root 하위 디렉터리(디렉터리 이름
        = site_name)로 판별한다. rewrite 타겟은 플랫폼이 쓴 <action type="Rewrite"
        url="..."/>에서 뽑는다 — 서버구성이 연결 여부와 미등록 항목(이름·rewrite 주소)을
        표시하는 근거가 된다."""
        root = get_settings().iis_sites_root
        result: list[tuple[str, list[str]]] = []
        routes_dir = root / BASE_SITE_NAME / "routes"
        if routes_dir.is_dir():
            for f in sorted(routes_dir.glob("*.xml")):
                result.append((f.stem, _rewrite_targets(f.read_text(encoding="utf-8"))))
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if d.name == BASE_SITE_NAME or not d.is_dir():
                    continue
                cfg = d / "web.config"
                if cfg.exists():
                    result.append((d.name, _rewrite_targets(cfg.read_text(encoding="utf-8"))))
        return result
