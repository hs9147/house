"""에이전트 기획(Agent Planning) — 단계별 대화(문서 초안)·확정(Gitea 커밋)·제약·모니터링·MCP 서버.

에이전트 빌더(app/api/llm.py)의 채팅 파이프라인을 재사용하되, 출력은 코드 diff가 아니라
단계 산출물 문서다. 확정 시 산출물을 프로젝트 Gitea 리포에 커밋하고(services/workspace),
DB(PlanArtifact)에는 위치·커밋·확정 포인터만 남긴다. 빌드는 외부 개발도구에서 수행하며,
플랫폼은 가용 모듈 제약(guardrail)과 MCP 서버·모니터링만 제공한다.
"""
import asyncio
import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import (
    ApiKey,
    AuditEvent,
    BuildTask,
    BuildTaskStatus,
    ChatMessage,
    ChatSession,
    LlmProvider,
    PlanArtifact,
    PlanStage,
    Project,
)
from ..schemas import (
    BuildTaskOut,
    BuildTaskSyncOut,
    BuildTaskUpdate,
    ComplianceOut,
    PlanArtifactContentOut,
    PlanArtifactOut,
    PlanChatMessageOut,
    PlanConfirmIn,
    PlanMergeOut,
    PlanMessageIn,
    PlanMessageReply,
    PlanSessionCreate,
    PlanSessionOut,
    PlanSessionSummary,
)
from ..security import can_view_git_url, require_api_key, viewer_org_ids
from ..services import a2a as a2a_service
from ..services import codemap as codemap_service
from ..services import compliance as compliance_service
from ..services import llm as llm_service
from ..services import mcp_server
from ..services import modules as modules_service
from ..services import planning as planning_service
from ..services import workspace
from ..services.build import checkout

router = APIRouter(tags=["planning"])


def _parse_stage(stage: str) -> PlanStage:
    try:
        return PlanStage(stage)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"unknown plan stage: {stage}")


def _artifacts_out(db: Session, session_id: int) -> list[PlanArtifactOut]:
    """모든 단계를 순서대로 반환하되, 아직 없는 단계는 미확정 기본값으로 채운다(진행단계 표시용)."""
    rows = db.execute(
        select(PlanArtifact).where(PlanArtifact.session_id == session_id)
    ).scalars().all()
    by_stage = {a.stage: a for a in rows}
    out: list[PlanArtifactOut] = []
    for stage in planning_service.STAGE_ORDER:
        a = by_stage.get(stage)
        out.append(PlanArtifactOut(
            stage=stage.value,
            title=planning_service.stage_title(stage),
            repo_path=a.repo_path if a else planning_service.stage_repo_path(stage),
            commit_sha=a.commit_sha if a else None,
            confirmed=bool(a and a.confirmed),
            default_request=planning_service.stage_request(stage),
        ))
    return out


def _session_out(db: Session, session: ChatSession) -> PlanSessionOut:
    project = db.get(Project, session.project_id)
    provider = db.get(LlmProvider, session.provider_id)
    return PlanSessionOut(
        id=session.id,
        branch=session.branch,
        provider=provider.name if provider else "",
        project_id=session.project_id,
        project_name=project.name if project else "",
        artifacts=_artifacts_out(db, session.id),
    )


@router.post("/plan/sessions", response_model=PlanSessionOut, status_code=201)
def create_plan_session(
    body: PlanSessionCreate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, body.project_id)
    provider = db.get(LlmProvider, body.provider_id)
    if project is None or provider is None:
        raise HTTPException(status_code=404, detail="project or provider not found")
    llm_service.require_provider_access(provider, project, key)
    session = ChatSession(project_id=project.id, provider_id=provider.id, branch="")
    db.add(session)
    db.commit()
    # 세션마다 고유한 작업 브랜치를 만든다. id만 쓰면 세션을 지우고 다시 열었을 때
    # 원격에 남은 동명 브랜치와 얽히므로 hex 접미사로 갈라 둔다.
    session.branch = body.branch or f"paas/plan-{session.id}-{secrets.token_hex(4)}"
    db.commit()
    audit.record(db, key.name, "plan.session.create", project.name,
                 {"provider": provider.name, "branch": session.branch})
    return _session_out(db, session)


