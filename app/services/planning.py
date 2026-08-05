"""에이전트 기획(Agent Planning) — 단계 정의·스테이지 프롬프트·가용 모듈 제약 문서.

에이전트 빌더가 곧바로 코드 diff를 만들던 것과 달리, 기획은 코딩 전 4단계를 순차
수행하며 각 단계에서 **문서 산출물**을 만든다. 산출물 본문은 프로젝트 Gitea 리포에
커밋되고(services/workspace.write_and_commit), DB(PlanArtifact)에는 포인터만 남는다.

설계 근거: docs/agent-planning/ (기획서·아키텍처·솔루션 구성·개발원칙).
"""
import json
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..models import LlmProvider, Module, ModuleBinding, PlanStage, Project
from . import a2a as a2a_service
from . import gitea as gitea_service
from . import llm as llm_service
from . import mcp_client
from . import modules as modules_service

# 산출물이 커밋되는 리포 내 표준 경로(개발도구가 clone/open으로 그대로 열람).
ARTIFACT_DIR = "docs/agent-planning"

# 단계별 메타: 순서·제목·리포 파일명·문서 작성 지시(스테이지 프롬프트)·기본 생성 요청.
# "request"는 콘솔 입력창에 미리 채워지는 기본값 — 사용자가 아무것도 쓰지 않아도
# 바로 '초안 생성'을 누를 수 있어야 한다.
STAGES: dict[PlanStage, dict[str, str]] = {
    PlanStage.spec: {
        "title": "기획서",
        "filename": "01-기획서.md",
        "prompt": (
            "이번 단계는 '기획서 확정'이다. 요구사항·목적·범위·사용자 시나리오·성공 기준을 "
            "구조화해 확정 가능한 기획서 문서를 작성하라."
        ),
        "request": (
            "이 프로젝트의 리포 구성과 가용 모듈 제약을 참고해 기획서 초안을 작성해줘. "
            "목적·범위·주요 사용자 시나리오·기능 요구사항·비기능 요구사항·성공 기준을 포함해줘."
        ),
    },
    PlanStage.architecture: {
        "title": "아키텍처 설계",
        "filename": "02-아키텍처설계.md",
        "prompt": (
            "이번 단계는 '아키텍처 설계'다. 확정된 기획서를 바탕으로 컴포넌트·데이터 흐름·경계·"
            "비기능 요건을 담은 아키텍처 설계 문서를 작성하라."
        ),
        "request": (
            "확정된 기획서를 근거로 아키텍처 설계 초안을 작성해줘. "
            "컴포넌트 구성·데이터 흐름·인터페이스 경계·저장소 설계·비기능 요건을 포함해줘."
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
        "request": (
            "확정된 기획서·아키텍처 설계와 가용 모듈 제약을 근거로 솔루션 구성 초안을 작성해줘. "
            "사용할 내부 모듈과 게이트웨이 경유 방식, 배포 형상, 외부 연동 대체 방안을 포함해줘."
        ),
    },
    PlanStage.principles: {
        "title": "개발원칙",
        "filename": "04-개발원칙.md",
        "prompt": (
            "이번 단계는 '개발원칙'이다. 구현계획 및 현황관리, 스키마 및 의사결정사항, 배포 및 "
            "사용 가이드를 포함한 개발원칙 문서를 작성하라."
        ),
        "request": (
            "앞 단계 확정 산출물을 근거로 개발원칙 초안을 작성해줘. "
            "구현계획 및 현황관리, 스키마 및 의사결정사항, 배포 및 사용 가이드를 포함해줘."
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


def stage_request(stage: PlanStage) -> str:
    """콘솔 입력창에 미리 채워지는 기본 생성 요청 프롬프트."""
    return STAGES[stage]["request"]


def prev_stage(stage: PlanStage) -> PlanStage | None:
    idx = STAGE_ORDER.index(stage)
    return STAGE_ORDER[idx - 1] if idx > 0 else None


def prev_stages(stage: PlanStage) -> list[PlanStage]:
    """이 단계 앞의 모든 단계(순서대로) — 각 단계는 앞 단계 문서를 참조해 작성한다."""
    return STAGE_ORDER[: STAGE_ORDER.index(stage)]


# git 파일 목록은 항상 기본 참조하되, 내용까지 넣는 파일은 요청당 이만큼으로 제한한다.
MAX_TREE_FILES = 400
AUTO_CONTEXT_FILES = 6

_FILE_SELECT_PROMPT = (
    "You select which repository files must be read to answer a planning request.\n"
    "You are given the repository file list and the user's request.\n"
    "Reply with ONLY a JSON array of file paths copied verbatim from the list "
    "(at most {limit}). Reply with [] when the file list alone is enough."
)


def select_context_files(
    provider: LlmProvider,
    db: Session,
    tree: list[str],
    request: str,
    explicit: list[str] | None = None,
    limit: int = AUTO_CONTEXT_FILES,
) -> list[str]:
    """이번 요청에서 '내용 확인이 필요한' 리포 파일을 고른다.

    사용자가 직접 지정한 파일은 항상 포함하고, 나머지는 파일 목록과 요청을 LLM에 주어
    고르게 한다(요청이 한국어여도 동작해야 하므로 경로 키워드 매칭이 아니라 모델이 고른다).
    선정 실패는 조용히 무시한다 — 파일 목록(기본 참조)만으로도 대화는 성립한다.
    """
    selected = [p for p in (explicit or []) if p.strip()]
    if not tree or not request.strip() or limit <= 0:
        return selected
    messages = [
        {"role": "system", "content": _FILE_SELECT_PROMPT.format(limit=limit)},
        {"role": "user", "content": "\n".join(tree) + f"\n\n=== REQUEST ===\n{request}"},
    ]
    try:
        reply = llm_service.chat_completion(provider, messages, db)
        picked = json.loads(reply[reply.index("["): reply.rindex("]") + 1])
    except Exception:  # noqa: BLE001 — 선정 실패는 대화를 막지 않는다
        return selected
    known = set(tree)
    auto = 0
    for path in picked:
        if isinstance(path, str) and path in known and path not in selected:
            selected.append(path)
            auto += 1
            if auto >= limit:
                break
    return selected


# 작업 지시(work order) 분해 — 확정 산출물을 외주 빌더가 집어갈 단위로 쪼갠다.
MAX_TASKS = 12

_TASK_DECOMPOSE_PROMPT = (
    "You turn confirmed planning documents into a build work order for an EXTERNAL builder "
    "(VSCode/Claude/Antigravity) that will implement the code outside this platform.\n"
    "Reply with ONLY a JSON array of at most {limit} objects:\n"
    '[{{"title": "...", "detail": "...", "verify": "..."}}]\n'
    "- title: 한 줄 작업명\n"
    "- detail: 무엇을 어떻게 구현할지 (근거가 된 산출물 내용에 기반)\n"
    "- verify: 완료 판정 기준 — 실행 가능한 확인 방법(테스트·명령·확인 절차)\n"
    "Write the values in Korean. Ground every task in the given documents. "
    "Never propose resources outside the given constraints, and route all resource access "
    "through the central PaaS gateway."
)


def decompose_tasks(
    provider: LlmProvider,
    db: Session,
    documents: str,
    constraints_doc: str,
    limit: int = MAX_TASKS,
) -> list[dict]:
    """확정 산출물 + 제약에서 작업 지시 목록을 만든다. 형식이 깨지면 빈 목록."""
    messages = [
        {"role": "system", "content": _TASK_DECOMPOSE_PROMPT.format(limit=limit)},
        {"role": "user", "content": f"{documents}\n\n=== 가용 모듈 제약 ===\n{constraints_doc}"},
    ]
    try:
        reply = llm_service.chat_completion(provider, messages, db)
        items = json.loads(reply[reply.index("["): reply.rindex("]") + 1])
    except Exception:  # noqa: BLE001
        return []
    tasks: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        tasks.append({
            "title": title[:255],
            "detail": str(item.get("detail", "")).strip(),
            "verify": str(item.get("verify", "")).strip(),
        })
        if len(tasks) >= limit:
            break
    return tasks


# 솔루션 구성 단계 전용 도구 — 쓰기로 결정한 내부 모듈을 그 자리에서 프로젝트에 바인딩한다.
# 문서로만 "이 모듈을 쓴다"고 적어 두면 배포 시 환경변수가 주입되지 않아 외주 빌더가
# 받을 자원과 문서가 어긋난다. 결정과 바인딩을 같은 단계에서 끝낸다.
BIND_TOOL = "bind_module"


def module_bind_tools(resources: list[dict]) -> list[dict]:
    names = [str(r.get("name", "")) for r in resources if r.get("name")]
    if not names:
        return []
    return [{
        "type": "function",
        "function": {
            "name": BIND_TOOL,
            "description": (
                "솔루션에 사용하기로 결정한 내부 모듈을 이 프로젝트에 바인딩한다. "
                "바인딩하면 배포 시 규약된 환경변수가 자동 주입된다. "
                "문서에 사용한다고 적은 모듈은 반드시 이 도구로 바인딩하라."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "module_name": {"type": "string", "enum": names},
                    "env_prefix": {
                        "type": "string",
                        "description": "주입 환경변수 접두사(대문자·숫자·밑줄, 예: PAY → PAY_URL)",
                    },
                },
                "required": ["module_name", "env_prefix"],
            },
        },
    }]


def solution_tools(db: Session, project: Project, resources: list[dict], actor: str,
                   bound: list[str]) -> tuple[list[dict], Callable[[str, dict], str] | None]:
    """솔루션 구성 단계의 도구 묶음 — 모듈 바인딩 + 바인딩된 MCP 서버가 광고하는 도구.

    MCP 도구는 이번 턴 기준으로 **이미 바인딩된** 서버에서만 모은다. 이번 턴에 새로
    바인딩한 MCP 모듈의 도구는 다음 턴부터 잡힌다 — 도구 목록은 LLM 호출 전에 확정되기
    때문이며, 응답하지 않는 서버는 조용히 빠진다(services/mcp_client).
    """
    tools = module_bind_tools(resources)
    bind_execute = make_bind_executor(db, project, actor, bound) if tools else None

    servers = modules_service.mcp_servers_for_project(db, project)
    mcp_tools, mcp_registry = mcp_client.build_openai_tools(servers) if servers else ([], {})
    tools += mcp_tools
    mcp_execute = mcp_client.make_tool_executor(mcp_registry) if mcp_tools else None

    if not tools:
        return [], None

    def execute(fn_name: str, arguments: dict) -> str:
        if fn_name == BIND_TOOL and bind_execute is not None:
            return bind_execute(fn_name, arguments)
        if mcp_execute is not None:
            return mcp_execute(fn_name, arguments)
        return f"unknown tool: {fn_name}"

    return tools, execute


def make_bind_executor(db: Session, project: Project, actor: str, bound: list[str]):
    """도구 호출을 실제 바인딩으로 처리한다. 성공한 모듈명은 bound에 쌓인다.

    실패는 예외로 올리지 않고 모델이 읽을 문장으로 돌려준다 — 도구 하나가 실패해도
    문서 작성 자체는 이어져야 한다(services/mcp_client와 같은 규약).
    """
    def execute(fn_name: str, arguments: dict) -> str:
        if fn_name != BIND_TOOL:
            return f"unknown tool: {fn_name}"
        name = str(arguments.get("module_name", "")).strip()
        prefix = str(arguments.get("env_prefix", "")).strip().upper()
        if not name or not prefix:
            return "module_name과 env_prefix가 모두 필요합니다."
        module = db.execute(select(Module).where(Module.name == name)).scalar_one_or_none()
        if module is None:
            return f"모듈을 찾을 수 없습니다: {name}"
        allowed = {r["name"] for r in modules_service.available_resources(db, project)}
        if name not in allowed:
            return f"이 프로젝트에서 사용할 수 없는 모듈입니다: {name}"

        existing = db.execute(
            select(ModuleBinding).where(ModuleBinding.project_id == project.id)
        ).scalars().all()
        for b in existing:
            if b.module_id == module.id:
                return f"이미 바인딩된 모듈입니다: {name} (prefix={b.env_prefix})"
            if b.env_prefix == prefix:
                return f"이 프로젝트에서 이미 쓰는 접두사입니다: {prefix}"

        db.add(ModuleBinding(project_id=project.id, module_id=module.id, env_prefix=prefix))
        db.commit()
        audit.record(db, actor, "module.bind", project.name,
                     {"module": name, "prefix": prefix, "via": "plan.solution"})
        bound.append(name)
        keys = sorted(modules_service.binding_env(module, prefix, db=db).keys())
        return f"바인딩 완료: {name} (prefix={prefix}) — 주입될 환경변수: {', '.join(keys)}"

    return execute


def auto_pull_request(project: Project, branch: str, title: str, body: str = "") -> dict:
    """커밋 후 git 상태에 따라 PR 생성·머지를 자동 수행하고 그 결과를 반환한다.

    - 작업 브랜치가 곧 기본 브랜치면 커밋 자체가 반영이므로 PR을 만들지 않는다.
    - 그 외에는 PR을 만들고(이미 있으면 재사용) 머지 가능하면 머지한다.
    - 충돌 등으로 머지가 안 되면 PR을 열어둔 채 사람이 처리하도록 보고한다.
    커밋은 이미 성공했으므로 Gitea 연동 실패로 확정을 되돌리지는 않는다(skipped 보고).
    """
    if branch == project.branch:
        return {"action": "committed", "detail": f"기본 브랜치({project.branch})에 직접 커밋"}
    slug = gitea_service.repo_slug(project.git_url)
    if slug is None:
        return {"action": "skipped", "detail": "사내 Gitea 리포가 아니어서 PR을 만들지 않았습니다."}
    owner, repo = slug
    try:
        pr = gitea_service.ensure_pull_request(owner, repo, branch, project.branch, title, body)
    except gitea_service.GiteaError as e:
        return {"action": "skipped", "detail": str(e)}

    number = pr.get("number")
    result = {"action": "pr_opened", "url": pr.get("html_url")}
    if number is None:
        result["detail"] = "PR 번호를 확인할 수 없어 자동 머지를 건너뜁니다."
        return result
    if pr.get("mergeable") is False:
        result["detail"] = "충돌로 자동 머지할 수 없어 PR을 열어 두었습니다."
        return result
    try:
        merged = gitea_service.merge_pull_request(owner, repo, number, title=title)
    except gitea_service.GiteaError as e:
        result["detail"] = str(e)
        return result
    if merged:
        result["action"] = "merged"
    else:
        result["detail"] = "자동 머지가 거부되어 PR을 열어 두었습니다."
    return result


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
