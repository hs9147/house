"""플랫폼 전역 설정.

PAAS_ 접두사의 환경변수로 재정의한다. 예:
  PAAS_TIER=enterprise PAAS_BASE_DOMAIN=apps.example.com uvicorn app.main:app
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Tier = Literal["small", "enterprise"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAAS_", env_file=".env", extra="ignore")

    # PaaS 명칭 (환경변수: PAAS_PLATFORM_NAME)
    platform_name: str = "chofam cloud platform"

    # 허용할 계정 이메일 도메인 (환경변수: PAAS_ALLOWED_EMAIL_DOMAIN, 기본값: cho-fam.com)
    allowed_email_domain: str = "cho-fam.com"

    # 1차(small): Docker 단일/소수 서버, 2차(enterprise): Kubernetes 클러스터
    tier: Tier = "small"

    # --- 설치 빌드옵션 ---
    # 기능 모듈 선택 (core는 항상 켜짐). 예: "deploy" 만 켜면 배포 전용 서버.
    features: str = "deploy,workspace"
    # 운영환경 OS. auto면 platform.system()으로 감지. 컨테이너 등 감지가 틀릴 때 명시.
    host_os: Literal["auto", "linux", "macos", "windows"] = "auto"
    # 기능 매트릭스가 GPU 불가로 판단해도 강제 허용 (예: 커스텀 GPU 런타임)
    force_gpu: bool = False

    # --- OIDC/RBAC (Keycloak 호환, 선택 — API 키 체계와 병행) ---
    oidc_issuer: str = ""  # 예: https://sso.example.com/realms/company
    oidc_audience: str = ""  # 비우면 audience 검증 생략
    oidc_jwks_url: str = ""  # 비우면 {issuer}/protocol/openid-connect/certs (Keycloak 규약)
    oidc_admin_role: str = "paas-admin"  # realm_access.roles에 이 롤이 있으면 admin

    # --- 배포 작업 큐 ---
    deploy_workers: int = 2

    # --- OpenBao 시크릿 (선택 — 설정 시 Fernet 키를 KV v2에서 로드) ---
    openbao_url: str = ""  # 예: https://bao.example.com
    openbao_token: str = ""
    openbao_key_path: str = "secret/data/paas/fernet"  # data.data.key 에 Fernet 키 저장

    database_url: str = "sqlite:///./paas.db"
    base_domain: str = "deploy.localhost"

    # 관리자 부트스트랩 API 키. 미설정 시 기동 로그에 일회성 키를 출력한다.
    admin_api_key: str = ""
    # EnvVar 암호화용 Fernet 키(urlsafe base64 32byte). 미설정 시 개발용 키를 생성한다.
    fernet_key: str = ""
    # 키 회전용 구(舊) 키 목록(콤마 구분) — 복호화에만 사용, 암호화는 fernet_key로.
    # 회전 절차: 새 키 발급 → fernet_key 교체 + 기존 키를 여기로 이동 →
    # POST /admin/rotate-secrets 로 전체 재암호화 → 구 키 제거.
    fernet_keys_old: str = ""

    # Git 작업 디렉토리 / 빌드 로그 저장소
    work_dir: Path = Path("./data/workspaces")
    build_log_dir: Path = Path("./data/build-logs")

    # --- 1차(small) 전용 ---
    # 실행 런타임: docker(기본, 컨테이너 이미지) | windows_service(Docker 없이 nssm으로
    # 네이티브 프로세스를 Windows Service로 등록 — IIS 뒤에 배치하는 구성 등)
    runtime_backend: Literal["docker", "windows_service"] = "docker"
    # 리버스프록시: caddy(기본) | iis | apache — 운영환경에 맞춰 선택
    proxy_backend: Literal["caddy", "iis", "apache"] = "caddy"
    caddy_sites_dir: Path = Path("./data/caddy-sites")
    caddy_admin_url: str = "http://127.0.0.1:2019"
    port_range_start: int = 8100
    port_range_end: int = 8999

    # --- windows_service 런타임 전용 ---
    # nssm(Non-Sucking Service Manager, public domain) 실행 파일 경로. 리포 루트의
    # start.cmd(배포 시 자동 생성 — PORT 환경변수로 리슨 포트 전달)를 서비스로 등록해 기동한다.
    nssm_path: str = "nssm"

    # --- iis 프록시 전용 ---
    # 사이트별 web.config를 생성해 둘 물리 경로 루트. IIS 사이트의 physicalPath로 쓰인다.
    iis_sites_root: Path = Path("./data/iis-sites")
    # IIS 사이트 등록/삭제에 쓰는 appcmd.exe 경로(Windows 전용)
    iis_appcmd_path: str = r"C:\Windows\System32\inetsrv\appcmd.exe"

    # --- apache 프록시 전용 ---
    # VirtualHost 설정 파일을 생성해 둘 디렉토리 (예: /etc/apache2/sites-enabled)
    apache_sites_dir: Path = Path("./data/apache-sites")
    # 설정 반영 후 실행할 reload 명령 (공백으로 분리해 실행)
    apache_reload_cmd: str = "apachectl graceful"

    # --- 2차(enterprise) 전용 ---
    k8s_namespace: str = "paas-apps"
    k8s_registry: str = ""  # 예: harbor.example.com/paas — 빈 값이면 로컬 이미지명 사용
    k8s_ingress_class: str = "traefik"
    k8s_cluster_issuer: str = "letsencrypt"  # cert-manager ClusterIssuer
    # 멀티테넌시 격리(갭6): 유닛별 NetworkPolicy 생성 — ingress 컨트롤러·동일 네임스페이스만 허용
    k8s_isolation: bool = False
    k8s_ingress_namespace: str = "traefik"  # ingress 컨트롤러가 사는 네임스페이스
    # GitOps(ArgoCD) 연계: 설정 시 직접 apply 대신 매니페스트를 이 리포에 커밋·푸시
    k8s_gitops_repo: str = ""  # 예: git@git.example.com:org/paas-apps.git
    k8s_gitops_branch: str = "main"
    k8s_gitops_path: str = "apps"  # 리포 내 매니페스트 디렉토리
    # 네임스페이스 ResourceQuota (빈 값이면 미생성)
    k8s_quota_cpu: str = ""  # 예: "20"
    k8s_quota_memory: str = ""  # 예: "64Gi"
    # kubernetes 패키지가 없거나 apply 권한이 없을 때 매니페스트를 내려쓸 위치
    k8s_manifest_dir: Path = Path("./data/k8s-manifests")

    # 웹훅 서명 검증용 공유 시크릿 (GitHub/Gitea 웹훅 설정에 동일 값 입력)
    webhook_secret: str = ""

    # 사내 Git 서버(Gitea 등) 기본 URL — 콘솔에 "Git" 메뉴를 노출하는 용도로만 쓰인다
    # (배포 동작에는 영향 없음, git_url은 프로젝트별로 여전히 개별 지정). infra/gitea/ 참고.
    gitea_url: str = ""
    # 조직/리포 자동 생성용 Gitea API 토큰 (Site Administration → Applications에서 발급,
    # 조직 생성 권한 필요). 설정 없으면 /orgs API는 503으로 명확히 실패한다.
    gitea_api_token: str = ""
    # 기업용 거버넌스: true면 프로젝트 등록 시 git_url 호스트가 gitea_url과 일치해야
    # 한다(github.com 등 외부 호스트 등록을 422로 거부). "소스가 사외로 나가지 않는다"는
    # 보장을 internal LLM 강제(schemas.py)와 동일한 원칙으로 git 저장소에도 적용한다.
    # 기본값 true — 사내 Gitea 미설정 시(PAAS_GITEA_URL 없음) 등록 자체를 503으로 막는
    # 안전한 실패가 기본. 외부 git 호스트를 허용하려면 명시적으로 false로 설정할 것.
    git_internal_only: bool = True

    # release 빌드 기본 리소스 (development는 build.py의 프로필 정의가 절반 수준으로 축소)
    default_memory_limit: str = "1g"
    default_cpu_limit: float = 1.0

    # --- zip/폴더 업로드로 프로젝트 등록 (services/upload.py) ---
    # 업로드 원본(zip 파일) 자체의 스트리밍 크기 상한
    upload_max_zip_mb: int = 200
    # 압축 해제 시 총 바이트 상한 (zip 헤더 선언값이 아닌 실제 해제 바이트로 강제) —
    # 폴더 업로드(다중 파일)의 총 용량 상한으로도 동일하게 쓰인다.
    upload_max_uncompressed_mb: int = 500
    # zip 엔트리 수 / 폴더 업로드 파일 수 상한 (엔트리 폭탄 방지)
    upload_max_files: int = 5000
    # 파일별 (압축해제크기/압축크기) 상한 — 초과 시 zip bomb 의심으로 즉시 거부
    upload_max_compression_ratio: int = 100
    # 웹훅 자동 등록 시 플랫폼 자신을 가리키는 공개 URL (예: https://paas.example.com)
    # 비우면 웹훅 자동 등록을 건너뛰고 infra/gitea/README.md의 수동 절차를 안내한다.
    platform_public_url: str = ""

    # --- 외부 API 디렉터리 검색 (services/apisearch.py) ---
    # 키워드로 공개 API를 검색해 external_api 모듈로 추가할 때 조회하는 머신리더블
    # OpenAPI 디렉터리. 기본은 apis.guru 공개 목록. 폐쇄망이라면 사내 미러 URL로 교체.
    api_directory_url: str = "https://api.apis.guru/v2/list.json"

    # --- 콘솔 자기 배포 (옵트인, services/self_deploy.py) ---
    # true면 백엔드 기동 시 platform/console/을 일반 react Project(source_subdir 사용)로
    # 등록하고 기존 배포 파이프라인(build_image → DockerRuntime → 리버스프록시)으로 최초
    # 1회 배포한다. 기본 꺼짐 — 꺼져 있으면 지금처럼 콘솔은 /console에 정적 마운트된다.
    # Docker 데몬 접근과 최초 배포 시간(그동안 콘솔 접근 불가)이 필요해 기본값을 false로 둔다.
    self_deploy_console: bool = False
    self_deploy_console_git_url: str = ""  # 이 플랫폼 자신의 git 리포 URL
    self_deploy_console_branch: str = "main"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (
        s.work_dir, s.build_log_dir, s.caddy_sites_dir, s.k8s_manifest_dir,
        s.iis_sites_root, s.apache_sites_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
    return s
