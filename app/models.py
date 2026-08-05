import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProjectType(str, enum.Enum):
    react = "react"
    python = "python"
    node = "node"
    llm = "llm"
    html = "html"  # 정적 HTML/CSS/JS — 빌드 단계 없이 그대로 서빙
    streamlit = "streamlit"  # Streamlit 앱 (streamlit run) — python(FastAPI) 타입과는 별개
    composite = "composite"  # 백엔드+프론트엔드 복합 — 리포 안 backend/, frontend/ 서브폴더를
    # 자동 감지해 두 컴포넌트를 각각 빌드·배포한다 (services/build.py의
    # detect_composite_components, services/deployer.py의 deploy_composite_sync 참고)


class BuildProfile(str, enum.Enum):
    """빌드 옵션. development는 디버깅용 경량 실행, release는 운영용 최적화 빌드."""

    development = "development"
    release = "release"


class DeploymentStatus(str, enum.Enum):
    building = "building"
    running = "running"
    failed = "failed"
    stopped = "stopped"


class Organization(Base):
    """조직별 작업공간. 생성 시 사내 Gitea에 동명의 Organization을 함께 만든다
    (services/gitea.py). 이름은 Gitea org명과 동일하게 유지한다."""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    projects: Mapped[list["Project"]] = relationship(back_populates="organization")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    type: Mapped[ProjectType] = mapped_column(Enum(ProjectType))
    # 조직 소속 프로젝트는 git_url을 Gitea API로 내부 생성한다(사용자 직접 지정 불가) —
    # api/projects.py 참고. organization_id가 없는 레거시 프로젝트만 git_url을 직접 받는다.
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True
    )
    git_url: Mapped[str] = mapped_column(String(512))
    branch: Mapped[str] = mapped_column(String(128), default="main")
    # 모노레포에서 리포 루트가 아닌 서브디렉터리를 빌드 컨텍스트로 쓸 때 지정
    # (예: "platform/console"). 미지정 시 기존처럼 리포 루트 전체가 컨텍스트.
    source_subdir: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 미지정 시 {name}.{base_domain} / development는 {name}-dev.{base_domain}
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    health_check_path: Mapped[str] = mapped_column(String(255), default="/")
    memory_limit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cpu_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 웹훅 자동 배포 시 사용할 기본 프로필
    default_profile: Mapped[BuildProfile] = mapped_column(
        Enum(BuildProfile), default=BuildProfile.release
    )
    # LLM 전용 확장 필드 (vLLM 옵션 등)
    llm_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    organization: Mapped["Organization | None"] = relationship(back_populates="projects")
    deployments: Mapped[list["Deployment"]] = relationship(back_populates="project")
    env_vars: Mapped[list["EnvVar"]] = relationship(back_populates="project")


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    git_sha: Mapped[str] = mapped_column(String(40))
    image_tag: Mapped[str] = mapped_column(String(255))
    profile: Mapped[BuildProfile] = mapped_column(Enum(BuildProfile))
    status: Mapped[DeploymentStatus] = mapped_column(
        Enum(DeploymentStatus), default=DeploymentStatus.building
    )
    # 1차(small)에서 Caddy 업스트림으로 쓰는 호스트 포트. 2차(k8s)에서는 None.
    host_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    build_log_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # composite 프로젝트 전용 — "backend"/"frontend" 중 어느 컴포넌트의 배포 행인지.
    # 일반(단일 컴포넌트) 프로젝트는 항상 None(기존 조회 결과 불변).
    component: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 같은 배포 시도에서 함께 만들어진 backend/frontend 행을 묶는 상관키(uuid4 hex).
    deploy_group_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # composite 컴포넌트의 컨테이너 내부 포트 — 롤백/복구 시 리포를 다시 체크아웃해
    # 타입을 재감지하지 않고도 이 값으로 바로 재기동할 수 있도록 빌드 시점에 저장한다.
    internal_port: Mapped[int | None] = mapped_column(Integer, nullable=True)

    project: Mapped[Project] = relationship(back_populates="deployments")


class EnvVar(Base):
    __tablename__ = "env_vars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    key: Mapped[str] = mapped_column(String(128))
    value_encrypted: Mapped[str] = mapped_column(Text)  # Fernet 암호문. 평문 저장 금지.
    is_secret: Mapped[bool] = mapped_column(Boolean, default=True)

    project: Mapped[Project] = relationship(back_populates="env_vars")


