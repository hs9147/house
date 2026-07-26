"""Runtime 인터페이스.

1차(small)  → DockerRuntime : Docker Engine에 컨테이너로 실행, Caddy가 도메인 라우팅
2차(enterprise) → K8sRuntime : Deployment/Service/Ingress 매니페스트 생성 후 apply

배포 단위는 항상 "이미지 태그"이므로 두 런타임은 동일한 RuntimeSpec을 받는다.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ...models import BuildProfile


@dataclass
class RuntimeSpec:
    project_name: str
    image_tag: str
    internal_port: int
    profile: BuildProfile
    domain: str
    env: dict[str, str] = field(default_factory=dict)
    # env 중 프로젝트 EnvVar(is_secret=True)에서 온 키 — K8s 런타임이 이 값을
    # 일반 매니페스트(특히 GitOps로 외부 git에 커밋되는 파일)에 평문으로 넣지 않고
    # 별도 Secret으로 분리하는 기준이 된다.
    secret_keys: frozenset[str] = field(default_factory=frozenset)
    memory_limit: str = "1g"
    cpu_limit: float = 1.0
    replicas: int = 1
    gpu: bool = False
    health_check_path: str = "/"
    # composite 프로젝트에서만 사용 — "backend"/"frontend". 일반 프로젝트는 None.
    component: str | None = None

    @property
    def unit_name(self) -> str:
        """컨테이너/K8s 리소스 이름. 프로필별로 분리해 dev·release 동시 기동을 허용한다."""
        suffix = "-dev" if self.profile == BuildProfile.development else ""
        component = f"-{self.component}" if self.component else ""
        return f"paas-{self.project_name}{component}{suffix}"


@dataclass
class Endpoint:
    """프록시가 바라볼 업스트림. 1차는 호스트 포트, 2차는 클러스터 내 Service."""

    host: str
    port: int


class Runtime(ABC):
    @abstractmethod
    def start(self, spec: RuntimeSpec) -> Endpoint:
        """새 버전을 기동하고 트래픽을 받을 endpoint를 반환한다. 무중단 교체는 구현체 책임."""

    @abstractmethod
    def stop(self, project_name: str, profile: BuildProfile) -> None: ...

    @abstractmethod
    def status(self, project_name: str, profile: BuildProfile) -> str: ...

    @abstractmethod
    def logs(self, project_name: str, profile: BuildProfile, tail: int = 200) -> str: ...
