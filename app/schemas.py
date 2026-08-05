from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import BuildProfile, DeploymentStatus, ProjectType, RedirectKind


class OrgCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")


class OrgOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    project_count: int


class GiteaSyncSkip(BaseModel):
    name: str
    kind: str  # "org" | "project"
    reason: str


class GiteaSyncResult(BaseModel):
    orgs_created: list[str]
    projects_created: list[str]
    repos_created: list[str]
    projects_deleted: list[str]
    skipped: list[GiteaSyncSkip]


class ProjectCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    type: ProjectType
    # 지정 시 리포를 조직 소속 Gitea 레포로 플랫폼이 내부 생성한다 — git_url을
    # 함께 줄 수 없다(아래 검증). 미지정 시 기존처럼 git_url을 직접 받는 레거시 경로.
    organization_id: int | None = None
    git_url: str | None = None
    branch: str = "main"
    # 모노레포에서 리포 루트가 아닌 서브디렉터리를 빌드 컨텍스트로 쓸 때 지정 (예: "platform/console")
    source_subdir: str | None = None
    domain: str | None = None
    health_check_path: str = "/"
    memory_limit: str | None = None
    cpu_limit: float | None = None
    default_profile: BuildProfile = BuildProfile.release
    llm_config: dict | None = None

    @model_validator(mode="after")
    def _git_source_exactly_one(self) -> "ProjectCreate":
        if self.organization_id is None and not self.git_url:
            raise ValueError("organization_id 또는 git_url 중 하나는 필수입니다")
        if self.organization_id is not None and self.git_url:
            raise ValueError(
                "organization_id 지정 시 git_url을 직접 지정할 수 없습니다 "
                "(내부 Gitea 리포로 자동 생성됩니다)"
            )
        return self


