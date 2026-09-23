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
    # 관리자와 그 프로젝트 조직 소속(전역 프로젝트는 누구나) 외에는 마스킹된다
    # (api/projects.py `_serialize_project`, security.py `can_view_git_url`)
    git_url: str
    branch: str
    source_subdir: str | None
    default_profile: BuildProfile
    # 리포에서 감지한 배포 단위 목록(services/structure.py). type 하나로는 백엔드+
    # 프론트엔드처럼 서로 다른 템플릿·포트로 빌드돼야 하는 구성을 표현할 수 없다.
    # 화면은 이 목록을 배지로 보여준다. 아직 감지하지 않은 프로젝트는 null.
    structure: dict | None = None
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


class StartScriptProposeIn(BaseModel):
    """LLM 호출 요청. provider_id를 비우면 **기본 프로바이더**로 돈다.

    기동 스크립트 작성은 사람이 고르는 자리가 있어서 그대로 받고(화면에 선택 상자가 있다),
    실패 원인 분석처럼 버튼 하나로 도는 자리에서는 비워 보낸다 — 그때마다 모델을 묻는 것은
    일을 하나 더 만드는 것이다(services/llm.default_provider).
    """

    provider_id: int | None = None


class StartScriptSet(BaseModel):
    """사람이 확인한 기동 스크립트. 검증을 통과하지 못하면 저장하지 않는다."""

    script: str = Field(min_length=1)


class StartScriptOut(BaseModel):
    """제안·현재 스크립트와 **검증 결과**. problems가 비어 있지 않으면 저장할 수 없다."""

    script: str
    problems: list[str] = []
    # 프롬프트에 실은 사실 — 무엇을 보고 쓴 것인지 사람이 확인할 수 있어야 한다.
    facts: str = ""
    source: str = "template"  # template | project
    # 이 스크립트가 어느 유닛의 것인지. 복합 배포는 컴포넌트마다 스크립트가 따로다
    # (유닛·포트·공개 경로가 이미 컴포넌트별이다). ""는 단일 배포.
    component: str = ""
    # 프로필도 따로다 — 개발 배포는 dev 서버로, 운영 배포는 빌드본으로 뜬다.
    profile: BuildProfile = BuildProfile.release
    # LLM이 몇 번 썼는지. 검증에 걸리면 그 문구를 돌려주고 한 번 고치게 한다 —
    # 2면 "한 번 고쳐 다시 제안한 것"이다(화면이 그 사실을 말해 준다).
    attempts: int = 1
    # 이 프로젝트에서 고를 수 있는 컴포넌트 — 화면이 목록을 따로 조회하지 않게 함께 준다.
    components: list[str] = []


class ProjectSourceSubdirSet(BaseModel):
    """빌드 대상 폴더 — 배포 실패 진단이 제안한 값을 적용하는 입력.

    프로젝트 설정 전체를 고치는 경로를 열지 않는다: 진단이 제안하는 것은 이 값 하나이고,
    다른 값(타입·git_url 등)을 함께 받으면 "진단 적용"이 무엇을 바꾼 것인지 알 수 없게 된다.
    빈 문자열은 "리포 루트"다(지정 해제).
    """

    source_subdir: str = Field(default="", max_length=255)


class EnvVarSet(BaseModel):
    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value: str
    is_secret: bool = True


class McpTokenCreate(BaseModel):
    # 어느 기계·어느 도구에 넣은 토큰인지 — 폐기할 때 고를 수 있어야 한다.
    label: str = ""
    # 주면 그 프로젝트의 MCP 주소까지 함께 돌려준다(붙여 넣을 설정을 화면이 만들어 준다).
    project: str | None = None


class McpTokenIssued(BaseModel):
    """발급 응답 — 원문은 여기 **한 번만** 실린다(해시만 저장하므로 다시 볼 수 없다)."""

    token: str
    url: str = ""
    # 게이트웨이(/proxy·/a2a)의 기준 주소. 에이전트는 MCP로 "무엇을 만들지"를 읽고
    # 게이트웨이로 자원에 닿는다 — 둘을 같이 주지 않으면 주소를 손으로 짜맞추게 된다.
    gateway_base_url: str = ""
    ttl_days: int


class McpTokenOut(BaseModel):
    id: int
    label: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    # 만료된 행도 남긴다 — "사라졌다"가 아니라 "만료됐다"로 보여야 다시 발급할 줄 안다.
    is_expired: bool = False


