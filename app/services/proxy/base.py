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


# development 배포를 가리키는 접미사. **경로와 사이트 이름이 같은 것을 쓴다.**
#
#   경로       /apps/{조직}/{프로젝트}~dev/
#   사이트 이름  {프로젝트}~dev   (조각 파일 이름·규칙 이름)
#
# 문자는 프로젝트 이름 규칙(^[a-z0-9][a-z0-9-]{1,40}$)에 **없는** 것이어야 한다. 그래야
# 두 가지가 동시에 성립한다:
#
#   1. site_name이 단사다. "-dev"이던 때는 프로젝트 shop의 dev와 프로젝트 shop-dev의
#      release가 같은 이름이 되어 조각 파일 하나를 공유했다 — 뒤에 배포한 쪽이 앞엣것의
#      라우트를 덮어쓰고 remove()가 남의 것을 지웠다.
#   2. release 경로와 dev 경로가 **서로소**다. 예전에는 dev가 release 안에 있어서
#      (/apps/_/shop/ ⊃ /apps/_/shop/dev/) 어느 규칙이 먼저 놓이느냐가 라우팅을 정했고,
#      그 순서는 조각 파일 정렬이라는 눈에 안 보이는 성질이 정했다. 형제로 떼어 놓으면
#      순서가 아예 무관해진다 — 세 백엔드 모두에서.
#
# 그리고 이제 release 앱이 **자기 /dev/ 경로를 되찾는다**. 예전에는 플랫폼이 그 자리를
# 가져가서 /apps/_/shop/dev/... 가 앱에 닿지 못했다.
#
# '~'인 이유: URL 경로에서 이스케이프가 필요 없는 문자(RFC 3986 unreserved)이고 정규식
# 메타문자가 아니다 — 이 값은 IIS match 패턴에 그대로 들어간다('+'였다면 깨진다).
DEV_SUFFIX = "~dev"


def site_name(project_name: str, profile: BuildProfile) -> str:
    return f"{project_name}{DEV_SUFFIX}" if profile == BuildProfile.development else project_name


@dataclass
class PathRoute:
    """한 도메인 안에서 경로 접두사로 서로 다른 업스트림에 나눠 라우팅하는 규칙
    (composite 프로젝트의 backend/frontend 분리 전용). 매칭된 접두사는 백엔드로
    전달되기 전에 제거된다(예: "/api/" 라우트는 "/api/users" → 업스트림 "/users") —
    세 프록시 백엔드가 동일한 규칙을 따른다."""

    path_prefix: str  # 예: "/api/", "/"
    endpoint: Endpoint
    # False면 접두사를 벗기지 않고 그대로 전달한다. Vite dev 서버처럼 업스트림이 자기
    # 공개 경로(base)를 알고 그 접두사가 붙은 요청만 받는 경우에 쓴다 — 빌드본은 반대로
    # 벗겨서 넘겨야 한다(HTML에 이미 전체 경로가 박혀 있고 서버는 루트에서 서빙한다).
    strip_prefix: bool = True


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
