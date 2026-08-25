"""플랫폼 전역 설정.

PAAS_ 접두사의 환경변수로 재정의한다. 예:
  PAAS_TIER=enterprise PAAS_BASE_DOMAIN=apps.example.com uvicorn app.main:app
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

Tier = Literal["small", "enterprise"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAAS_", env_file=".env", extra="ignore")

    # 플랫폼 명칭 — 콘솔 헤더·로그인 화면·API 문서 제목에 그대로 노출된다.
    # (환경변수: PAAS_PLATFORM_NAME. URL prefix(/paas)와 환경변수 접두사(PAAS_)는
    #  배포된 리버스프록시 설정·등록된 웹훅 주소가 걸려 있어 명칭과 별개로 유지한다.)
    platform_name: str = "house"

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

    # --- OIDC Provider (paas 자신이 발급자가 되는 경량 SSO — Keycloak 없이 직접 구현) ---
    # true면 paas가 /paas/.well-known/openid-configuration 등 OIDC Provider 엔드포인트를
    # 노출한다(services/oidc_provider.py). Gitea 등 외부 서비스가 이 엔드포인트로 SSO를
    # 걸면 paas의 UserAccount 계정 그 자체가 로그인 ID가 된다 — 별도 IdP(Keycloak 등)
    # 없이도 SSO가 성립한다. 켤 때는 oidc_issuer도 이 플랫폼 자신의 주소로 맞출 것(예:
    # https://paas.example.com/paas) — 그러면 위 authenticate_bearer가 우리 토큰도
    # 받는다. 이때 JWKS는 HTTP로 가져오지 않고 로컬 개인키로 바로 검증하므로
    # oidc_jwks_url을 따로 설정할 필요가 없다(security._verification_key 참고).
    oidc_provider_enabled: bool = False
    # RSA 서명 키(PEM) 경로 — 없으면 최초 기동 시 새로 만들어 저장한다. 재시작 후에도
    # 같은 키를 써야 이미 나눠준 JWKS 공개키를 신뢰하는 클라이언트(Gitea 등)가 계속
    # 동작한다 — 지우거나 옮기면 그 순간부터 기존 토큰 검증이 깨진다.
    oidc_provider_signing_key_path: Path = Path("./data/oidc-signing-key.pem")
    # 등록된 클라이언트(JSON 객체): {"gitea": {"secret": "...", "redirect_uris": ["https://.../callback"]}}
    # Keycloak처럼 클라이언트를 UI로 동적 등록하는 기능은 없다 — 이 값에 직접 추가한다.
    oidc_provider_clients: str = "{}"
    # 미로그인 사용자를 이 URL로 보낸다(콘솔 로그인 화면). 비우면 platform_public_url
    # 기준으로 자동 계산(/console/#/login).
    oidc_provider_login_url: str = ""
    # 백채널(서버→서버) 전용 주소. Gitea 같은 클라이언트가 discovery·token·jwks를
    # 부를 때 쓰는 주소로, 브라우저가 가는 주소와 달라도 된다.
    #
    # 공개 도메인의 binding이 이 플랫폼을 가리키지 않아 https://공개도메인/paas 로는
    # 들어올 수 없는 구성에서 쓴다 — 클라이언트가 사내에서 직접 닿는 주소(예:
    # http://10.0.0.5:7000/paas)를 넣으면 TLS 자체를 안 타므로 인증서 불일치가 사라진다.
    # 이 값이 issuer가 되고(클라이언트는 discovery URL과 issuer가 같은지만 확인한다),
    # 브라우저가 가는 authorization_endpoint·로그인 화면은 계속 platform_public_url을
    # 쓴다 — 이 둘이 달라도 되는 건 OIDC 규약이 보장한다.
    # 비우면(기본) 백채널도 브라우저와 같은 주소를 쓴다.
    oidc_provider_backchannel_url: str = ""

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
    # 빌드 단계(docker build, npm/pip install 등) subprocess 하나에 허용하는 최대 시간.
    # 없으면 응답 없는 명령(네트워크 문제로 멈춘 npm install 등)이 배포를 "진행중"에
    # 영원히 묶어 둔다 — 초과 시 그 단계를 실패로 끝낸다.
    build_timeout_seconds: int = 600
    # 내부 저장소 — 플랫폼이 쓰기까지 하는 유일한 저장소 경로. 콘솔 파일 관리 화면과
    # /storage 창구, /mcp/storage/internal이 여기를 본다. (services/storage.py)
    storage_root: str = "./data/storage"
    # 사내 문서 폴더(읽기 전용). 쉼표로 여러 개 지정하고, 각 항목은 `이름=경로` 또는
    # 경로만 쓴다. 이름은 소문자·숫자·하이픈만 쓴다(URL 조각이자 색인 파일 이름이고,
    # 모듈로 가져올 때 모듈 이름이 된다). 경로만 주면 마지막 폴더 이름에서 만들어 보고
    # ("Company Docs" → company-docs), 만들 수 없으면(한글 폴더 등) 직접 쓰라고 알려 준다:
    #   PAAS_DOC_ROOTS=rules=D:\공유\사내규정,D:\shared\contracts
    # 플랫폼이 만든 저장소가 아니라 **이미 있는 공유 폴더**를 붙이는 자리라 쓰기는 열지
    # 않는다 — 읽고 찾기만 한다(/mcp/docs로 본문 검색, /mcp/storage/{이름}으로 열람).
    doc_roots: str = ""
    powershell_start_dir: str = ""
    # /exec가 쓰는 상주 PowerShell 세션의 로컬 TCP 브로커 포트. paas 프로세스가 재시작돼도
    # 이 고정 포트로 다시 붙어 같은 세션(cd·변수 등 상태)을 잇는다 — services/ps_broker.py.
    ps_broker_port: int = 47231
    # 콘솔 터미널(WebSocket PTY)이 쓰는 pywinpty 백엔드: conpty | winpty | (빈 값=자동).
    # ConPTY는 Windows 10 1809 / Server 2019부터다 — **Server 2016이면 winpty**로 못
    # 박아야 할 수 있다. 자동 선택으로 먼저 열어 보고, 안 되면 여기서 지정한다.
    pty_backend: str = ""
    # 터미널이 띄울 셸. 다른 셸(pwsh.exe, cmd.exe)로 바꿀 수 있다.
    pty_shell: str = "powershell.exe"

    # 플랫폼(paas) 리포지토리 루트. 비우면 소스 트리에서 자동 계산한다.
    # SW 업데이트의 git pull 위치로 쓰인다. (환경변수: PAAS_REPO_ROOT)
    repo_root: str = ""

    # --- SW 업데이트(git pull + Windows 서비스 재시작) ---
    # git pull 후 재시작할 Windows 서비스명(콤마 구분). 백엔드(paas)와 콘솔(console) 서비스명은
    # 설치 환경마다 다르므로 여기서 지정한다 — 예: "paas,paas-console".
    sw_update_services: str = "paas,paas-console"

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

    # --- 문서 텍스트 추출 (services/doctext.py) ---
    # 97-2003 바이너리 오피스 파일(.doc/.xls/.ppt)은 순수 파이썬으로 뽑을 수 없어
    # LibreOffice에 맡긴다. 윈도우에서는 PATH에 없으므로 실행 파일 경로를 직접 지정한다
    # (예: C:\Program Files\LibreOffice\program\soffice.exe). 비우면 PATH의 soffice를
    # 찾고, 그것도 없으면 해당 형식만 "추출 불가"로 표시된다(docx류·pdf는 무관).
    soffice_path: str = ""

    # 사내 문서 검색(services/docsearch.py)이 추출 텍스트를 캐시하는 자리. 파생 데이터라
    # 언제든 지우고 다시 만들 수 있어서 플랫폼 DB가 아니라 저장소별 sqlite 파일로 둔다.
    # 같은 자리 아래 `.ready/{저장소}/…`에 LLM이 읽을 마크다운도 남는다
    # (services/docready.py) — 열어 보면 모델이 실제로 보는 것을 그대로 확인할 수 있다.
    doc_index_dir: Path = Path("./data/doc-index")

    # 사내 도메인 접미사(쉼표 구분). 사설 IP·localhost·단일 라벨 이름은 자동으로 사내로
    # 보지만, 공개 도메인처럼 보이는 사내 주소(예: corp.example.com)는 여기 적어야
    # 아웃바운드 검증(services/egress.py)이 "망을 벗어나지 않는다"고 판정한다.
    internal_domains: str = ""

    # --- 사내 MCP 서버 (api/mcp_servers.py) ---
    # 사내 MCP 서버 주소의 기준. 플랫폼이 **자기 자신에게** 닿는 주소여야 한다
    # (예: http://localhost:7000/paas). 공개 도메인이 이 플랫폼으로 라우팅되지 않는
    # 서브패스 구성이 있어 브라우저 주소를 그대로 쓸 수 없다. 비우면 백채널 주소
    # → 공개 주소 순으로 떨어지고, 둘 다 없으면 목록에 주소가 비어 나가 등록이 막힌다.
    mcp_internal_base_url: str = ""

    # DB 조회 MCP 서버(/mcp/db/{모듈})를 열어 줄 database 모듈 이름 목록(쉼표 구분).
    # 기본값은 빈 목록 = 전부 차단이다 — 어떤 DB를 LLM에게 읽히는지는 모듈 등록만으로
    # 정해질 일이 아니라 명시적으로 고를 일이다. 목록에 있어도 SELECT 한 문장만
    # 실행되고 행 수는 서버가 자른다.
    mcp_db_modules: str = ""

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

    @property
    def resolved_repo_root(self) -> Path:
        """플랫폼 리포 루트. PAAS_REPO_ROOT가 있으면 그 값을, 없으면 소스 트리 기준으로 계산한다.

        이 파일은 app/config.py이므로 parent.parent가 리포 루트다.
        """
        if self.repo_root:
            return Path(self.repo_root)
        return Path(__file__).resolve().parent.parent


class SettingsError(RuntimeError):
    """설정값이 잘못돼 기동할 수 없음 — 어떤 환경변수가 문제인지 이름과 값으로 알려준다."""


def _describe_validation_error(exc: "ValidationError") -> str:
    """pydantic 오류를 '어떤 PAAS_ 환경변수가 왜 틀렸는지'로 바꾼다.

    값 하나만 잘못돼도 Settings() 생성이 실패해 **플랫폼 전체가 기동하지 못한다**(콘솔
    로그인부터 모든 API까지). 그런데 기본 오류는 pydantic 내부 필드명과 트레이스백이라,
    서비스로 띄운 경우(nssm 등) 로그를 열어봐도 원인을 바로 알기 어렵다 — 프록시 쪽에서는
    그냥 전 경로 502로만 보인다. 그래서 환경변수 이름 그대로 되짚어 준다.
    """
    lines = ["설정값이 잘못돼 기동할 수 없습니다:"]
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"]) or "(알 수 없음)"
        env_name = f"PAAS_{field.upper()}"
        lines.append(f"  - {env_name}: {err['msg']} (현재 값: {err.get('input')!r})")
    lines.append("  .env 또는 환경변수를 고친 뒤 다시 기동하세요.")
    return "\n".join(lines)


@lru_cache
def get_settings() -> Settings:
    try:
        s = Settings()
    except ValidationError as e:
        raise SettingsError(_describe_validation_error(e)) from e
    for d in (
        s.work_dir, s.build_log_dir, s.caddy_sites_dir, s.k8s_manifest_dir,
        s.iis_sites_root, s.apache_sites_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
    return s