class ApiKey(Base):
    """기계용 키 — issue_key()가 만드는 256비트 난수라 sha256으로 충분하다.

    사람이 정한 비밀번호는 여기 저장하지 않는다(UserAccount 참고). 난수 키와 달리
    비밀번호는 추측 가능한 공간에 있어서 빠른 해시로는 지킬 수 없다.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), index=True)  # sha256 hex
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserOrganization(Base):
    """사용자-조직 다대다 매핑 테이블."""

    __tablename__ = "user_organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)


class UserAccount(Base):
    """사람 계정 — 비밀번호는 솔트 + scrypt로만 저장한다(security.hash_password)."""

    __tablename__ = "user_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    # 관리자가 승인해야 로그인할 수 있다 — 도메인만 맞으면 누구나 들어오는 것을 막는다.
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True
    )
    organization: Mapped["Organization | None"] = relationship()
    organizations: Mapped[list["Organization"]] = relationship(
        secondary="user_organizations",
        lazy="selectin",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSession(Base):
    """로그인 세션 — 비밀번호에서 유도되지 않는 난수 토큰. 만료되고 폐기할 수 있다."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 hex
    email: Mapped[str] = mapped_column(String(255), index=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LlmProviderKind(str, enum.Enum):
    openai = "openai"        # OpenAI Official API (GPT-4o, o1 등)
    anthropic = "anthropic"  # Anthropic Official API (Claude 3.5 Sonnet 등)
    aws = "aws"              # AWS Bedrock (Claude, Titan, Llama 3 등)
    azure = "azure"          # Azure OpenAI Service
    gcp = "gcp"              # GCP Vertex AI / Gemini API
    internal = "internal"    # 사내 배포 LLM (vLLM, Ollama)
    external = "external"    # 기존 DB 레코드 하위 호환용 (OpenAI로 간주)


class LlmProvider(Base):
    """OpenAI 호환 chat completions 엔드포인트로 통일해 외부/내부를 같은 인터페이스로 다룬다."""

    __tablename__ = "llm_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[LlmProviderKind] = mapped_column(Enum(LlmProviderKind))
    # internal은 "project://<llm 프로젝트명>" 표기를 허용 — 배포 도메인으로 자동 해석
    base_url: Mapped[str] = mapped_column(String(512))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(128))
    # Module과 동일한 조직 범위 규칙 — 미지정(NULL) = 전역(모든 프로젝트에서 사용 가능),
    # 지정 시 해당 조직 소속 프로젝트에서만 사용 가능(services/llm.py require_provider_access 참고).
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True
    )
    organization: Mapped["Organization | None"] = relationship()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("llm_providers.id"))
    branch: Mapped[str] = mapped_column(String(128))  # 편집 대상 작업 브랜치
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanStage(str, enum.Enum):
    """에이전트 기획의 순차 단계. 순서는 아래 정의 순서를 따른다."""

    spec = "spec"  # 기획서 확정
    architecture = "architecture"  # 아키텍처 설계
    solution = "solution"  # 솔루션 구성(내부 솔루션 사용)
    principles = "principles"  # 개발원칙
    tasks = "tasks"  # 작업 지시(외주 빌드 단위) — 산출물은 BuildTask를 렌더한 문서


