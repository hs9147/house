"""리버스프록시 인터페이스 — 1차(small)에서 도메인 라우팅을 맡는 백엔드를 추상화한다.

caddy(기본)/iis/apache 세 구현이 있다(PAAS_PROXY_BACKEND). 2차(enterprise)는 K8s
Ingress + cert-manager가 이 역할을 대신하므로 이 패키지를 쓰지 않는다.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ...models import BuildProfile, RedirectRule
from ..runtime.base import Endpoint


@dataclass
class RedirectSpec:
    """RedirectRule을 프록시 설정 생성에 필요한 최소 필드로 옮긴 것(DB 세션 비의존)."""

    from_path: str
    to_path: str
    kind: str  # "redirect" | "rewrite"
    status_code: int = 302  # kind="redirect"일 때만 의미

    @classmethod
    def from_rule(cls, rule: RedirectRule) -> "RedirectSpec":
        return cls(
            from_path=rule.from_path, to_path=rule.to_path,
            kind=rule.kind.value, status_code=rule.status_code,
        )


def site_name(project_name: str, profile: BuildProfile) -> str:
    suffix = "-dev" if profile == BuildProfile.development else ""
    return f"{project_name}{suffix}"


@dataclass
class PathRoute:
    """한 도메인 안에서 경로 접두사로 서로 다른 업스트림에 나눠 라우팅하는 규칙
    (composite 프로젝트의 backend/frontend 분리 전용). 매칭된 접두사는 백엔드로
    전달되기 전에 제거된다(예: "/api/" 라우트는 "/api/users" → 업스트림 "/users") —
    세 프록시 백엔드가 동일한 규칙을 따른다."""

    path_prefix: str  # 예: "/api/", "/"
    endpoint: Endpoint


class ReverseProxy(ABC):
    @abstractmethod
    def configure(
        self, project_name: str, profile: BuildProfile, domain: str, path_prefix: str,
        endpoint: Endpoint, redirects: list[RedirectSpec],
    ) -> None:
        """domain 아래 path_prefix 경로 → endpoint 라우팅 + redirect/rewrite 규칙을
        반영하고 무중단 reload한다. path_prefix가 "/"(또는 "")면 도메인 전체가 이
        프로젝트 것(커스텀 도메인 등) — 그 외에는 domain을 여러 프로젝트가 공유하는
        전제로 서브패스 라우팅을 구성한다(services/proxy/__init__.py의 path_prefix_for)."""

    @abstractmethod
    def configure_paths(
        self, project_name: str, profile: BuildProfile, domain: str,
        routes: list[PathRoute], redirects: list[RedirectSpec],
    ) -> None:
        """한 도메인을 경로 접두사별로 여러 업스트림에 나눠 라우팅한다(composite 전용)."""

    @abstractmethod
    def remove(self, project_name: str, profile: BuildProfile) -> None:
        """사이트 설정을 제거한다."""

    def configured_routes(self) -> list[tuple[str, list[str]]] | None:
        """프록시 설정 파일에 구성된 (사이트 이름 site_name, rewrite 타겟 URL 목록) 목록.

        설정 파일을 소스로 실제 라우팅을 읽을 수 있는 백엔드(IIS의 web.config)만
        구현한다. 그 외 백엔드는 None을 반환해 "추적하지 않음"을 알린다 —
        호출측(서버구성)은 None이면 프록시 설정 기반 표시(연결 여부·미등록 항목)를
        생략하고 기존처럼 런타임 상태로만 판단한다."""
        return None
