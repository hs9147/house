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
from .base import PathRoute, RedirectSpec, ReverseProxy
from .caddy_proxy import CaddyProxy
from .iis_proxy import IISProxy

__all__ = [
    "RedirectSpec", "ReverseProxy", "PathRoute", "get_proxy", "domain_for", "path_prefix_for",
    "configure", "configure_paths", "remove",
]


def get_proxy() -> ReverseProxy:
    backend = get_settings().proxy_backend
    if backend == "iis":
        return IISProxy()
    if backend == "apache":
        return ApacheProxy()
    return CaddyProxy()


def domain_for(project_name: str, custom_domain: str | None, profile: BuildProfile) -> str:
    """1차(small)의 배포 URL은 서브패스 기반이다 — 모든 프로젝트가 base_domain
    하나를 공유하고 /apps/{조직}/{프로젝트}/ 경로로 구분된다(path_prefix_for). 예외는
    release 배포에 커스텀 도메인(project.domain)을 지정한 경우 — 그 도메인을
    그대로 쓴다(development는 항상 공유 base_domain).

    2차(enterprise/K8s)는 이 서브패스 라우팅 대상이 아니다 — Ingress 클래스마다
    경로 스트리핑 방식이 달라(nginx rewrite-target, traefik Middleware 등) 일반화가
    어렵고, 지금까지도 프로젝트당 서브도메인 1개로 잘 동작해왔다. 기존 방식 그대로
    유지한다(k8s_runtime.py의 Ingress host/path는 이 값을 그대로 쓴다)."""
    settings = get_settings()
    if settings.tier == "enterprise":
        if profile == BuildProfile.development:
            return f"{project_name}-dev.{settings.base_domain}"
        return custom_domain or f"{project_name}.{settings.base_domain}"
    if profile == BuildProfile.release and custom_domain:
        return custom_domain
    return settings.base_domain


def path_prefix_for(
    org_name: str | None, project_name: str, custom_domain: str | None, profile: BuildProfile,
) -> str:
    """domain_for와 짝 — 2차(enterprise)와 release+커스텀 도메인 예외는 "/"(도메인
    전체가 이 프로젝트 것), 그 외에는 /apps/{조직 또는 "_"}/{프로젝트}/[dev/] 서브패스.
    "apps" 세그먼트는 배포된 프로젝트 트래픽을 base_domain의 다른 용도(향후 추가될
    최상위 경로 등)와 네임스페이스로 분리하기 위한 고정 접두사다."""
    settings = get_settings()
    if settings.tier == "enterprise":
        return "/"
    if profile == BuildProfile.release and custom_domain:
        return "/"
    org_segment = org_name or "_"
    dev_segment = "dev/" if profile == BuildProfile.development else ""
    return f"/apps/{org_segment}/{project_name}/{dev_segment}"


def configure(
    project_name: str, profile: BuildProfile, domain: str, path_prefix: str, endpoint: Endpoint,
    redirects: list[RedirectRule] | list[RedirectSpec] | None = None,
) -> None:
    specs = [
        r if isinstance(r, RedirectSpec) else RedirectSpec.from_rule(r)
        for r in (redirects or [])
    ]
    get_proxy().configure(project_name, profile, domain, path_prefix, endpoint, specs)


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