class LlmProviderCreate(BaseModel):
    name: str
    kind: str = Field(pattern=r"^(openai|anthropic|aws|azure|gcp|internal)$")
    # internal은 project://<프로젝트명> 형식만 허용 (아래 검증). aws는 자격증명 프로필을
    # 고르면 비워도 된다 — 프로필의 리전이 런타임 주소를 결정한다(사람이 적을 값이 아니다).
    base_url: str = ""
    api_key: str | None = None
    # kind="aws"(Bedrock)는 정적 키 대신 서버 ~/.aws의 자격증명 프로필로 서명한다.
    aws_profile: str | None = None
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
        # aws 외의 종류는 자격증명 프로필을 쓰지 않는다 — 받아 두면 저장은 되고
        # 호출에는 안 쓰여서, 설정한 사람은 적용됐다고 믿는다.
        if self.aws_profile and self.kind != "aws":
            raise ValueError("aws_profile은 kind='aws'(Bedrock)에서만 사용합니다")
        # Endpoint가 필요한지는 고른 프로필이 정한다. Bedrock을 자격증명으로 부를 때는
        # 리전에서 주소가 유도되므로 비워도 되고, 그 밖에는 어디로 보낼지 알 수 없다.
        if not self.base_url and not (self.kind == "aws" and self.aws_profile):
            raise ValueError("base_url이 필요합니다 (aws는 자격증명 프로필을 고르면 생략 가능)")
        return self


class LlmProviderOut(BaseModel):
    id: int
    name: str
    kind: str
    base_url: str
    model: str
    has_api_key: bool
    # 사람이 고르지 않아도 도는 판단(배포 점검·실패 원인 분석·레포 검토)이 쓰는 모델.
    # 하나만 참이다 — 화면마다 선택 상자를 늘리는 대신 여기서 한 번 정한다.
    is_default: bool = False
    aws_profile: str | None = None
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
    branch: str | None = None  # 기본: paas/plan-{session_id}-{hex}


class PlanMessageIn(BaseModel):
    content: str
    # 지금 편집 중인 산출물 본문 — 주면 새로 쓰지 않고 이것을 고친다.
    draft: str = ""
    # 컨텍스트 한도 초과(413) 후 재시도 — 앞 단계 문서를 개요로 줄이고 코드 컨텍스트를 뺀다.
    compact: bool = False


class PlanMessageReply(BaseModel):
    summary: str  # 대화창에 보일 응답 개요(무엇을 담았는지·무엇을 고쳤는지)
    document: str  # 산출물 편집기에 들어갈 문서 본문(마크다운)
    used_modules: list[str] = []
    context_files: list[str] = []  # 이번 요청에서 본문까지 참조한 리포 파일
    bound_modules: list[str] = []  # 솔루션 구성 단계에서 이번에 바인딩된 모듈
    compacted: bool = False  # 압축된 컨텍스트로 생성됐는지
    # 길이 제한에서 잘렸는지. 부분 결과는 그대로 싣는다 — 버리면 사람이 쓴 요청과 모델이
    # 만든 본문을 되살릴 수 없다. 화면이 경고를 띄워 잘린 문서를 확정하지 않게 한다.
    truncated: bool = False


class PlanArtifactContentOut(BaseModel):
    """단계 산출물 본문 — 세션 재개·단계 이동 시 편집기를 채운다."""

    stage: str
    repo_path: str
    content: str
    confirmed: bool = False
    # session = 이 세션에서 확정한 산출물 · repo = 리포에 이미 있던 문서
    # tasks = 작업 지시 목록에서 렌더한 문서(5단계) · "" = 없음
    source: str = ""


class PlanConfirmIn(BaseModel):
    content: str  # 확정할 단계 산출물 본문(마크다운) — Gitea 리포에 커밋된다
    # 리포에 이미 다른 내용의 같은 문서가 있을 때만 필요 — 확인 없이 덮어쓰지 않는다.
    overwrite: bool = False


class PlanMergeOut(BaseModel):
    """세션 마무리 — 작업 브랜치를 기본 브랜치로 반영한 결과."""

    branch: str
    action: str  # merged | pr_opened | committed | skipped
    detail: str | None = None
    pull_request_url: str | None = None


class PlanArtifactOut(BaseModel):
    stage: str
    title: str
    repo_path: str
    commit_sha: str | None = None
    confirmed: bool = False
    default_request: str = ""  # 콘솔 입력창 기본값(바로 생성 요청 가능)
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
    # 세션을 마무리(브랜치 머지)한 시각. 화면이 '브랜치 머지'와 '진행 현황 업데이트' 중
    # 무엇을 보여줄지 이 값으로 갈린다 — 세션을 다시 열어도 상태가 유지되어야 한다.
    merged_at: datetime | None = None
    artifacts: list[PlanArtifactOut] = []


class PlanSessionSummary(BaseModel):
    """기획 세션 이력 한 줄 — 목록에서 재개·삭제 대상을 고르기 위한 최소 정보."""

    id: int
    project_id: int
    project_name: str
    provider: str
    branch: str
    confirmed_stages: list[str] = []
    task_count: int = 0
    created_at: datetime | None = None


class PlanChatMessageOut(BaseModel):
    """세션 재개 시 복원할 대화 한 줄."""

    role: str
    content: str
    created_at: datetime | None = None


class BuildTaskOut(BaseModel):
    id: int
    # 프로젝트별 작업 번호 — 커밋 규약(`task #3`)에 쓰는 값. id는 API 식별자다.
    number: int = 0
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


