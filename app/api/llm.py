"""LLM 프로바이더 · 대화식 편집(diff 제안/승인) · 코드 리뷰."""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import (
    ApiKey,
    ChangeStatus,
    ChatMessage,
    ChatSession,
    LlmProvider,
    LlmProviderKind,
    Project,
    ProposedChange,
)
from ..schemas import (
    ChatMessageIn,
    ChatReply,
    ChatSessionCreate,
    LlmProviderCreate,
    LlmProviderOut,
    ReviewRequest,
)
from ..security import encrypt_value, require_admin, require_api_key
from ..services import codemap as codemap_service
from ..services import llm as llm_service
from ..services import mcp_client
from ..services import modules as modules_service
from ..services import workspace
from ..services.build import BuildError, checkout

router = APIRouter(tags=["llm"])


def _require_admin_for_external(provider: LlmProvider, key: ApiKey) -> None:
    """외부 LLM 프로바이더 사용은 admin 키만 허용한다.

    일반 키가 임의 project_id + 임의 provider_id를 조합해 아무 프로젝트의
    소스를 외부로 보낼 수 있는 경로를 막는다. internal 프로바이더(project://)는
    사내망을 벗어나지 않으므로 일반 키에도 열어둔다.
    """
    if provider.kind == LlmProviderKind.external and not key.is_admin:
        raise HTTPException(
            status_code=403,
            detail="외부 LLM 프로바이더는 admin 키만 사용할 수 있습니다.",
        )


def _provider_out(p: LlmProvider) -> LlmProviderOut:
    return LlmProviderOut(
        id=p.id, name=p.name, kind=p.kind.value, base_url=p.base_url,
        model=p.model, has_api_key=bool(p.api_key_encrypted),
    )