@router.get("/plan/sessions", response_model=list[PlanSessionSummary])
def list_plan_sessions(
    project_id: int | None = None,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """기획 세션 이력 — 재개·삭제 대상을 고르기 위한 목록(최근 순)."""
    stmt = select(ChatSession).order_by(ChatSession.id.desc())
    if project_id is not None:
        stmt = stmt.where(ChatSession.project_id == project_id)
    out: list[PlanSessionSummary] = []
    for session in db.execute(stmt).scalars().all():
        project = db.get(Project, session.project_id)
        provider = db.get(LlmProvider, session.provider_id)
        confirmed = [
            s.value for s in planning_service.STAGE_ORDER
            if _is_confirmed(db, session.id, s)
        ]
        task_count = len(db.execute(
            select(BuildTask.id).where(BuildTask.session_id == session.id)
        ).scalars().all())
        out.append(PlanSessionSummary(
            id=session.id,
            project_id=session.project_id,
            project_name=project.name if project else "",
            provider=provider.name if provider else "",
            branch=session.branch,
            confirmed_stages=confirmed,
            task_count=task_count,
            created_at=session.created_at,
        ))
    return out


@router.get("/plan/sessions/{session_id}/messages", response_model=list[PlanChatMessageOut])
def list_plan_messages(
    session_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """세션 재개용 대화 이력 — 어디까지 이야기했는지 그대로 복원한다."""
    if db.get(ChatSession, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    rows = db.execute(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
    ).scalars().all()
    return [PlanChatMessageOut(role=m.role, content=m.content, created_at=m.created_at)
            for m in rows]


@router.delete("/plan/sessions/{session_id}", status_code=204)
def delete_plan_session(
    session_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """기획 세션과 그 대화·산출물 포인터·작업 지시, 그리고 작업 브랜치를 지운다.

    기본 브랜치로 머지된 산출물 문서와 감사 로그는 건드리지 않는다 — 리포에 남은
    기록은 세션과 별개이고, 세션을 지웠다고 사라져서는 안 된다. 브랜치 삭제는 베스트
    에포트라 실패해도 세션 삭제는 성공 처리한다.
    """
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    project = db.get(Project, session.project_id)
    branch = session.branch
    for model in (ChatMessage, PlanArtifact, BuildTask):
        db.execute(sa_delete(model).where(model.session_id == session_id))
    db.delete(session)
    db.commit()

    branch_deleted = False
    if project is not None:
        try:
            branch_deleted = workspace.delete_branch(project, branch)
        except Exception:  # noqa: BLE001 — 브랜치 정리 실패가 세션 삭제를 되돌리지 않는다
            branch_deleted = False
    audit.record(db, key.name, "plan.session.delete",
                 project.name if project else str(session.project_id),
                 {"session_id": session_id, "branch": branch, "branch_deleted": branch_deleted})


@router.get("/plan/sessions/{session_id}", response_model=PlanSessionOut)
def get_plan_session(
    session_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _session_out(db, session)


@router.post("/plan/sessions/{session_id}/stages/{stage}/messages", response_model=PlanMessageReply)
async def post_plan_message(
    session_id: int,
    stage: str,
    body: PlanMessageIn,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    plan_stage = _parse_stage(stage)
    if plan_stage == PlanStage.tasks:
        # 5단계 문서는 대화로 쓰지 않는다 — 작업 지시 목록을 렌더한 것이 산출물이다.
        # 대화로 따로 쓰게 두면 문서와 MCP list_tasks가 어긋난다.
        raise HTTPException(
            status_code=409,
            detail="작업 지시 단계는 '작업 지시 생성'으로 산출물을 만듭니다.",
        )
    project = db.get(Project, session.project_id)
    provider = db.get(LlmProvider, session.provider_id)
    llm_service.require_provider_access(provider, project, key)

    # 앞 단계 확정을 전제로 한다(순차 진행 강제)
    prev = planning_service.prev_stage(plan_stage)
    if prev is not None and not _is_confirmed(db, session_id, prev):
        raise HTTPException(
            status_code=409,
            detail=f"앞 단계('{planning_service.stage_title(prev)}')를 먼저 확정하세요.",
        )

    # 역할 → 기획·구현 원칙(플랫폼 표준 문서) → 이번 단계 지시 순으로 쌓는다.
    system_prompt = "\n\n".join(filter(None, [
        planning_service.PLANNING_SYSTEM_PROMPT,
        llm_service.agent_principles_prompt(),
        planning_service.stage_prompt(plan_stage),
    ]))
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    context_parts = [f"Project: {project.name} (type={project.type.value})"]
    workdir = workspace.workdir_for(project)
    if workdir.exists():
        context_parts.append(
            "=== PROJECT STACK & DEPENDENCIES ===\n"
            + json.dumps(workspace.detect_project_stack_and_deps(workdir), indent=2, ensure_ascii=False)
        )

    # 가용 모듈 제약(guardrail) — 모든 단계에 주입, 솔루션 구성 단계의 핵심 제약
    constraints = planning_service.build_constraints(db, project)
    context_parts.append("=== 가용 모듈 제약 (사용 가능 자원·게이트웨이 규칙) ===\n"
                         + planning_service.render_constraints_doc(constraints))

    # 앞 단계에서 확정된 산출물 본문을 순서대로 주입 — 각 단계는 이전 단계 문서를 근거로 쓴다.
    # 세션 브랜치에 커밋된 본문을 읽으므로 워킹카피가 어떤 브랜치에 있든 동일하게 참조된다.
    for idx, s in enumerate(planning_service.prev_stages(plan_stage), start=1):
        if not _is_confirmed(db, session_id, s):
            continue
        path = planning_service.stage_repo_path(s)
        # 세션 브랜치 → 원격 추적 → 기본 브랜치 → 워킹카피 순으로 찾는다(_artifact_content).
        # 워킹카피가 다시 clone돼 로컬 세션 브랜치가 없어도 앞 단계 문서를 놓치지 않는다.
        content = _artifact_content(db, project, session, s)
        if content:
            if body.compact:
                content = planning_service.document_outline(
                    content, planning_service.COMPACT_OUTLINE_HEADS)
            context_parts.append(
                f"=== 이전 단계 확정 산출물 {idx}. {planning_service.stage_title(s)} ({path}) ===\n{content}"
            )

    # 지금 편집 중인 산출물 — 있으면 새로 쓰지 않고 이것을 고치게 한다(수정 요청 문맥).
    # 편집기가 비어 있으면 확정본이나 리포에 이미 있는 이 단계 문서를 대신 싣는다.
    current = body.draft.strip() or (_artifact_content(db, project, session, plan_stage) or "")
    if current and body.compact:
        current = planning_service.document_outline(
            current, planning_service.COMPACT_OUTLINE_HEADS)
    if current:
        context_parts.append(
            f"=== 현재 산출물 (수정 대상: {planning_service.stage_title(plan_stage)}) ===\n{current}"
        )

    # git 파일 목록은 기본 참조, 내용 확인이 필요한 파일만 골라 본문을 덧붙인다.
    context_files: list[str] = []
    if workdir.exists():
        tree = workspace.file_tree(workdir, limit=(
            planning_service.COMPACT_TREE_FILES if body.compact
            else planning_service.MAX_TREE_FILES))
        if tree:
            context_parts.append("=== GIT 파일 목록 (리포 추적 파일) ===\n" + "\n".join(tree))
        # 압축 모드에서는 코드 개요·파일 본문을 싣지 않는다 — 컨텍스트에서 가장 큰 덩어리다.
        if not body.compact:
            try:
                outline = codemap_service.render_outline(codemap_service.build_code_map(workdir))
                context_parts.append("=== CODE STRUCTURE (OUTLINE) ===\n" + outline)
            except Exception:  # noqa: BLE001
                pass
            selected = await asyncio.to_thread(
                planning_service.select_context_files, provider, db, tree, body.content,
            )
            for path, content in workspace.read_context_files(workdir, selected).items():
                context_parts.append(f"--- {path} ---\n{content}")
                context_files.append(path)

    messages.append({"role": "system", "content": "\n\n".join(context_parts)})

    history = list(db.execute(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
    ).scalars())
    if body.compact:
        history = history[-planning_service.COMPACT_HISTORY_MESSAGES:]
    messages.extend({"role": m.role, "content": m.content} for m in history)
    messages.append({"role": "user", "content": body.content})

    # 솔루션 구성 단계에서만 도구를 준다 — 쓰기로 결정한 모듈을 문서와 같은 자리에서
    # 프로젝트에 붙이고(bind_module), 바인딩된 MCP 서버의 도구로 실제 규격을 확인한다.
    bound_modules: list[str] = []
    tools: list[dict] = []
    tool_executor = None
    if plan_stage == PlanStage.solution:
        tools, tool_executor = await asyncio.to_thread(
            planning_service.solution_tools,
            db, project, constraints.get("available_resources") or [], key.name, bound_modules,
        )

    try:
        reply = await asyncio.to_thread(
            llm_service.chat_completion, provider, messages, db, tools or None, tool_executor,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"llm call failed: {e}")

    if not reply.strip():
        # 빈 응답은 대개 컨텍스트가 모델 한도를 넘었다는 신호다. 아직 압축하지 않았다면
        # 413으로 알려 콘솔이 "압축해서 다시 시도할까요?"를 묻게 한다.
        if not body.compact:
            raise HTTPException(
                status_code=413,
                detail=(
                    "컨텍스트가 모델의 한도를 넘은 것 같습니다(빈 응답). "
                    "앞 단계 문서를 개요로 줄이고 코드 컨텍스트를 빼면 다시 시도할 수 있습니다."
                ),
            )
        raise HTTPException(
            status_code=502,
            detail=(
                "컨텍스트를 압축했는데도 LLM이 빈 응답을 반환했습니다 — 요청을 더 좁게 "
                "나누거나 더 큰 컨텍스트를 지원하는 프로바이더로 다시 시도하세요."
            ),
        )

    used_modules: list[str] = []
    reply_upper = reply.upper()
    for res_item in constraints.get("available_resources") or []:
        r_name = str(res_item.get("name", ""))
        if r_name and (r_name in reply or r_name.upper().replace("-", "_") in reply_upper):
            used_modules.append(r_name)

    # 대화에는 요약만, 산출물 본문은 편집기로 — 대화 이력에도 요약만 쌓아 컨텍스트를
    # 부풀리지 않는다(수정 대상 문서는 매 요청 '현재 산출물'로 따로 실린다).
    summary, document = planning_service.split_reply(reply)
    db.add(ChatMessage(session_id=session_id, role="user", content=body.content))
    db.add(ChatMessage(session_id=session_id, role="assistant", content=summary))
    db.commit()
    return PlanMessageReply(summary=summary, document=document, used_modules=used_modules,
                            context_files=context_files, bound_modules=bound_modules,
                            compacted=body.compact)


@router.post("/plan/sessions/{session_id}/stages/{stage}/confirm", response_model=PlanArtifactOut)
async def confirm_plan_stage(
    session_id: int,
    stage: str,
    body: PlanConfirmIn,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    plan_stage = _parse_stage(stage)
    project = db.get(Project, session.project_id)
    if not body.content.strip():
        raise HTTPException(status_code=422, detail="확정할 산출물 본문이 비어 있습니다.")

    prev = planning_service.prev_stage(plan_stage)
    if prev is not None and not _is_confirmed(db, session_id, prev):
        raise HTTPException(
            status_code=409,
            detail=f"앞 단계('{planning_service.stage_title(prev)}')를 먼저 확정하세요.",
        )

    # 리포에 이미 다른 내용의 같은 문서가 있으면(다른 세션·외부 도구가 남긴 것) 확인 없이
    # 덮어쓰지 않는다. 이 세션에서 이미 확정한 문서를 고치는 것은 정상 경로라 묻지 않는다.
    if not body.overwrite and not _is_confirmed(db, session_id, plan_stage):
        existing = _artifact_content(db, project, session, plan_stage)
        if existing and existing.strip() != body.content.strip():
            raise HTTPException(
                status_code=412,
                detail=(
                    f"리포에 이미 '{planning_service.stage_repo_path(plan_stage)}' 문서가 있습니다. "
                    "덮어쓰려면 확인이 필요합니다."
                ),
            )

    repo_path = planning_service.stage_repo_path(plan_stage)
    message = f"plan({plan_stage.value}): {planning_service.stage_title(plan_stage)} 확정 (session #{session_id})"
    try:
        sha = await asyncio.to_thread(
            workspace.write_and_commit, project, session.branch, repo_path, body.content, message,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Gitea 커밋 실패: {e}")

    # 커밋 후 git 상태에 따라 PR 생성·머지를 자동 수행(작업 브랜치일 때만).
    git_result = await asyncio.to_thread(
        planning_service.auto_pull_request, project, session.branch, message,
        f"에이전트 기획 산출물 확정: {repo_path}",
    )

    artifact = db.execute(
        select(PlanArtifact).where(
            PlanArtifact.session_id == session_id, PlanArtifact.stage == plan_stage
        )
    ).scalar_one_or_none()
    if artifact is None:
        artifact = PlanArtifact(session_id=session_id, stage=plan_stage, repo_path=repo_path)
        db.add(artifact)
    artifact.repo_path = repo_path
    artifact.commit_sha = sha
    artifact.confirmed = True
    db.commit()
    audit.record(db, key.name, "plan.stage.confirm", project.name,
                 {"stage": plan_stage.value, "sha": sha, "branch": session.branch,
                  "git_action": git_result["action"]})
    return PlanArtifactOut(
        stage=plan_stage.value, title=planning_service.stage_title(plan_stage),
        repo_path=repo_path, commit_sha=sha, confirmed=True,
        default_request=planning_service.stage_request(plan_stage),
        git_action=git_result["action"],
        git_detail=git_result.get("detail"),
        pull_request_url=git_result.get("url"),
    )


@router.post("/plan/sessions/{session_id}/merge", response_model=PlanMergeOut)
async def merge_plan_session(
    session_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """세션 마무리 — 작업 브랜치를 기본 브랜치로 반영한다.

    단계별 확정도 매번 PR·머지를 시도하지만(auto_pull_request), 충돌 등으로 열린 채
    남은 PR이 있을 수 있다. 작업 지시까지 끝낸 뒤 여기서 한 번 더 마무리한다.
    """
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    project = db.get(Project, session.project_id)
    confirmed = [s for s in planning_service.STAGE_ORDER if _is_confirmed(db, session_id, s)]
    if not confirmed:
        raise HTTPException(status_code=409, detail="확정된 산출물이 없습니다.")

    result = await asyncio.to_thread(
        planning_service.auto_pull_request, project, session.branch,
        f"plan: 기획 산출물 반영 (session #{session_id})",
        "에이전트 기획 세션 마무리 — 확정 산출물과 작업 지시를 기본 브랜치로 반영합니다.",
    )
    audit.record(db, key.name, "plan.session.merge", project.name,
                 {"session_id": session_id, "branch": session.branch, "action": result["action"]})
    return PlanMergeOut(
        branch=session.branch,
        action=result["action"],
        detail=result.get("detail"),
        pull_request_url=result.get("url"),
    )


def _artifact_content(db: Session, project: Project, session: ChatSession,
                      stage: PlanStage) -> str | None:
    """단계 산출물 본문 — 세션 확정본이 없으면 리포의 표준 경로 문서를 그대로 쓴다.

    이 세션에서 확정한 적이 없어도 리포에 docs/agent-planning/*.md가 이미 있으면
    (다른 세션·외부 개발도구가 남긴 문서) 그것이 이 단계의 현재 산출물이다.
    세션 브랜치 → 기본 브랜치 → 워킹카피 순으로 찾는다.
    """
    workdir = workspace.workdir_for(project)
    if not workdir.exists():
        return None
    artifact = db.execute(
        select(PlanArtifact).where(
            PlanArtifact.session_id == session.id, PlanArtifact.stage == stage
        )
    ).scalar_one_or_none()
    path = artifact.repo_path if artifact else planning_service.stage_repo_path(stage)
    for ref in (session.branch, f"origin/{session.branch}", project.branch,
                f"origin/{project.branch}"):
        content = workspace.read_file_at_ref(workdir, ref, path)
        if content:
            return content
    return workspace.read_context_files(workdir, [path]).get(path)


def _tasks_document(db: Session, session_id: int) -> str:
    """현재 작업 지시를 5단계 산출물 문서로 렌더한다(아직 커밋 전 미리보기)."""
    return planning_service.render_tasks_doc(
        [_task_out(t).model_dump() for t in _session_tasks(db, session_id)])


@router.get("/plan/sessions/{session_id}/stages/{stage}/artifact",
            response_model=PlanArtifactContentOut)
def get_plan_artifact_content(
    session_id: int,
    stage: str,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """단계 산출물 본문 — 세션을 재개하거나 단계를 열면 편집기를 이 내용으로 채운다.

    리포를 먼저 최신화한다. 아직 한 번도 체크아웃하지 않은 프로젝트라도 리포에 이미
    있는 기획 문서를 보여줄 수 있어야 하기 때문이다(실패해도 있는 것으로 진행).
    """
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    plan_stage = _parse_stage(stage)
    project = db.get(Project, session.project_id)
    try:
        checkout(project)
    except Exception:  # noqa: BLE001 — 원격이 없어도 워킹카피가 있으면 읽는다
        pass
    artifact = db.execute(
        select(PlanArtifact).where(
            PlanArtifact.session_id == session_id, PlanArtifact.stage == plan_stage
        )
    ).scalar_one_or_none()
    # 5단계 산출물의 원천은 작업 지시 목록이다 — 목록이 있으면 리포 문서보다 그것을 보여
    # 준다. '작업 지시 생성'을 다시 돌리면 편집기 내용도 바로 따라와야 하기 때문이다.
    tasks_rendered = (plan_stage == PlanStage.tasks and bool(_session_tasks(db, session_id)))
    content = (_tasks_document(db, session_id) if tasks_rendered
               else _artifact_content(db, project, session, plan_stage) or "")
    if tasks_rendered and not (artifact and artifact.confirmed):
        source = "tasks"  # 작업 지시 목록에서 렌더한 문서(아직 확정 전)
    elif artifact is not None and artifact.confirmed:
        source = "session"  # 이 세션에서 확정한 산출물
    elif content:
        source = "repo"  # 리포에 이미 있던 문서
    else:
        source = ""
    return PlanArtifactContentOut(
        stage=plan_stage.value,
        repo_path=artifact.repo_path if artifact else planning_service.stage_repo_path(plan_stage),
        content=content,
        confirmed=bool(artifact and artifact.confirmed),
        source=source,
    )


@router.get("/plan/sessions/{session_id}/repo")
def get_plan_repo(
    session_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """개발도구(VSCode·Claude·Antigravity) 연동용 리포 정보.

    clone_url은 관리자, 또는 그 프로젝트 조직 소속(전역 프로젝트는 누구나) 사용자에게만
    노출한다 — 기획을 진행하는 사용자가 정작 리포를 못 열면 화면의 존재 의미가 없다.
    """
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    project = db.get(Project, session.project_id)
    return {
        "clone_url": project.git_url if can_view_git_url(project, key, viewer_org_ids(db, key)) else None,
        "branch": session.branch,
        "artifact_dir": planning_service.ARTIFACT_DIR,
        "artifacts": [a.model_dump() for a in _artifacts_out(db, session_id)],
    }


# --- 작업 지시(work order) — 확정 산출물을 외주 빌더가 집어갈 단위로 쪼개 추적한다 ---


def _task_out(t: BuildTask) -> BuildTaskOut:
    return BuildTaskOut(
        id=t.id, title=t.title, detail=t.detail, verify=t.verify,
        status=t.status.value, note=t.note, commit_sha=t.commit_sha,
    )


def _session_tasks(db: Session, session_id: int) -> list[BuildTask]:
    return db.execute(
        select(BuildTask).where(BuildTask.session_id == session_id).order_by(BuildTask.id)
    ).scalars().all()


@router.post("/plan/sessions/{session_id}/tasks/generate", response_model=list[BuildTaskOut])
async def generate_build_tasks(
    session_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """확정된 기획 산출물에서 외주 빌드 작업 지시를 만든다.

    아직 아무도 착수하지 않았을 때만 다시 만든다 — 진행 중인 지시를 덮어쓰면 외부
    빌더가 보고 있던 작업이 말없이 사라진다.
    """
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    project = db.get(Project, session.project_id)
    provider = db.get(LlmProvider, session.provider_id)
    llm_service.require_provider_access(provider, project, key)

    existing = _session_tasks(db, session_id)
    if any(t.status != BuildTaskStatus.pending for t in existing):
        raise HTTPException(status_code=409, detail="이미 진행 중인 작업 지시가 있습니다.")

    # 5단계 자신은 근거가 아니다 — 작업 지시에서 만든 문서를 다시 넣으면 순환이 된다.
    confirmed = [s for s in planning_service.STAGE_ORDER
                 if s != PlanStage.tasks and _is_confirmed(db, session_id, s)]
    if not confirmed:
        raise HTTPException(status_code=409, detail="확정된 산출물이 없습니다. 단계를 먼저 확정하세요.")

    workdir = workspace.workdir_for(project)
    documents = []
    for stage in confirmed:
        path = planning_service.stage_repo_path(stage)
        content = workspace.read_file_at_ref(workdir, session.branch, path) if workdir.exists() else None
        if content:
            documents.append(f"=== {planning_service.stage_title(stage)} ({path}) ===\n{content}")
    if not documents:
        raise HTTPException(status_code=409, detail="확정 산출물 본문을 리포에서 읽지 못했습니다.")

    constraints = planning_service.build_constraints(db, project)
    items = await asyncio.to_thread(
        planning_service.decompose_tasks, provider, db, "\n\n".join(documents),
        planning_service.render_constraints_doc(constraints),
    )
    if not items:
        raise HTTPException(status_code=502, detail="작업 지시 생성에 실패했습니다.")

    for t in existing:
        db.delete(t)
    for item in items:
        db.add(BuildTask(
            project_id=project.id, session_id=session_id,
            title=item["title"], detail=item["detail"], verify=item["verify"],
        ))
    db.commit()
    audit.record(db, key.name, "plan.tasks.generate", project.name, {"count": len(items)})
    return [_task_out(t) for t in _session_tasks(db, session_id)]


@router.get("/plan/sessions/{session_id}/tasks", response_model=list[BuildTaskOut])
def list_build_tasks(
    session_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    if db.get(ChatSession, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return [_task_out(t) for t in _session_tasks(db, session_id)]


@router.post("/plan/sessions/{session_id}/tasks/sync", response_model=BuildTaskSyncOut)
async def sync_build_tasks(
    session_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """작업 지시 진행 현황을 기본 브랜치 기준으로 갱신한다.

    빌더의 자기 보고(update_task·submit_build_result)는 신호일 뿐이다 — 실제로 반영된
    것은 기본 브랜치에 올라간 커밋뿐이라, 보고된 커밋이 거기서 도달 가능할 때만 완료로
    둔다. 아직 PR이 머지되지 않았으면 완료를 진행 중으로 되돌린다.

    빌더가 남긴 note(구현 요약·질의)는 건드리지 않는다 — 상태를 고치자고 근거 기록을
    지우면 왜 그 상태인지 알 수 없게 된다. 질의로 막힌(blocked) 작업도 그대로 둔다.
    """
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    project = db.get(Project, session.project_id)
    try:
        await asyncio.to_thread(checkout, project)  # 기본 브랜치 최신화(실패해도 있는 것으로 판정)
    except Exception:  # noqa: BLE001
        pass

    tasks = _session_tasks(db, session_id)
    workdir = workspace.workdir_for(project)
    base_ref = workspace.default_branch_ref(workdir, project.branch) if workdir.exists() else None
    if base_ref is None:
        return BuildTaskSyncOut(base_ref="", merged=0, pending=0,
                                tasks=[_task_out(t) for t in tasks])

    merged = pending = 0
    changed: list[int] = []
    for task in tasks:
        if not task.commit_sha or task.status == BuildTaskStatus.blocked:
            continue
        if await asyncio.to_thread(workspace.is_merged, workdir, base_ref, task.commit_sha):
            merged += 1
            if task.status != BuildTaskStatus.done:
                task.status = BuildTaskStatus.done
                changed.append(task.id)
        else:
            pending += 1
            if task.status == BuildTaskStatus.done:
                task.status = BuildTaskStatus.in_progress
                changed.append(task.id)
    db.commit()
    audit.record(db, key.name, "plan.tasks.sync", project.name,
                 {"base_ref": base_ref, "merged": merged, "pending": pending,
                  "changed": changed})
    return BuildTaskSyncOut(base_ref=base_ref, merged=merged, pending=pending,
                            tasks=[_task_out(t) for t in tasks])


@router.patch("/plan/tasks/{task_id}", response_model=BuildTaskOut)
def update_build_task(
    task_id: int,
    body: BuildTaskUpdate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    task = db.get(BuildTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    _apply_task_update(db, key.name, task, body.status, body.note, body.commit_sha)
    return _task_out(task)


def _apply_task_update(
    db: Session, actor: str, task: BuildTask,
    status: str | None, note: str | None, commit_sha: str | None,
) -> None:
    """작업 지시 갱신 — 콘솔(PATCH)과 외부 빌더(MCP)가 같은 경로를 쓴다."""
    if status is not None:
        try:
            task.status = BuildTaskStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"unknown status: {status}")
    if note is not None:
        task.note = note[:2000]
    if commit_sha is not None:
        task.commit_sha = commit_sha[:40]
    db.commit()
    project = db.get(Project, task.project_id)
    audit.record(db, actor, "plan.task.update", project.name if project else str(task.project_id),
                 {"task_id": task.id, "status": task.status.value})


@router.get("/plan/projects/{project_id}/compliance", response_model=ComplianceOut)
def get_plan_compliance(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """외주 빌드 결과 검증 — LLM·모듈 사용이 제약을 지켰는지 보고, 위반 시 수정 지시 프롬프트를 만든다."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return _compliance_out(db, project)


def _compliance_out(db: Session, project: Project) -> ComplianceOut:
    constraints = planning_service.build_constraints(db, project)
    findings = compliance_service.scan(workspace.workdir_for(project), constraints)
    return ComplianceOut(
        project=project.name,
        findings=findings,
        summary=compliance_service.summarize(findings),
        builder_prompt=compliance_service.builder_prompt(
            findings, planning_service.render_constraints_doc(constraints)
        ),
    )


@router.get("/plan/projects/{project_id}/constraints")
def get_plan_constraints(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """외부 빌드 guardrail이 되는 가용 모듈 제약(데이터 + 마크다운 문서)."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    constraints = planning_service.build_constraints(db, project)
    return {**constraints, "document": planning_service.render_constraints_doc(constraints)}


@router.get("/plan/sessions/{session_id}/build-status")
def get_plan_build_status(
    session_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """외부 빌드 모니터링 — 프로젝트 관련 감사 이벤트(커밋 트리거·모듈 사용·기획 확정)를 집계."""
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    project = db.get(Project, session.project_id)
    events = db.execute(
        select(AuditEvent)
        .where(AuditEvent.target == project.name)
        .order_by(AuditEvent.id.desc())
        .limit(50)
    ).scalars().all()
    return {
        "project": project.name,
        "branch": session.branch,
        "events": [
            {"actor": e.actor, "action": e.action, "detail": e.detail,
             "created_at": e.created_at.isoformat() if e.created_at else None}
            for e in events
        ],
    }


# --- MCP 서버 (외부 빌드 도구 연동) ---
# 플랫폼은 기존에 MCP 클라이언트였으나(services/mcp_client.py), 외부 빌드 도구(MCP
# 클라이언트)가 접속해 제약·모듈을 조회하고 진행을 보고할 수 있도록 여기서 MCP 서버를
# 노출한다. JSON-RPC 2.0 / tools/list · tools/call 최소 구현이며, 노출 데이터는 기존
# A2A 카드·모듈 메타를 매핑한다(신규 데이터 소스 없음).

_MCP_TOOLS = [
    {
        "name": "get_constraints",
        "description": "이 프로젝트의 가용 모듈 제약(외부 빌드 guardrail) 문서를 반환한다.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_available_modules",
        "description": "이 프로젝트에서 사용 가능한 내부 모듈/A2A 능력 목록을 반환한다.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "report_build_progress",
        "description": "외부 빌드 진행 상황을 플랫폼에 보고한다(모니터링에 집계됨).",
        "inputSchema": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    },
    {
        "name": "read_artifact",
        "description": "확정된 기획 산출물 본문을 읽는다(clone 없이). stage: spec|architecture|solution|principles",
        "inputSchema": {
            "type": "object",
            "properties": {"stage": {"type": "string"}},
            "required": ["stage"],
        },
    },
    {
        "name": "list_tasks",
        "description": "이 프로젝트의 빌드 작업 지시(work order) 목록과 상태를 반환한다.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_task",
        "description": "작업 지시 상태를 갱신한다. status: pending|in_progress|done|blocked",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "submit_build_result",
        "description": "구현 결과(커밋 sha·요약)를 제출한다. task_id를 주면 그 작업을 완료 처리한다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "commit_sha": {"type": "string"},
                "summary": {"type": "string"},
                "task_id": {"type": "integer"},
            },
            "required": ["commit_sha"],
        },
    },
    {
        "name": "request_clarification",
        "description": (
            "기획이 모호해 진행할 수 없을 때 질의한다. 질의는 기획 세션 대화에 남아 "
            "다음 초안에 반영되고, task_id를 주면 그 작업은 blocked가 된다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "task_id": {"type": "integer"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "check_compliance",
        "description": (
            "커밋된 코드의 LLM·모듈 사용이 제약을 지켰는지 검사한다. "
            "위반이 있으면 그대로 따를 수정 지시가 함께 온다."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


@router.post("/plan/projects/{project_id}/mcp")
async def plan_mcp_server(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """외부 빌드 도구가 접속하는 MCP 서버 엔드포인트(JSON-RPC 2.0)."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return mcp_server.dispatch(
        await mcp_server.read_payload(request),
        server_name=f"paas-plan-{project.name}",
        tools=_MCP_TOOLS,
        call=lambda name, args: _mcp_call(db, key.name, project, name, args),
    )


def _mcp_call(db: Session, actor: str, project: Project, name: str, args: dict) -> str:
    """도구 하나를 실행해 텍스트를 돌려준다. 실행할 수 없으면 McpToolError."""
    if name == "get_constraints":
        constraints = planning_service.build_constraints(db, project)
        return planning_service.render_constraints_doc(constraints)
    if name == "list_available_modules":
        constraints = planning_service.build_constraints(db, project)
        return json.dumps(constraints["bound_agents"], ensure_ascii=False)
    if name == "report_build_progress":
        note = str(args.get("note", ""))[:1000]
        audit.record(db, actor, "plan.build.progress", project.name, {"note": note})
        return "recorded"
    if name == "read_artifact":
        try:
            stage = PlanStage(str(args.get("stage", "")))
        except ValueError:
            raise mcp_server.McpToolError(f"unknown stage: {args.get('stage')}")
        content = _read_confirmed_artifact(db, project, stage)
        if content is None:
            raise mcp_server.McpToolError(f"확정된 산출물이 없습니다: {args.get('stage')}")
        return content
    if name == "list_tasks":
        tasks = db.execute(
            select(BuildTask).where(BuildTask.project_id == project.id).order_by(BuildTask.id)
        ).scalars().all()
        text = json.dumps(
            [_task_out(t).model_dump() for t in tasks], ensure_ascii=False, indent=2)
        # 최근 push에서 잡힌 위반은 작업 목록을 볼 때 함께 알린다 — 빌더가 폴링하지 않아도 안다.
        warning = _latest_compliance_warning(db, project)
        return f"{warning}\n\n{text}" if warning else text
    if name == "check_compliance":
        result = _compliance_out(db, project)
        audit.record(db, actor, "plan.build.compliance", project.name, {
            "status": "warning" if result.findings else "clean",
            "summary": result.summary,
        })
        if not result.findings:
            return "위반 없음 — 제약을 지켰습니다."
        return result.builder_prompt
    return _mcp_build_report(db, actor, project, name, args)


def _latest_compliance_warning(db: Session, project: Project) -> str:
    """가장 최근 컴플라이언스 검사에서 위반이 남아 있으면 그 요약 한 줄.

    검사 결과는 감사 이벤트가 원천이라 별도 저장소를 두지 않는다 — 다시 검사해 깨끗하면
    최신 이벤트가 clean이 되어 경고도 자연히 사라진다.
    """
    event = db.execute(
        select(AuditEvent)
        .where(AuditEvent.target == project.name,
               AuditEvent.action == "plan.build.compliance")
        .order_by(AuditEvent.id.desc())
    ).scalars().first()
    summary = (event.detail or {}).get("summary") if event else None
    if not summary:
        return ""
    return (
        "⚠️ 최근 커밋에서 제약 위반이 감지됐습니다(머지·배포는 막지 않음): "
        f"{json.dumps(summary, ensure_ascii=False)}\n"
        "check_compliance를 호출해 수정 지시를 받으세요."
    )


def _latest_session(db: Session, project_id: int) -> ChatSession | None:
    return db.execute(
        select(ChatSession).where(ChatSession.project_id == project_id)
        .order_by(ChatSession.id.desc())
    ).scalars().first()


def _read_confirmed_artifact(db: Session, project: Project, stage: PlanStage) -> str | None:
    """확정된 단계 산출물 본문 — 커밋된 세션 브랜치에서 읽는다(가장 최근 확정 우선)."""
    row = db.execute(
        select(PlanArtifact, ChatSession)
        .join(ChatSession, PlanArtifact.session_id == ChatSession.id)
        .where(
            ChatSession.project_id == project.id,
            PlanArtifact.stage == stage,
            PlanArtifact.confirmed.is_(True),
        )
        .order_by(PlanArtifact.id.desc())
    ).first()
    if row is None:
        return None
    artifact, session = row
    workdir = workspace.workdir_for(project)
    if not workdir.exists():
        return None
    for ref in (session.branch, project.branch):
        content = workspace.read_file_at_ref(workdir, ref, artifact.repo_path)
        if content:
            return content
    return workspace.read_context_files(workdir, [artifact.repo_path]).get(artifact.repo_path)


def _mcp_build_report(db: Session, actor: str, project: Project, tool: str, args: dict) -> str:
    """외부 빌더의 쓰기 3종(작업 갱신·결과 제출·질의)을 한 자리에서 처리한다."""
    task = None
    task_id = args.get("task_id")
    if task_id is not None:
        task = db.get(BuildTask, int(task_id))
        if task is None or task.project_id != project.id:
            raise mcp_server.McpToolError(f"task not found: {task_id}")

    if tool == "update_task":
        if task is None:
            raise mcp_server.McpToolError("task_id가 필요합니다.")
        _apply_task_update(db, actor, task, args.get("status"), args.get("note"), None)
        return f"task {task.id} → {task.status.value}"

    if tool == "submit_build_result":
        sha = str(args.get("commit_sha", ""))[:40]
        summary = str(args.get("summary", ""))[:1000]
        audit.record(db, actor, "plan.build.result", project.name,
                     {"commit_sha": sha, "summary": summary})
        if task is not None:
            _apply_task_update(db, actor, task, BuildTaskStatus.done.value, summary, sha)
        return "recorded"

    # request_clarification — 질의를 기획 세션 대화에 남겨 다음 초안에 반영되게 한다.
    question = str(args.get("question", "")).strip()[:2000]
    if not question:
        raise mcp_server.McpToolError("question이 비어 있습니다.")
    session = _latest_session(db, project.id)
    if session is not None:
        db.add(ChatMessage(session_id=session.id, role="user",
                           content=f"[외주 빌더 질의] {question}"))
        db.commit()
    audit.record(db, actor, "plan.build.clarification", project.name, {"question": question})
    if task is not None:
        _apply_task_update(db, actor, task, BuildTaskStatus.blocked.value, question, None)
    return "질의를 기획 세션에 전달했습니다."


def _is_confirmed(db: Session, session_id: int, stage: PlanStage) -> bool:
    a = db.execute(
        select(PlanArtifact).where(
            PlanArtifact.session_id == session_id, PlanArtifact.stage == stage
        )
    ).scalar_one_or_none()
    return bool(a and a.confirmed)
