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
    health_check_path: Mapped[str] = mapped_column(String(255), default="/")
    memory_limit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cpu_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 웹훅 자동 배포 시 사용할 기본 프로필
    default_profile: Mapped[BuildProfile] = mapped_column(
        Enum(BuildProfile), default=BuildProfile.release
    )
    # LLM 전용 확장 필드 (vLLM 옵션 등)
    llm_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 리포에서 감지한 **배포 단위 목록**(services/structure.py). type 하나로는 백엔드+
    # 프론트엔드처럼 서로 다른 템플릿·포트로 빌드돼야 하는 구성을 표현할 수 없어서, 감지
    # 결과를 그대로 둔다. type 컬럼은 이 구조의 대표값으로 남는다(structure.representative_type).
    #   {"method", "detected_at", "git_sha", "source",
    #    "components": [{"name", "path", "type"}, ...]}
    structure: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # windows_service 런타임의 기동 스크립트(start.cmd) 본문 — **컴포넌트별**로 둔다.
    #   {"": "...단일 배포용...", "api": "...", "web": "..."}
    # 복합 배포는 컴포넌트마다 유닛·포트·공개 경로가 따로이므로 스크립트도 따로여야 한다.
    # 비어 있으면(또는 그 키가 없으면) 플랫폼의 제네릭 템플릿을 쓴다
    # (services/build._START_SCRIPT — 리포 시그니처로 실행 방법을 추정).
    #
    # 값이 있으면 그것이 이긴다. 템플릿은 흔한 모양만 맞히므로, 맞지 않는 프로젝트는
    # LLM이 리포를 보고 제안한 스크립트를 **사람이 확인해** 여기에 넣는다(리포에 Dockerfile이
    # 있으면 그것을 쓰는 것과 같은 원칙 — 구체적인 의사표시가 추정보다 앞선다).
    # 서버에서 서비스 권한으로 실행되는 값이라 저장 전에 검증한다(services/startscript).
    start_scripts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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


class PortAllocation(Base):
    """호스트 포트 배정 대장 — 어느 프로젝트·프로필·컴포넌트가 어느 포트를 쓰는지.

    배정을 기록으로 남기는 이유는 services/ports.py에 적어 뒀다(요약: 동시 배포가 같은
    포트를 고르는 경쟁, 멈춘 배포의 포트를 남에게 넘기는 망각, "8123을 누가 쓰는가"에
    답할 곳이 없는 불투명).

    port에 unique를 걸어 경쟁이 삽입 충돌로 드러나게 하고, (프로젝트·프로필·컴포넌트)에도
    unique를 걸어 한 주인이 포트를 여러 개 쥐지 않게 한다. component가 NULL이면 SQLite가
    NULL끼리는 서로 다르다고 보아 뒤쪽 제약이 안 걸리므로, 없을 때는 빈 문자열을 쓴다.
    """

    __tablename__ = "port_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    port: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    profile: Mapped[BuildProfile] = mapped_column(Enum(BuildProfile))
    component: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "profile", "component", name="uq_port_owner"),
    )


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