class PlanArtifact(Base):
    """단계 산출물 포인터. 본문은 프로젝트 Gitea 리포에 커밋되고 여기엔 위치·커밋·확정만 둔다."""

    __tablename__ = "plan_artifacts"
    __table_args__ = (UniqueConstraint("session_id", "stage", name="uq_plan_artifact_session_stage"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    stage: Mapped[PlanStage] = mapped_column(Enum(PlanStage))
    repo_path: Mapped[str] = mapped_column(String(255))  # 예: docs/agent-planning/01-기획서.md
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BuildTaskStatus(str, enum.Enum):
    pending = "pending"  # 발주됨(대기)
    in_progress = "in_progress"  # 외주 빌더가 착수
    done = "done"  # 구현 완료 보고
    blocked = "blocked"  # 질의·차단으로 진행 불가


class BuildTask(Base):
    """외주 빌드 작업 지시(work order).

    확정된 기획 산출물에서 분해되어 나오고, 외부 빌더가 MCP로 조회·갱신한다.
    산출물이 '무엇을 만들지'라면 이 표는 '어디까지 됐는지'다.
    """

    __tablename__ = "build_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str] = mapped_column(Text, default="")
    # 완료 판정 기준(기획서의 성공 기준을 작업 단위로 내린 것)
    verify: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[BuildTaskStatus] = mapped_column(
        Enum(BuildTaskStatus), default=BuildTaskStatus.pending
    )
    note: Mapped[str] = mapped_column(Text, default="")  # 빌더의 마지막 보고·질의
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ModuleType(str, enum.Enum):
    external_api = "external_api"
    internal_api = "internal_api"
    database = "database"
    file_storage = "file_storage"
    # 외부 MCP(Model Context Protocol) 서버 — env 주입은 external_api와 같은 모양
    # (URL/API_KEY)이지만, 타입을 분리해 두면 채팅 기능이 "이 프로젝트에 바인딩된
    # MCP 서버가 뭔지"를 category(자유 텍스트, 표시용일 뿐 동작에 안 씀)에 기대지
    # 않고 구조적으로 찾을 수 있다(services/mcp_client.py가 tools/list·tools/call로
    # 실제 도구를 호출).
    mcp = "mcp"


class Module(Base):
    """코드가 의존하는 외부/내부 자원. 바인딩 시 규약된 환경변수로 자동 주입된다."""

    __tablename__ = "modules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[ModuleType] = mapped_column(Enum(ModuleType))
    # 자유 텍스트 분류(예: "news", "llm", "payment") — 대화식 편집 화면의 자원
    # 리스팅에서 API를 카테고리별로 묶어 보여주는 용도. 미지정이면 "기타"로 묶인다.
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 미지정(NULL) = 전역(모든 프로젝트에 노출), 지정 시 해당 조직 소속 프로젝트에만 노출
    # ("조직별 db" 등 조직 전용 자원).
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True
    )
    # 민감 필드(api_key, dsn, password, secret)는 저장 시 Fernet 암호화됨
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModuleBinding(Base):
    __tablename__ = "module_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id"))
    env_prefix: Mapped[str] = mapped_column(String(32))  # 예: PAY → PAY_URL, PAY_API_KEY


class RedirectKind(str, enum.Enum):
    redirect = "redirect"  # 브라우저 301/302 리다이렉트
    rewrite = "rewrite"  # 서버 내부 재작성(클라이언트에 노출 안 됨)


class RedirectRule(Base):
    """프로젝트별 URL redirect/rewrite 규칙. 배포 시 리버스프록시(Caddy/IIS/Apache)
    사이트 설정에 반영된다 — services/proxy 참고."""

    __tablename__ = "redirect_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    from_path: Mapped[str] = mapped_column(String(255))
    to_path: Mapped[str] = mapped_column(String(255))
    kind: Mapped[RedirectKind] = mapped_column(Enum(RedirectKind), default=RedirectKind.redirect)
    # redirect일 때만 의미 있음(301/302 등). rewrite는 항상 무시된다.
    status_code: Mapped[int] = mapped_column(Integer, default=302)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PreviewStatus(str, enum.Enum):
    running = "running"
    expired = "expired"
    failed = "failed"


class PreviewSession(Base):
    """편집 브랜치의 TTL 임시 프리뷰. development 프로필로 빌드해 별도 유닛으로 기동한다."""

    __tablename__ = "preview_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    branch: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[PreviewStatus] = mapped_column(Enum(PreviewStatus), default=PreviewStatus.running)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaymentStatus(str, enum.Enum):
    ready = "ready"  # 승인 요청 접수(토스 호출 전)
    confirmed = "confirmed"
    canceled = "canceled"
    failed = "failed"


class Payment(Base):
    """레거시 결제 기록.

    결제 런타임은 CHO-FAM Functions로 이관됐다. 기존 Platform DB의 운영 기록을
    파괴하지 않기 위해 테이블 매핑만 유지하며 Platform API에서는 사용하지 않는다.
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    payment_key: Mapped[str] = mapped_column(String(200), index=True)
    amount: Mapped[int] = mapped_column(Integer)  # KRW 정수
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), default=PaymentStatus.ready)
    method: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 카드/가상계좌 등
    source: Mapped[str] = mapped_column(String(64))  # 호출한 API 키 이름
    fail_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuditEvent(Base):
    """감사 로그. 2차(대기업) 요구를 위해 1차부터 배포·롤백·시크릿 변경·키 발급을 기록한다."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))  # API 키 이름 또는 "webhook"
    action: Mapped[str] = mapped_column(String(64))  # deploy / rollback / env.set / key.issue ...
    target: Mapped[str] = mapped_column(String(255))
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