@router.post("/llm/providers", response_model=LlmProviderOut, status_code=201)
def create_provider(
    body: LlmProviderCreate,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    if db.execute(select(LlmProvider).where(LlmProvider.name == body.name)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="provider name already exists")
    row = LlmProvider(
        name=body.name,
        kind=LlmProviderKind(body.kind),
        base_url=body.base_url,
        api_key_encrypted=encrypt_value(body.api_key) if body.api_key else None,
        model=body.model,
    )
    db.add(row)
    db.commit()
    audit.record(db, admin.name, "llm.provider.create", body.name, {"kind": body.kind})
    return _provider_out(row)


@router.get("/llm/providers", response_model=list[LlmProviderOut])
def list_providers(db: Session = Depends(get_db), _: ApiKey = Depends(require_api_key)):
    rows = db.execute(select(LlmProvider).order_by(LlmProvider.id)).scalars()
    return [_provider_out(p) for p in rows]


@router.post("/chat/sessions")
def create_session(
    body: ChatSessionCreate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, body.project_id)
    provider = db.get(LlmProvider, body.provider_id)
    if project is None or provider is None:
        raise HTTPException(status_code=404, detail="project or provider not found")
    _require_admin_for_external(provider, key)
    session = ChatSession(project_id=project.id, provider_id=provider.id, branch="")
    db.add(session)
    db.commit()
    session.branch = body.branch or f"paas/chat-{session.id}"
    db.commit()
    audit.record(db, key.name, "chat.session.create", project.name,
                 {"provider": provider.name, "branch": session.branch})
    return {"id": session.id, "branch": session.branch, "provider": provider.name}


@router.post("/chat/sessions/{session_id}/messages", response_model=ChatReply)
async def post_message(
    session_id: int,
    body: ChatMessageIn,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    project = db.get(Project, session.project_id)
    provider = db.get(LlmProvider, session.provider_id)

    messages: list[dict] = [{"role": "system", "content": llm_service.EDIT_SYSTEM_PROMPT}]

    # 프로젝트 컨텍스트: 바인딩된 모듈 규약 + 코드 구조 개요 + 요청 파일 내용
    module_ctx = modules_service.context_for_llm(db, project)
    workdir = workspace.workdir_for(project)
    context_parts = [f"Project: {project.name} (type={project.type.value})"]
    if module_ctx:
        context_parts.append("Bound modules (use these env vars):\n" + json.dumps(module_ctx))
    if workdir.exists():
        # 전체 파일 목록 대신 구조 개요(클래스/함수 시그니처+요약)를 준다 — 사용자의
        # 요청을 전체 구조와 항목별 기능을 참조해 대응하도록(요청 2). 개요 추출이
        # 실패해도(파싱 불가 등) 채팅 자체는 막지 않는다.
        try:
            outline = codemap_service.render_outline(codemap_service.build_code_map(workdir))
        except Exception:  # noqa: BLE001
            outline = "\n".join(workspace.file_tree(workdir))
        context_parts.append("Code structure (outline):\n" + outline)
        for path, content in workspace.read_context_files(workdir, body.files).items():
            context_parts.append(f"--- {path} ---\n{content}")
    messages.append({"role": "system", "content": "\n\n".join(context_parts)})

    history = db.execute(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
    ).scalars()
    messages.extend({"role": m.role, "content": m.content} for m in history)
    messages.append({"role": "user", "content": body.content})

    # 프로젝트에 mcp 타입 모듈이 바인딩돼 있으면 그 서버들의 도구를 tools=로
    # 넘겨 모델이 직접 호출할 수 있게 한다(services/mcp_client.py) — tools/list
    # 호출도 블로킹 HTTP라 chat_completion과 함께 스레드에서 돈다.
    mcp_servers = modules_service.mcp_servers_for_project(db, project)
    tools, registry = mcp_client.build_openai_tools(mcp_servers) if mcp_servers else ([], {})
    tool_executor = mcp_client.make_tool_executor(registry) if tools else None

    try:
        reply = await asyncio.to_thread(
            llm_service.chat_completion, provider, messages, db, tools or None, tool_executor,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"llm call failed: {e}")

    db.add(ChatMessage(session_id=session_id, role="user", content=body.content))
    db.add(ChatMessage(session_id=session_id, role="assistant", content=reply))
    db.commit()

    change_id = None
    diff = llm_service.extract_diff(reply)
    if diff:
        change = ProposedChange(
            session_id=session_id, diff=diff, summary=body.content[:255]
        )
        db.add(change)
        db.commit()
        change_id = change.id
    return ChatReply(reply=reply, proposed_change_id=change_id)


@router.post("/changes/{change_id}/apply")
async def apply_change(
    change_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    change = db.get(ProposedChange, change_id)
    if change is None:
        raise HTTPException(status_code=404, detail="change not found")
    if change.status != ChangeStatus.proposed:
        raise HTTPException(status_code=409, detail=f"change already {change.status.value}")
    session = db.get(ChatSession, change.session_id)
    project = db.get(Project, session.project_id)
    try:
        workdir = await asyncio.to_thread(workspace.ensure_branch, project, session.branch)
        sha = await asyncio.to_thread(
            workspace.apply_diff, workdir, change.diff,
            f"chat: {change.summary or f'change #{change.id}'}",
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"diff apply failed: {e}")
    change.status = ChangeStatus.applied
    change.applied_sha = sha
    db.commit()
    audit.record(db, key.name, "chat.change.apply", project.name,
                 {"change_id": change.id, "sha": sha, "branch": session.branch})
    return {"applied_sha": sha, "branch": session.branch}


@router.post("/changes/{change_id}/reject", status_code=204)
def reject_change(
    change_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    change = db.get(ProposedChange, change_id)
    if change is None:
        raise HTTPException(status_code=404, detail="change not found")
    change.status = ChangeStatus.rejected
    db.commit()


@router.get("/projects/{project_id}/files")
def project_files(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """읽기 전용 파일 트리 — 코드 확인 화면. 실제 수정은 채팅/diff 승인 플로우로만 이뤄진다."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        workdir, _sha = checkout(project)
    except BuildError as e:
        raise HTTPException(status_code=502, detail=str(e)[:1000])
    return {"files": workspace.file_tree(workdir)}


@router.get("/projects/{project_id}/files/content")
def project_file_content(
    project_id: int,
    path: str,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """읽기 전용 단일 파일 내용 — 코드 확인 화면. 저장·수정 엔드포인트는 존재하지 않는다."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        workdir, _sha = checkout(project)
    except BuildError as e:
        raise HTTPException(status_code=502, detail=str(e)[:1000])
    try:
        content = workspace.read_file(workdir, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file not found")
    except ValueError as e:
        raise HTTPException(status_code=413, detail=str(e))
    return {"path": path, "content": content}


@router.get("/projects/{project_id}/codemap")
def project_codemap(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """코드 구조 트리 — 파일→클래스/함수 계층 + 항목별 요약(정적 파싱). 확대/축소
    시각화(코드 채팅)용이며, 같은 개요가 채팅 LLM 컨텍스트에도 주입된다."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        workdir, _sha = checkout(project)
    except BuildError as e:
        raise HTTPException(status_code=502, detail=str(e)[:1000])
    return {"files": codemap_service.build_code_map(workdir)}


@router.post("/projects/{project_id}/review")
async def review_project(
    project_id: int,
    body: ReviewRequest,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, project_id)
    provider = db.get(LlmProvider, body.provider_id)
    if project is None or provider is None:
        raise HTTPException(status_code=404, detail="project or provider not found")
    _require_admin_for_external(provider, key)

    diff = body.diff
    if diff is None:
        workdir = workspace.workdir_for(project)
        if not workdir.exists():
            raise HTTPException(status_code=409, detail="no workspace; pass diff explicitly")
        base = body.base_ref or f"origin/{project.branch}"
        diff = await asyncio.to_thread(workspace.diff_between, workdir, base)
    if not diff.strip():
        return {"findings": [], "max_severity": "none"}

    try:
        findings = await asyncio.to_thread(llm_service.review_diff, provider, diff, db)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"llm call failed: {e}")
    severity = llm_service.max_severity(findings)
    audit.record(db, key.name, "code.review", project.name,
                 {"findings": len(findings), "max_severity": severity})
    return {"findings": findings, "max_severity": severity}