class McpToken(Base):
    """외주 개발 에이전트가 MCP 서버에 붙을 때 쓰는 **개인** 토큰.

    외주 에이전트에게는 API 키가 없고, 관리자가 키를 나눠 주는 것도 답이 아니다 — 누구에게
    나갔는지·언제 회수하는지가 남지 않는다. SSO로 로그인한 사람이 자기 몫을 직접 발급하고,
    그 사람의 조직 권한으로 프로젝트 접근을 판정한다(security.require_project_mcp_access).

    **API 키가 아니다.** require_api_key는 이 토큰을 받지 않는다 — 개발자 기계의 설정
    파일에 놓이는 값이라, 새어도 MCP 밖으로는 아무것도 못 하게 둔다.

    UserSession과 같은 이유로 원문 대신 해시만 저장한다. is_admin을 행에 박아 두는 것도
    같은 이유다(발급 시점의 권한으로 고정 — 나중에 권한이 오르면 새로 발급받는다).
    """

    __tablename__ = "mcp_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 hex
    email: Mapped[str] = mapped_column(String(255), index=True)
    # 어느 기계·어느 도구에 넣은 토큰인지 사람이 알아보게 — 폐기할 때 고를 수 있어야 한다.
    label: Mapped[str] = mapped_column(String(128), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 쓰이고 있는 토큰인지 — 안 쓰는 토큰을 지울 근거가 된다.
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OidcAuthCode(Base):
    """paas 자체 OIDC Provider(services/oidc_provider.py)의 인증 코드 — 1회용, 60초만
    산다. UserSession과 같은 이유로 코드 원문 대신 해시만 저장한다."""

    __tablename__ = "oidc_auth_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 hex
    client_id: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(255))
    redirect_uri: Mapped[str] = mapped_column(String(512))
    nonce: Mapped[str] = mapped_column(String(255), default="")
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
    # kind="aws"(Bedrock)는 정적 키가 아니라 AWS 자격증명으로 서명한다 — 서버의
    # ~/.aws 프로필 이름. 비밀이 아니라 어떤 자격증명을 쓸지 고른 결과라서 평문이고
    # 화면에도 그대로 보인다(만료 시 어느 프로필로 재로그인해야 하는지 알아야 한다).
    aws_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str] = mapped_column(String(128))
    # Module과 동일한 조직 범위 규칙 — 미지정(NULL) = 전역(모든 프로젝트에서 사용 가능),
    # 지정 시 해당 조직 소속 프로젝트에서만 사용 가능(services/llm.py require_provider_access 참고).
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True
    )
    organization: Mapped["Organization | None"] = relationship()
    # **기본 프로바이더.** 사람이 고르지 않아도 도는 기능(배포 점검·실패 원인 분석·레포 검토)이
    # 쓸 모델이다. 그 기능들은 "어느 모델로?"를 물을 자리가 없다 — 물으면 화면마다 선택
    # 상자가 하나씩 늘고, 정작 기본값은 아무도 정하지 않는다. 하나만 참이어야 하므로
    # 설정 시 나머지를 내린다(services/llm.set_default).
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("llm_providers.id"))
    branch: Mapped[str] = mapped_column(String(128))  # 편집 대상 작업 브랜치
    # 세션 마무리(작업 브랜치를 기본 브랜치로 반영) 시각. 화면이 '브랜치 머지'와 '진행
    # 현황 업데이트' 중 무엇을 보여줄지 여기서 갈린다 — 클라이언트 state로만 두면 세션을
    # 다시 열었을 때 이미 머지한 세션에 머지 버튼이 또 보인다. 작업 지시를 재생성하면
    # 다시 None이 된다(마무리가 무효가 되므로 — api/planning.generate_build_tasks).
    merged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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


class PlanConstraint(Base):
    """모든 프로젝트에 공통으로 적용되는 기획 제약사항(관리자가 콘솔에서 등록).

    프로젝트마다 다시 말해 줄 수 없는 환경 제약(리버스 프록시 구조·외부 솔루션 금지 등)을
    한 곳에 모아 둔다. 기획 각 단계의 제약 문서(services/planning.render_constraints_doc)에
    실려 단계 대화·작업 지시 분해·외주 빌더(MCP)가 모두 같은 문장을 본다.
    """

    __tablename__ = "plan_constraints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    # 프로젝트별 작업 번호(1부터). 커밋 규약(`task #3`)과 화면에 쓰는 번호다 — 전역 id를
    # 쓰면 다른 프로젝트의 작업까지 번호를 밀어 올려서 새 프로젝트 첫 작업이 #57로
    # 시작한다. 사람이 커밋 메시지에 적는 값이라 프로젝트 안에서 읽혀야 한다.
    number: Mapped[int] = mapped_column(Integer, default=0)
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
    # (file_storage는 없다 — 저장소 경로는 PAAS_STORAGE_ROOT·PAAS_DOC_ROOTS 환경변수가
    #  정하고 접근은 /storage 창구와 사내 MCP 서버가 맡는다. services/storage.py 참고.)
    # MCP(Model Context Protocol) 서버 — 사내(api/mcp_servers.py)든 외부든 같은 타입이다.
    # env 주입은 external_api와 같은 모양
    # (URL/API_KEY)이지만, 타입을 분리해 두면 채팅 기능이 "이 프로젝트에 바인딩된
    # MCP 서버가 뭔지"를 category(자유 텍스트, 표시용일 뿐 동작에 안 씀)에 기대지
    # 않고 구조적으로 찾을 수 있다(services/mcp_client.py가 tools/list·tools/call로
    # 실제 도구를 호출).
    mcp = "mcp"
    # LLM 프로바이더(에이전트 기획의 LlmProvider와 별개) — 배포된 프로젝트 코드가 직접
    # 쓸 수 있는 LLM 엔드포인트를 자원으로 등록·바인딩한다. env 주입은 URL/API_KEY에
    # MODEL이 더해진 모양(services/modules.binding_env 참고).
    llm = "llm"


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
    # 사용 이력 리포트가 조직 이름을 붙여 준다(api/modules.get_platform_module_report).
    # LlmProvider와 같은 모양 — back_populates를 두지 않는 이유도 같다(Organization
    # 쪽에서 모듈을 거슬러 올라갈 일이 없다).
    organization: Mapped["Organization | None"] = relationship()
    # 민감 필드(api_key, dsn, password, secret)는 저장 시 Fernet 암호화됨
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModuleBinding(Base):
    __tablename__ = "module_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id"))
    env_prefix: Mapped[str] = mapped_column(String(32))  # 예: PAY → PAY_URL, PAY_API_KEY


