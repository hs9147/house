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
from .base import DEV_SEGMENT, PathRoute, RedirectSpec, ReverseProxy
from .caddy_proxy import CaddyProxy
from .iis_proxy import IISProxy

__all__ = [
    "RedirectSpec", "ReverseProxy", "PathRoute", "get_proxy", "domain_for", "path_prefix_for",
    "DEV_SEGMENT",
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
    """모든 배포 URL은 /apps/{조직 또는 "_"}/{프로젝트}/[dev/] 서브패스(Sub Path) 구조로 통일한다."""
    settings = get_settings()
    if settings.tier == "enterprise":
        return "/"
    org_segment = org_name or "_"
    dev_segment = f"{DEV_SEGMENT}/" if profile == BuildProfile.development else ""
    return f"/apps/{org_segment}/{project_name}/{dev_segment}"


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
