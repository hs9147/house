"""에이전트 기획(Agent Planning) — 단계 정의·스테이지 프롬프트·가용 모듈 제약 문서.

에이전트 빌더가 곧바로 코드 diff를 만들던 것과 달리, 기획은 코딩 전 4단계를 순차
수행하며 각 단계에서 **문서 산출물**을 만든다. 산출물 본문은 프로젝트 Gitea 리포에
커밋되고(services/workspace.write_and_commit), DB(PlanArtifact)에는 포인터만 남는다.

설계 근거: docs/agent-planning/ (기획서·아키텍처·솔루션 구성·개발원칙).
"""
import json

from sqlalchemy.orm import Session

from ..models import PlanStage, Project
from . import a2a as a2a_service
from . import modules as modules_service

# 산출물이 커밋되는 리포 내 표준 경로(개발도구가 clone/open으로 그대로 열람).
ARTIFACT_DIR = "docs/agent-planning"

# 단계별 메타: 순서·제목·리포 파일명·문서 작성 지시(스테이지 프롬프트).
STAGES: dict[PlanStage, dict[str, str]] = {
    PlanStage.spec: {
        "title": "기획서",
        "filename": "01-기획서.md",
        "prompt": (
            "이번 단계는 '기획서 확정'이다. 요구사항·목적·범위·사용자 시나리오·성공 기준을 "
            "구조화해 확정 가능한 기획서 문서를 작성하라."
        ),
    },
    PlanStage.architecture: {
        "title": "아키텍처 설계",
        "filename": "02-아키텍처설계.md",
        "prompt": (
            "이번 단계는 '아키텍처 설계'다. 확정된 기획서를 바탕으로 컴포넌트·데이터 흐름·경계·"
            "비기능 요건을 담은 아키텍처 설계 문서를 작성하라."
        ),
    },
    PlanStage.solution: {
        "title": "솔루션 구성",
        "filename": "03-솔루션구성.md",
        "prompt": (
            "이번 단계는 '솔루션 구성'이다. 아래 '가용 모듈 제약'에 명시된 내부 모듈/자원만 "
            "사용하고, 외부 직접 호출 대신 중앙 게이트웨이(A2A/프록시) 경유를 전제로 솔루션 구성 "
            "문서를 작성하라. 제약에 없는 자원은 사용하지 말라."
        ),
    },
    PlanStage.principles: {
        "title": "개발원칙",
        "filename": "04-개발원칙.md",
        "prompt": (
            "이번 단계는 '개발원칙'이다. 구현계획 및 현황관리, 스키마 및 의사결정사항, 배포 및 "
            "사용 가이드를 포함한 개발원칙 문서를 작성하라."
        ),
    },
}

STAGE_ORDER: list[PlanStage] = list(STAGES.keys())

# 문서 작성 지시의 공통 골격 — 코드 diff가 아니라 마크다운 문서를 만들게 한다.
PLANNING_SYSTEM_PROMPT = (
    "You are an Agent Planning AI for an enterprise PaaS platform.\n"
    "You do NOT write code or unified diffs. You produce a single, review-ready planning "
    "document in Markdown for the current stage only.\n"
    "Rules:\n"
    "1. Output the document body in Korean Markdown. No code diffs, no ``` diff fences.\n"
    "2. Ground every claim in the injected project context (stack, modules, resources).\n"
    "3. Respect the injected '가용 모듈 제약' — never propose resources outside it, and always "
    "route resource access through the central PaaS gateway (A2A/proxy), never external direct calls.\n"
    "4. Build on the confirmed artifacts of previous stages when provided; do not contradict them."
)


def stage_title(stage: PlanStage) -> str:
    return STAGES[stage]["title"]


def stage_repo_path(stage: PlanStage) -> str:
    return f"{ARTIFACT_DIR}/{STAGES[stage]['filename']}"


def stage_prompt(stage: PlanStage) -> str:
    return STAGES[stage]["prompt"]


def prev_stage(stage: PlanStage) -> PlanStage | None:
    idx = STAGE_ORDER.index(stage)
    return STAGE_ORDER[idx - 1] if idx > 0 else None


def build_constraints(db: Session, project: Project) -> dict:
    """외부 빌드의 guardrail이 되는 '가용 모듈 제약' 데이터.

    기존 A2A 카드(모듈→능력·엔드포인트·env_prefix)와 가용 자원 목록에 게이트웨이 경유
    규칙을 더한다. 신규 데이터 소스 없이 기존 서비스 출력을 재사용한다.
    """
    return {
        "project": project.name,
        "rules": [
            "허용된 내부 모듈/자원만 사용한다 — 아래 목록 밖의 자원은 사용 금지.",
            "외부 LLM API·외부 모듈/DB URL을 직접 호출하지 않는다.",
            "모든 자원 접근은 중앙 PaaS 게이트웨이를 경유한다 "
            "(LLM: /paas/api/v1/proxy/llm, 모듈: /paas/api/v1/proxy/modules/{module_name}, "
            "A2A: /paas/api/v1/a2a/agents/{agent_name}/task).",
        ],
        "bound_agents": a2a_service.list_project_a2a_cards(db, project),
        "available_resources": modules_service.available_resources(db, project),
    }


def render_constraints_doc(constraints: dict) -> str:
    """가용 모듈 제약을 마크다운 문서로 정형화(솔루션 구성 단계 컨텍스트·외부 빌드 guardrail용)."""
    lines = [
        f"# 가용 모듈 제약 (프로젝트: {constraints['project']})",
        "",
        "## 규칙 (외부 빌드 제약)",
    ]
    lines += [f"- {r}" for r in constraints["rules"]]
    lines += ["", "## 바인딩된 내부 모듈(A2A 능력)"]
    agents = constraints.get("bound_agents") or []
    if agents:
        lines += ["", "```json", json.dumps(agents, indent=2, ensure_ascii=False), "```"]
    else:
        lines.append("- (바인딩된 모듈 없음)")
    lines += ["", "## 가용 자원 목록"]
    resources = constraints.get("available_resources") or []
    if resources:
        lines += ["", "```json", json.dumps(resources, indent=2, ensure_ascii=False), "```"]
    else:
        lines.append("- (가용 자원 없음)")
    return "\n".join(lines) + "\n"
