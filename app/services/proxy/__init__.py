"""1차(small) 전용 도메인 라우팅.

기본 백엔드는 Caddy이고, PAAS_PROXY_BACKEND로 iis/apache를 선택할 수 있다
(운영환경에 맞춰 서버구성을 바꾸는 요건 — docs 10.2절/14절 참고).

호출측(services/deployer.py, services/preview.py)은 백엔드를 몰라도 되도록
모듈 함수(domain_for/configure/remove)만 쓴다 — get_proxy()가 설정에 따라
CaddyProxy/IISProxy/ApacheProxy 중 하나로 위임한다.
(2차/k8s에서는 Ingress + cert-manager가 이 역할을 하므로 이 패키지를 쓰지 않는다)
"""
from ...config import get_settings
from ...models import BuildProfile, RedirectRule
from ..runtime.base import Endpoint
from .apache_proxy import ApacheProxy
from .base import DEV_SUFFIX, PathRoute, RedirectSpec, ReverseProxy
from .caddy_proxy import CaddyProxy
from .iis_proxy import IISProxy

__all__ = [
    "RedirectSpec", "ReverseProxy", "PathRoute", "get_proxy", "domain_for", "path_prefix_for",
    "DEV_SUFFIX",
    "configure", "configure_paths", "remove",
]


def get_proxy() -> ReverseProxy:
    backend = get_settings().proxy_backend
    if backend == "iis":
        return IISProxy()
    if backend == "apache":
        return ApacheProxy()
    return CaddyProxy()


def domain_for(project_name: str, profile: BuildProfile) -> str:
    """모든 배포 도메인은 base_domain 서브패스(Sub Path) 기반으로 통일한다.

    **프로젝트별 커스텀 도메인은 없다.** 예전에는 project.domain을 받아 release에만
    썼는데, small tier에서는 이 함수가 그 값을 보지도 않았고(항상 base_domain) 세 프록시
    백엔드의 "전용 사이트" 분기는 그래서 한 번도 실행되지 않았다 — 입력만 받고 버리는
    필드였다. 도메인을 나누려면 프록시 바인딩과 인증서를 함께 다뤄야 하는데 그것은
    이 플랫폼이 하는 일이 아니다(서브패스로 통일한 이유가 그것이다).
    """
    settings = get_settings()
    if settings.tier == "enterprise":
        if profile == BuildProfile.development:
            return f"{project_name}-dev.{settings.base_domain}"
        return f"{project_name}.{settings.base_domain}"
    return settings.base_domain


def path_prefix_for(
    org_name: str | None, project_name: str, profile: BuildProfile,
) -> str:
    """모든 배포 URL은 {접두사}/{조직 또는 "_"}/{프로젝트}/ 서브패스로 통일한다.

    **접두사는 프로필마다**다(PAAS_RELEASE_PATH_PREFIX 기본 /apps, PAAS_DEV_PATH_PREFIX 기본 /dev) — 운영본과
    개발본의 주소가 첫 조각부터 갈려 한눈에 구분되고, 두 경로가 서로소라 규칙 순서가 무관하다. 둘을 같게 두면
    development는 같은 접두사 아래 형제(…/{프로젝트}~dev/)가 된다(아래 사정).

    **dev는 release의 형제다**(자식이 아니다). 예전에는 /apps/{조직}/{프로젝트}/dev/ 로
    release 안에 넣었는데, 그러면 release 규칙이 dev 요청까지 매칭해서 어느 규칙이 먼저
    놓이느냐가 라우팅을 정했다 — 그 순서를 정하는 것은 프록시 설정 조각의 파일 정렬이라는
    눈에 안 보이는 성질이었다(그리고 실제로 뒤집혀 있었다). 형제로 떼어 놓으면 두 경로가
    서로소라 순서가 무관해지고, release 앱이 자기 /dev/ 경로를 되찾는다.
    """
    settings = get_settings()
    if settings.tier == "enterprise":
        return "/"
    org_segment = org_name or "_"
    release, dev = path_bases()
    if profile == BuildProfile.development:
        # 접두사가 같으면 release와 겹치지 않게 형제 이름(~dev)으로 — 다르면 접두사가 이미 가른다
        return f"{dev}/{org_segment}/{project_name}{DEV_SUFFIX if dev == release else ''}/"
    return f"{release}/{org_segment}/{project_name}/"


def path_bases() -> tuple[str, str]:
    """(release 접두사, development 접두사) — 앞에 '/'를 두고 끝의 '/'는 뗀다(루트면 "")."""
    settings = get_settings()

    def norm(value: str, fallback: str) -> str:
        value = (value if value is not None else fallback).strip().strip("/")
        return f"/{value}" if value else ""

    return norm(settings.release_path_prefix, "/apps"), norm(settings.dev_path_prefix, "/dev")


def configure(
    project_name: str, profile: BuildProfile, domain: str, path_prefix: str, endpoint: Endpoint,
    redirects: list[RedirectRule] | list[RedirectSpec] | None = None,
    strip_prefix: bool = True,
) -> None:
    specs = [
        r if isinstance(r, RedirectSpec) else RedirectSpec.from_rule(r)
        for r in (redirects or [])
    ]
    get_proxy().configure_paths(
        project_name, profile, domain,
        [PathRoute(path_prefix=path_prefix, endpoint=endpoint, strip_prefix=strip_prefix)],
        specs,
    )


def configure_paths(
    project_name: str, profile: BuildProfile, domain: str, routes: list[PathRoute],
    redirects: list[RedirectRule] | list[RedirectSpec] | None = None,
) -> None:
    specs = [
        r if isinstance(r, RedirectSpec) else RedirectSpec.from_rule(r)
        for r in (redirects or [])
    ]
    get_proxy().configure_paths(project_name, profile, domain, routes, specs)


def remove(project_name: str, profile: BuildProfile) -> None:
    get_proxy().remove(project_name, profile)