class BuildTaskSyncOut(BaseModel):
    """작업 지시 진행 현황을 기본 브랜치 기준으로 맞춘 결과."""

    base_ref: str  # 판정 기준 ref(예: origin/main) — 비어 있으면 판정하지 못함
    merged: int  # 커밋이 기본 브랜치에 반영된 작업 수
    pending: int  # 커밋은 있지만 아직 기본 브랜치에 없는 작업 수
    # 판정 근거(커밋 메시지의 `task #N` 참조 또는 빌더가 보고한 sha)를 찾지 못한 작업 수.
    # 이 값이 없으면 "반영 0건 · 대기 0건"이 찍혀서, 판정할 것이 없었던 것과 반영이 없는
    # 것이 구분되지 않는다 — 화면이 동작하는 것처럼 보이면서 아무 일도 하지 않는다.
    unmatched: int = 0
    tasks: list[BuildTaskOut]


class ComplianceOut(BaseModel):
    """외주 빌드 결과의 LLM·모듈 사용 검증 결과."""

    project: str
    findings: list[dict] = []
    summary: dict[str, int] = {}
    # 위반이 있을 때 외주 빌더에게 그대로 전달할 수정 지시 프롬프트(없으면 빈 문자열)
    builder_prompt: str = ""


class C4ElementOut(BaseModel):
    """C4 다이어그램의 요소 하나 — 확정 산출물의 mermaid 블록에서 읽은 것(services/c4)."""

    kind: str  # 문서에 쓰인 선언 그대로(Person_Ext·ContainerDb 등)
    base: str  # person | system | container | component | boundary
    alias: str
    label: str
    technology: str = ""
    description: str = ""
    external: bool = False  # _Ext 선언 — 우리가 만들지 않는 사용자·시스템·솔루션
    shape: str = "box"  # box | db | queue | person | boundary
    boundary_type: str = ""  # enterprise | system | container (base=boundary일 때)
    parent: str | None = None  # 이 요소를 감싼 경계의 alias
    link: str = ""  # $link — 이 component가 구현되는 리포 경로
    tags: str = ""
    paths: list[str] = []  # link/이름으로 찾은 실제 리포 파일 — code 레벨의 대상


class C4RelationOut(BaseModel):
    source: str
    target: str
    label: str = ""
    technology: str = ""
    bidirectional: bool = False


class C4LevelOut(BaseModel):
    """레벨 하나의 그림과 그것이 실려 있던 단계."""

    stage: str  # 이 그림이 실려 있던 산출물의 단계
    title: str = ""
    # False = 편집 중 초안에서 읽은 그림(아직 확정 전). 그림은 확정을 검토하는 도구이므로
    # 초안에서도 보여야 하고, 화면은 둘을 구분해 표시해야 한다.
    confirmed: bool = True
    elements: list[C4ElementOut] = []
    relations: list[C4RelationOut] = []


class C4In(BaseModel):
    """시각화 요청 — 편집 중인 초안을 함께 주면 확정 전에도 그림을 본다."""

    stage: str = ""  # 초안이 속한 단계(비우면 확정 산출물만 본다)
    draft: str = ""


class C4ModelOut(BaseModel):
    """단계별 C4 시각화 모델 — 어느 레벨이 있는지는 각 단계 문서가 정한다."""

    project_id: int
    levels: dict[str, C4LevelOut] = {}  # context | container | component


class PlanConstraintIn(BaseModel):
    """모든 프로젝트에 적용되는 공통 제약사항 한 건(여러 줄 가능)."""

    text: str


class PlanConstraintOut(BaseModel):
    id: int
    text: str
    created_at: datetime | None = None


class ModuleCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    type: str = Field(pattern=r"^(external_api|internal_api|database|mcp|llm)$")
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
    # 프록시가 실제로 전달하는 업스트림(예: 127.0.0.1:8123). 지금 running인 배포
    # 레코드에서 가져오므로, 떠 있지 않으면 None이다.
    internal_host: str | None = None
    internal_port: int | None = None
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


class WindowsServiceOut(BaseModel):
    """실제로 등록돼 있는 Windows Service 한 건(windows_service 런타임에서만 채워진다)."""

    name: str
    state: str  # running | stopped | unknown
    # 이름에서 역산하지 않고 DB 프로젝트의 예상 이름과 맞춰 채운다 — 프로젝트 이름에
    # 하이픈이 들어가면 역산은 틀린다. 못 맞추면 None(= 지워진 프로젝트의 잔여 서비스).
    project_name: str | None = None
    profile: BuildProfile | None = None
    slot: str | None = None
    # 같은 프로젝트·프로필의 슬롯이 둘 다 남아 있음 — 다음 배포를 막는 상태였다.
    duplicate_slot: bool = False


class ServerConfigOut(BaseModel):
    runtime_backend: str
    proxy_backend: str
    sites: list[ServerConfigSite]
    # 프록시 설정에만 존재하고 DB 프로젝트와 매칭되지 않는 항목(추적 백엔드에서만 채워짐)
    unregistered: list[UnregisteredSite] = []
    # windows_service 런타임에서만 채워진다(그 외에는 빈 목록).
    windows_services: list[WindowsServiceOut] = []


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