class ApiCatalogEntry(Base):
    """외부 API 카탈로그 한 줄 — 소스에서 받아 쌓아 둔 것(services/apisearch.py).

    **검색은 이 표만 읽는다.** 수집은 따로 돌기 때문에 검색 경로에는 아웃바운드 호출이
    없고, 그래서 검색을 MCP 도구로 열 수 있다(api/mcp_servers.py의 /mcp/apis).
    모듈이 아니라 별도 표인 이유: 카탈로그는 수천 건이고 대부분 끝내 쓰이지 않는다 —
    modules에 넣으면 실제로 등록해 쓰는 자원과 "있더라"를 구분할 수 없게 된다.

    소스에서 사라진 항목은 행을 지우지 않고 removed_at을 찍는다: 잠깐 빠졌다 돌아오는
    일이 흔한데 지워 버리면 그 사이에 무엇이 있었는지도, import해 만든 모듈이 어디서
    왔는지도 남지 않는다.
    """

    __tablename__ = "api_catalog"
    __table_args__ = (
        UniqueConstraint("source", "ext_id", name="uq_api_catalog_source_ext"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # apisguru | publicdata
    ext_id: Mapped[str] = mapped_column(String(255))  # 소스가 붙인 식별자
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(128), default="")
    categories: Mapped[list] = mapped_column(JSON, default=list)
    homepage: Mapped[str] = mapped_column(String(500), default="")
    spec_url: Mapped[str] = mapped_column(String(500), default="")
    # 검색용 소문자 건초더미(ext_id+title+description+categories). SQL에서 한 번 걸러
    # 내려고 둔다 — 없으면 검색마다 카탈로그 전체를 파이썬으로 올려야 한다.
    search_text: Mapped[str] = mapped_column(Text, default="")
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # onupdate 덕에 "실제로 바뀐 행"만 시각이 움직인다 — 안 바뀐 행에는 UPDATE 자체가
    # 나가지 않으므로(services/apisearch._merge), 갱신 시각이 수집 시각에 덮이지 않는다.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class JobKind(str, enum.Enum):
    """주기 갱신이 필요한 일. **플랫폼이 이미 하고 있던 일들**이지 새로 만든 개념이 아니다.

    api_catalog는 자기 스레드를 따로 갖고 있었고(24시간 하드코딩), doc_index는 스케줄러가
    아예 없어 사람이 눌러야 했으며, 두 probe는 모듈 화면의 버튼으로만 있었다.
    """

    api_catalog = "api_catalog"  # 외부 API 카탈로그 수집(services/apisearch.sync_catalog)
    doc_index = "doc_index"      # 문서 색인 + 온톨로지(services/docsearch.reindex)
    mcp_probe = "mcp_probe"      # mcp 모듈이 실제로 응답하는지(services/mcp_client)
    api_probe = "api_probe"      # external_api 모듈 주소가 살아 있는지


class JobStatus(str, enum.Enum):
    ok = "ok"
    failed = "failed"
    skipped = "skipped"  # 할 일이 없었다(바뀐 문서 없음 등) — 실패가 아니다


class ScheduledJob(Base):
    """주기 갱신 작업 한 건과 **마지막 실행 결과**.

    행은 사람이 만들지 않는다 — services/scheduler.reconcile()이 지금 있는 것에서
    만들어 낸다(저장소가 늘면 job이 늘고, 모듈을 지우면 job이 사라진다). 목록을 손으로
    관리하면 없는 대상을 가리키는 job이 남고, 그것이 실패로 계속 뜬다.

    결과를 행에 함께 두는 이유: 대시보드가 묻는 것은 "지금 무엇이 밀려 있나"인데, 그
    답은 이력 전체가 아니라 **각 job의 마지막 상태**다. 이력이 필요하면 감사 로그를 본다.
    """

    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # kind + target 조합. 사람이 읽는 이름이자 재조정(reconcile)의 키다.
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    kind: Mapped[JobKind] = mapped_column(Enum(JobKind))
    target: Mapped[str] = mapped_column(String(128), default="")  # 저장소·모듈 이름
    interval_seconds: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status: Mapped[JobStatus | None] = mapped_column(Enum(JobStatus), nullable=True)
    last_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 연속 실패 횟수. 대시보드가 "한 번 튄 것"과 "계속 죽어 있는 것"을 가르는 값이다.
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


class WorkflowRunStatus(str, enum.Enum):
    running = "running"
    # 사람 단계에서 멈춰 있다 — 실패가 아니다. 사람이 제출하면 그 자리에서 이어 돈다.
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


class Workflow(Base):
    """조직의 워크플로 한 개 — 플랫폼 자원을 엮은 실행 가능한 그래프.

    **조직 단위다**(organization_id 필수). 한 조직이 여러 개를 갖고, 이름은 조직 안에서만
    유일하다 — 다른 조직의 "계약 검토"와 이름이 겹쳐도 상관없어야 한다.

    표현은 작은 JSON 스펙이다(services/workflow.py에 스키마와 검증기). BPMN이나 Petri net을
    쓰지 않은 이유: 요소의 대부분이 쓰이지 않고(zur Muehlen & Recker 2008), LLM이 생성·수정
    해야 하는데 XML은 검증·수선 비용이 크다. 코드/구조화 스펙이라야 LLM이 만들고 고칠 수
    있다는 것은 워크플로 자동 생성 연구의 공통된 설계 근거다(AFlow 2024, ProMoAI 2024).

    그림은 **파생물**이다 — 스펙이 원천이고 화면은 dagre로 배치해 그린다. C4와 반대인데
    (거기선 문서가 원천) 이유는 실행이다: 실행에 필요한 정보(도구·인자·분기 조건)를 담을
    자리가 있어야 한다.
    """

    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_workflow_org_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    organization: Mapped["Organization"] = relationship()
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    # {"nodes": [...], "edges": [...]} — 검증을 통과한 것만 저장한다(저장 거부가 안내다).
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    # 대화에서 LLM이 읽어 낸 것 — 개체(entity)·상태(state)·전이(transition)·제약(constraint).
    # 스펙만 보면 "그럴듯한데 내 업무가 아닌" 워크플로를 알아볼 수 없다. 모델이 무엇을
    # 업무로 이해했는지를 따로 내놓게 하고 사람이 그것을 검토한다 — 자연어에서 프로세스를
    # 뽑는 연구가 중간 표현을 두는 이유와 같다(Friedrich 2011 이후, ProMoAI 2024).
    extracted: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 저장마다 1 증가. 실행 기록이 "어느 판을 돌렸는지" 가리킬 수 있어야 한다.
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WorkflowMessage(Base):
    """구성 대화 한 줄. 대화식으로 고치려면 이전에 무엇을 요청했는지가 남아야 한다.

    산출물(스펙)은 Workflow.spec에 있고 여기엔 대화만 둔다 — 같은 분리를 기획(plan) 쪽에서
    쓰고 있고, 그래야 "대화는 길지만 스펙은 한 벌"이 유지된다.
    """

    __tablename__ = "workflow_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkflowRun(Base):
    """실행 한 번 — 단계별 결과와, 사람 단계에서 멈춘 자리까지.

    outputs를 행에 두는 이유: 사람 단계에서 멈춘 실행은 **몇 시간 뒤에** 이어진다. 그때
    앞 단계 출력이 없으면 처음부터 다시 돌려야 하고, LLM 단계가 있으면 그건 돈과 시간이다.
    """

    __tablename__ = "workflow_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    # 돌린 시점의 판(Workflow.version) — 스펙은 그 뒤에 바뀔 수 있다.
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[WorkflowRunStatus] = mapped_column(
        Enum(WorkflowRunStatus), default=WorkflowRunStatus.running
    )
    actor: Mapped[str] = mapped_column(String(64), default="")
    # [{"id","type","status","summary","ms"}] — 화면이 진행을 보여 주는 근거
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 노드 id → {"text","paths"} (상한을 넘으면 잘라 표시한다)
    outputs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 사람 단계에서 멈춰 있을 때 그 노드 id. 비어 있으면 멈춘 자리가 없다.
    pending_node: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class InfoSource(Base):
    """정보 출처 하나 — 웹사이트·API·MCP 서버. 스캔해서 저장소에 문서로 옮긴다.

    **요청 헤더(쿠키·토큰)는 암호화해 두고 이름만 내보낸다.** 사내 사이트는 대부분 로그인
    뒤에 있어 쿠키 없이는 메뉴 하나 못 본다. 그러나 그 값은 사람의 세션이다 — 화면·감사·
    LLM 어디에도 실리면 안 된다(services/infosource가 요청을 보낼 때만 복호화한다).

    스캔 결과와 저장 제안은 행에 둔다. 저장은 사람이 결정하는 별도 단계라, 스캔과 저장
    사이에 시간이 흐른다 — 그 사이에 결과를 다시 만들 이유가 없다.
    """

    __tablename__ = "info_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # web | api | mcp
    url: Mapped[str] = mapped_column(String(1024))
    # {"이름": "값"} JSON을 통째로 암호화한 것. 비어 있으면 헤더 없음.
    headers_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    # new | scanning | scanned | failed | saved
    status: Mapped[str] = mapped_column(String(16), default="new")
    scan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 저장 제안 — {"targets": {유형: {"mode": "existing"|"new", "store", "path", "reason", "evidence"}}}
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    target_store: Mapped[str] = mapped_column(String(64), default="")
    # 지난 저장에서 쓴 파일 — [{"category", "store", "path", "sha"}]. 다음 저장이 같은 자리에
    # 덮어쓰고, 이번에 없는 것을 휴지통으로 옮기는 근거다(파일마다 이력은 두지 않는다).
    saved_files: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    scans: Mapped[list["InfoSourceScan"]] = relationship(
        back_populates="source", cascade="all, delete-orphan")


class InfoSourceScan(Base):
    """스캔 한 번의 이력 — 주소마다 결과(받음·쉼·HTTP 상태)와 내용 해시.

    다음 스캔이 범위를 정하는 데 쓴다(지난번에 받은 화면을 먼저 다시 보고, 없던 주소는 한 번
    쉰다). 바뀜 집계(new·changed·same·gone)는 화면의 스캔 이력에 그대로 보인다.
    """

    __tablename__ = "info_source_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("info_sources.id", ondelete="CASCADE"), index=True)
    via: Mapped[str] = mapped_column(String(16))      # server | browser
    status: Mapped[str] = mapped_column(String(16))   # done | failed
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    files: Mapped[int] = mapped_column(Integer, default=0)
    changes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # {url: {"kind": page|file|skip, "status", "sha", "depth"}}
    urls: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[InfoSource] = relationship(back_populates="scans")


class PersonalContext(Base):
    """스마트워크의 **개인** 업무 맥락 — 한 사람에 한 행(email).

    행이 있다는 것이 곧 동의다. 동의 없이 개인 문서·메일을 받지 않고, 철회하면 행과 함께
    변환한 문서·색인·그래프를 전부 지운다(services/personal.revoke). 문서 본문은 여기 두지
    않는다 — 변환한 마크다운이 사용자별 숨은 저장소에 파일로 있고, 색인은 그 저장소 이름으로
    갈린다. **원본은 서버 어디에도 남지 않는다**(변환하고 바로 지운다).

    메일 토큰은 두지 않는다 — 로그인과 Graph 읽기는 사용자 브라우저가 하고, 여기에는
    메일 내용(마크다운)만 온다.
    """

    __tablename__ = "personal_contexts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # [{"name": 폴더 이름, "files": 개수, "synced_at": ISO 시각}] — 다시 동기화할 대상
    folders: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # {"폴더/상대경로": {"size", "mtime", "error"}} — PC 원본의 크기·수정 시각(내용 없음).
    # 원본을 남기지 않으니 "바뀐 파일" 판정의 근거가 여기밖에 없다. error는 변환 실패 사유.
    files: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 메일을 가져온 계정(화면 표시용) — 비밀이 아니다.
    mail_account: Mapped[str] = mapped_column(String(255), default="")
    mail_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