class ProjectUploadForm(BaseModel):
    """zip/폴더 업로드 등록용 폼 필드. git_url은 항상 조직 소속 사내 Gitea 리포로
    플랫폼이 생성하므로 organization_id가 필수다(레거시 git_url 직접 지정 경로 없음)."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    type: ProjectType
    organization_id: int
    branch: str = "main"
    domain: str | None = None
    health_check_path: str = "/"
    default_profile: BuildProfile = BuildProfile.release
    # 업로드·최초 push 완료 직후 바로 배포 큐에 올릴지 여부 (원클릭 배포)
    deploy_after_upload: bool = False


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: ProjectType
    organization_id: int | None
    org_name: str | None = None
    # 비관리자 응답에서는 마스킹된다 (api/projects.py `_serialize_project`)
    git_url: str
    branch: str
    source_subdir: str | None
    domain: str | None
    default_profile: BuildProfile
    created_at: datetime


class DeployRequest(BaseModel):
    # 빌드 옵션: development | release. 생략 시 프로젝트 기본값.
    profile: BuildProfile | None = None
    git_sha: str | None = None
    # False면 202 + building 레코드 즉시 반환, 파이프라인은 작업 큐에서 실행 (폴링으로 확인)
    wait: bool = True


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    git_sha: str
    image_tag: str
    profile: BuildProfile
    status: DeploymentStatus
    host_port: int | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    # composite 프로젝트에서만 값이 있음 — "backend"/"frontend". 일반 프로젝트는 None.
    component: str | None = None


class EnvVarSet(BaseModel):
    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value: str
    is_secret: bool = True


class LlmProviderCreate(BaseModel):
    name: str
    kind: str = Field(pattern=r"^(openai|anthropic|aws|azure|gcp|internal)$")
    base_url: str  # internal은 project://<프로젝트명> 형식만 허용 (아래 검증)
    api_key: str | None = None
    model: str
    # 미지정(None) = 전역(모든 프로젝트에서 사용 가능), 지정 시 해당 조직 소속
    # 프로젝트에서만 사용 가능 — Module.organization_id와 동일한 규칙.
    organization_id: int | None = None

    @model_validator(mode="after")
    def _internal_must_use_project_scheme(self) -> "LlmProviderCreate":
        # kind="internal"은 "소스가 사외로 나가지 않는다"는 보장의 근거다.
        # base_url을 자유 문자열로 두면 라벨만 internal이고 실제로는 외부 URL을
        # 가리키는 설정 실수(또는 악용)를 코드가 전혀 막지 못한다 — 여기서 강제한다.
        if self.kind == "internal" and not self.base_url.startswith("project://"):
            raise ValueError(
                "internal 프로바이더는 base_url이 'project://<프로젝트명>' 형식이어야 합니다 "
                "(외부 URL을 쓰려면 kind를 external로 등록하세요)"
            )
        return self


class LlmProviderOut(BaseModel):
    id: int
    name: str
    kind: str
    base_url: str
    model: str
    has_api_key: bool
    organization_id: int | None = None
    org_name: str | None = None


class ReviewRequest(BaseModel):
    provider_id: int
    diff: str | None = None  # 생략 시 base_ref..HEAD로 계산
    base_ref: str | None = None


# --- 에이전트 기획 (Agent Planning) ---


class PlanSessionCreate(BaseModel):
    project_id: int
    provider_id: int
    branch: str | None = None  # 기본: paas/plan-{session_id}


class PlanMessageIn(BaseModel):
    content: str
    files: list[str] = []  # 컨텍스트로 포함할 리포 내 파일 경로


class PlanMessageReply(BaseModel):
    reply: str  # 단계 산출물 문서 초안(마크다운)
    used_modules: list[str] = []
    context_files: list[str] = []  # 이번 요청에서 본문까지 참조한 리포 파일


class PlanConfirmIn(BaseModel):
    content: str  # 확정할 단계 산출물 본문(마크다운) — Gitea 리포에 커밋된다


class PlanArtifactOut(BaseModel):
    stage: str
    title: str
    repo_path: str
    commit_sha: str | None = None
    confirmed: bool = False
    default_request: str = ""  # 콘솔 입력창 기본값(바로 초안 생성 가능)
    # 확정 시 git 상태에 따라 자동 수행된 결과: committed | merged | pr_opened | skipped
    git_action: str | None = None
    git_detail: str | None = None
    pull_request_url: str | None = None


class PlanSessionOut(BaseModel):
    id: int
    branch: str
    provider: str
    project_id: int
    project_name: str
    artifacts: list[PlanArtifactOut] = []


class BuildTaskOut(BaseModel):
    id: int
    title: str
    detail: str = ""
    verify: str = ""  # 완료 판정 기준
    status: str
    note: str = ""
    commit_sha: str | None = None


class BuildTaskUpdate(BaseModel):
    status: str | None = None  # pending | in_progress | done | blocked
    note: str | None = None
    commit_sha: str | None = None


class ComplianceOut(BaseModel):
    """외주 빌드 결과의 LLM·모듈 사용 검증 결과."""

    project: str
    findings: list[dict] = []
    summary: dict[str, int] = {}
    # 위반이 있을 때 외주 빌더에게 그대로 전달할 수정 지시 프롬프트(없으면 빈 문자열)
    builder_prompt: str = ""


class ModuleCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    type: str = Field(pattern=r"^(external_api|internal_api|database|file_storage|mcp)$")
    # 카테고리별 API 리스팅용(예: "news", "llm") — 대화식 편집 화면의 자원 목록에서 그룹핑
    category: str | None = None
    # 지정 시 해당 조직 소속 프로젝트에만 노출("조직별 db" 등). 미지정=전역
    organization_id: int | None = None
    config: dict = {}


class ModuleBind(BaseModel):
    env_prefix: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,24}$")


class ApiModuleImport(BaseModel):
    """외부 API 디렉터리 검색 결과를 external_api 모듈로 추가할 때의 폼.

    name은 검색 결과 id(예: googleapis.com:calendar)를 그대로 받아 서버에서
    모듈명 규약으로 정규화한다(services/apisearch.normalize_module_name)."""

    name: str
    url: str
    category: str | None = None


class PreviewCreate(BaseModel):
    branch: str | None = None
    ttl_minutes: int = Field(default=60, ge=5, le=480)


class PreviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    branch: str
    url: str
    status: str
    expires_at: datetime


class RedirectRuleCreate(BaseModel):
    from_path: str = Field(min_length=1, max_length=255)
    to_path: str = Field(min_length=1, max_length=255)
    kind: str = Field(default="redirect", pattern=r"^(redirect|rewrite)$")
    status_code: int = Field(default=302, ge=300, le=399)


class RedirectRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    from_path: str
    to_path: str
    kind: RedirectKind
    status_code: int
    created_at: datetime


class ModuleUsageItem(BaseModel):
    id: int
    name: str
    type: str
    category: str | None = None
    env_prefix: str
    injected_env_keys: list[str] = []


class ModuleHistoryItem(BaseModel):
    id: int
    actor: str
    action: str
    target: str
    payload: dict = {}
    created_at: datetime


class ProjectModuleReportOut(BaseModel):
    project_id: int
    project_name: str
    org_name: str | None = None
    total_active_modules: int
    total_injected_envs: int
    active_modules: list[ModuleUsageItem] = []
    history: list[ModuleHistoryItem] = []


class GlobalModuleUsageSummary(BaseModel):
    module_id: int
    module_name: str
    type: str
    category: str | None = None
    organization_name: str | None = None
    bound_project_count: int
    bound_projects: list[str] = []
    created_at: datetime


class PlatformModuleReportOut(BaseModel):
    total_modules: int
    total_bindings: int
    modules: list[GlobalModuleUsageSummary] = []
    recent_history: list[ModuleHistoryItem] = []


class ComponentStatus(BaseModel):
    name: str  # "backend" | "frontend"
    status: str
    internal_port: int | None = None


class RedirectRuleSummary(BaseModel):
    """서버구성/배포구조 시각화에 얹는 URL redirect·rewrite 규칙 요약(id·project_id
    없이 규칙 내용만) — RedirectRuleOut의 경량판."""

    from_path: str
    to_path: str
    kind: RedirectKind
    status_code: int


class ServerConfigSite(BaseModel):
    project_id: int
    project_name: str
    profile: BuildProfile
    domain: str
    path_prefix: str
    status: str
    redirect_count: int
    redirects: list[RedirectRuleSummary]
    # composite 프로젝트만 채워짐(backend/frontend 개별 상태) — 일반 프로젝트는 None.
    components: list[ComponentStatus] | None = None
    # 프록시 설정(IIS web.config 등)에 실제로 라우팅이 구성돼 있는지 — 프록시가
    # 설정 멤버십을 추적하지 않는 백엔드(caddy/apache)에서는 None.
    in_proxy: bool | None = None


class UnregisteredSite(BaseModel):
    """프록시 설정(IIS web.config)에는 있으나 DB에 프로젝트로 등록되지 않은 라우트 —
    이름(site_name)과 rewrite 타겟 주소만 표시한다."""

    name: str
    rewrite_targets: list[str]


class ServerConfigOut(BaseModel):
    runtime_backend: str
    proxy_backend: str
    sites: list[ServerConfigSite]
    # 프록시 설정에만 존재하고 DB 프로젝트와 매칭되지 않는 항목(추적 백엔드에서만 채워짐)
    unregistered: list[UnregisteredSite] = []


class ApiKeyCreate(BaseModel):
    name: str
    is_admin: bool = False


class ApiKeyIssued(BaseModel):
    name: str
    key: str  # 발급 시 1회만 노출
    is_admin: bool


class UserRegisterRequest(BaseModel):
    email: str = Field(min_length=5, max_length=128)
    name: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=4, max_length=128)


class UserRegisterOut(BaseModel):
    name: str
    email: str
    # 가입 직후에는 세션을 발급하지 않는다 — 관리자 승인 전까지 로그인할 수 없다.
    is_approved: bool = False
    is_admin: bool = False


class UserOrgOut(BaseModel):
    id: int
    name: str


class UserAccountOut(BaseModel):
    id: int
    email: str
    name: str
    is_approved: bool
    is_admin: bool
    organization_id: int | None = None
    organization_name: str | None = None
    organizations: list[UserOrgOut] = []


class UserAccountOrganizationUpdate(BaseModel):
    organization_id: int | None = None


class UserAccountOrgModifyRequest(BaseModel):
    organization_id: int
    action: str = "add"  # "add" | "remove"


class UserLoginRequest(BaseModel):
    email: str
    password: str


class UserLoginOut(BaseModel):
    ok: bool = True
    name: str
    email: str
    key: str
    is_admin: bool = False
    organization_id: int | None = None
    organization_name: str | None = None
    organizations: list[UserOrgOut] = []
