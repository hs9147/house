"""스마트워크 — 모든 사용자에게 열린 메뉴(내비게이션 맨 위). 왼쪽 대화, 오른쪽 대시보드·에이전트·보고서.

대화는 세션(/smartwork/sessions/*) 단위다 — 세션 하나가 업무 하나이고, 소유자가 다른
사람(다른 부서여도)과 공유할 수 있다. 권한 판정은 services/worksession.py에 있다.

개인 업무 맥락(/smartwork/personal/*)은 **로그인한 그 사람의 것만** 다룬다 — 경로에
누구의 것인지 고르는 자리가 없다(키의 주체가 곧 대상이다). 관리자도 남의 것은 못 본다.
"""
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..features import is_enabled
from ..models import ApiKey
from ..security import require_api_key
from ..services import bedrock, personal, smartwork, worksession
from ..services import llm as llm_service
from . import mcp_servers as mcp
from .llm import provider_error

router = APIRouter(prefix="/smartwork", tags=["smartwork"])


def _personal_error(e: personal.PersonalError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


@router.get("/agents")
def list_agents(db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    return smartwork.agents(db, key)


@router.get("/workflows")
def list_department_workflows(db: Session = Depends(get_db),
                              key: ApiKey = Depends(require_api_key)):
    """업무 제안의 근거 — 대시보드가 대화와 같은 목록을 보여 준다."""
    return smartwork.department_workflows(db, key)


# --- 대화 ---

# 대화가 쓰는 사내 MCP 도구는 **읽기**만 고른다. 색인 다시 돌리기·카탈로그 동기화·주기
# 작업 실행은 대화 한 줄로 일어날 일이 아니다 — 그건 각 메뉴에서 사람이 누른다.
_EXCLUDED = {"reindex_docs", "sync_catalog", "run_scheduled_job"}

# 저장소 서버(/mcp/storage/{저장소})는 저장소마다 따로라, 대화에는 목록 보기 하나만
# source 인자를 붙여 얹는다. 검색·읽기는 docs 도구가 저장소를 가로질러 이미 한다.
_STORAGE_LIST_TOOL = {
    **mcp._STORAGE_READ_TOOLS[0],
    "inputSchema": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "저장소 이름(docs__list_sources 값)"},
            **mcp._STORAGE_READ_TOOLS[0]["inputSchema"]["properties"],
        },
        "required": ["source"],
    },
}


def _read_only(tools: list[dict]) -> list[dict]:
    return [t for t in tools if t["name"] not in _EXCLUDED]


def _toolsets(db: Session, key: ApiKey) -> list[smartwork.Toolset]:
    actor = key.name
    sets: list[smartwork.Toolset] = [
        ("docs", _read_only(mcp._DOCS_TOOLS), lambda n, a: mcp._docs_call(db, actor, n, a, key=key)),
        ("graph", mcp._GRAPH_TOOLS, lambda n, a: mcp._graph_call(n, a, db=db, key=key)),
        ("storage", [_STORAGE_LIST_TOOL], lambda n, a: mcp._storage_call(
            db, actor, mcp._doc_source(str(a.get("source", "")), db, key), n, a)),
        ("apis", _read_only(mcp._APIS_TOOLS), lambda n, a: mcp._apis_call(db, actor, n, a)),
    ]
    if is_enabled("workspace"):
        # 프로젝트별 접근 검사는 코드 서버의 것을 그대로 탄다(남의 조직 코드는 안 읽힌다).
        sets.append(("code", mcp._CODE_TOOLS, lambda n, a: mcp._code_dispatch(db, key, n, a)))
    if key.is_admin and is_enabled("deploy"):
        # 운영 도구는 조직을 가리지 않고 로그·감사를 보여 준다 — 관리자에게만 붙인다.
        sets.append(("ops", _read_only(mcp._OPS_TOOLS), lambda n, a: mcp._ops_call(db, actor, n, a)))
    return sets


def _session_error(e: worksession.SessionError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


@router.get("/orgs")
def session_orgs(db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    """세션 맥락으로 고를 수 있는 내 소속 조직과 그 조직의 워크플로."""
    return worksession.org_choices(db, key)


class SessionIn(BaseModel):
    title: str = ""
    organization_id: int | None = None
    workflow_id: int | None = None


@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    return worksession.list_for(db, key.name)


@router.post("/sessions", status_code=201)
def create_session(body: SessionIn, db: Session = Depends(get_db),
                   key: ApiKey = Depends(require_api_key)):
    try:
        row = worksession.create(db, key, body.title, body.organization_id, body.workflow_id)
        return worksession.detail(db, *worksession.get(db, row.id, key.name))
    except worksession.SessionError as e:
        raise _session_error(e)


@router.get("/sessions/{session_id}")
def get_session(session_id: int, after: int = 0, db: Session = Depends(get_db),
                key: ApiKey = Depends(require_api_key)):
    try:
        return worksession.detail(db, *worksession.get(db, session_id, key.name), after=after)
    except worksession.SessionError as e:
        raise _session_error(e)


class SessionPatch(BaseModel):
    title: str | None = None
    organization_id: int | None = None
    workflow_id: int | None = None


@router.patch("/sessions/{session_id}")
def update_session(session_id: int, body: SessionPatch, db: Session = Depends(get_db),
                   key: ApiKey = Depends(require_api_key)):
    """보낸 필드만 바꾼다 — null은 '비움'(조직·워크플로를 고르지 않은 상태로)."""
    try:
        worksession.update(db, key, session_id, body.model_dump(include=body.model_fields_set))
        return worksession.detail(db, *worksession.get(db, session_id, key.name))
    except worksession.SessionError as e:
        raise _session_error(e)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: int, db: Session = Depends(get_db),
                   key: ApiKey = Depends(require_api_key)):
    try:
        worksession.remove(db, key.name, session_id)
    except worksession.SessionError as e:
        raise _session_error(e)
    audit.record(db, key.name, "smartwork.session.delete", str(session_id))


class MemberIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)


@router.post("/sessions/{session_id}/members", status_code=204)
def add_session_member(session_id: int, body: MemberIn, db: Session = Depends(get_db),
                       key: ApiKey = Depends(require_api_key)):
    try:
        worksession.add_member(db, key.name, session_id, body.email)
    except worksession.SessionError as e:
        raise _session_error(e)
    audit.record(db, key.name, "smartwork.session.share", str(session_id), {"member": body.email})


@router.delete("/sessions/{session_id}/members/{email}", status_code=204)
def remove_session_member(session_id: int, email: str, db: Session = Depends(get_db),
                          key: ApiKey = Depends(require_api_key)):
    try:
        worksession.remove_member(db, key.name, session_id, email)
    except worksession.SessionError as e:
        raise _session_error(e)
    audit.record(db, key.name, "smartwork.session.unshare", str(session_id), {"member": email})


class SessionContextIn(BaseModel):
    folders: list[str] = []
    mail: bool = False


@router.put("/sessions/{session_id}/context")
def set_session_context(session_id: int, body: SessionContextIn, db: Session = Depends(get_db),
                        key: ApiKey = Depends(require_api_key)):
    """**내** 개인 맥락 중 이 세션에 쓸 것 — 참여자마다 따로다."""
    try:
        return worksession.set_context(db, key.name, session_id, body.folders, body.mail)
    except worksession.SessionError as e:
        raise _session_error(e)


class SessionMessageIn(BaseModel):
    # 비어 있으면 여는 턴 — 업무 맥락을 보고 할 일을 제안하며 시작한다(services/smartwork).
    content: str = ""


@router.post("/sessions/{session_id}/messages")
def session_message(session_id: int, body: SessionMessageIn, db: Session = Depends(get_db),
                    key: ApiKey = Depends(require_api_key)):
    provider = llm_service.default_provider(db)
    if provider is None:
        raise HTTPException(status_code=503, detail=(
            "기본 LLM 프로바이더가 없습니다 — 관리자가 LLM 메뉴에서 기본 프로바이더를 지정해야 합니다."))
    try:
        return worksession.send(db, key, provider, session_id, body.content, _toolsets(db, key))
    except worksession.SessionError as e:
        raise _session_error(e)
    except (bedrock.BedrockError, llm_service.LlmTimeout, llm_service.LlmCallFailed,
            llm_service.LlmTruncated) as e:
        # 프로바이더 쪽 사유를 그대로 올린다(api/workflows.py와 같은 이유). SSO 만료면 로그인을
        # 바로 시작해 승인 주소를 이 사람의 화면에 띄운다(api/llm.py provider_error).
        raise provider_error(db, key.name, e)


# --- 개인 업무 맥락 ---

@router.get("/personal")
def personal_status(db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    return personal.status(db, key.name)


@router.post("/personal/consent")
def personal_consent(db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    personal.consent(db, key.name)
    audit.record(db, key.name, "smartwork.personal.consent", key.name)
    return personal.status(db, key.name)


@router.delete("/personal", status_code=204)
def personal_revoke(db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    personal.revoke(db, key.name)
    audit.record(db, key.name, "smartwork.personal.revoke", key.name)


class ManifestEntry(BaseModel):
    path: str
    size: int
    mtime: float = 0


class ManifestIn(BaseModel):
    entries: list[ManifestEntry]


@router.post("/personal/folders/{folder}/manifest")
def folder_manifest(folder: str, body: ManifestIn, db: Session = Depends(get_db),
                    key: ApiKey = Depends(require_api_key)):
    try:
        return personal.manifest(db, key.name, folder, [e.model_dump() for e in body.entries])
    except personal.PersonalError as e:
        raise _personal_error(e)


@router.put("/personal/folders/{folder}/file")
async def folder_file(folder: str, path: str, request: Request, mtime: float = 0,
                      db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    """manifest가 needed로 돌려준 파일 **하나**를 본문 그대로 받는다(path는 폴더 안 상대경로).

    여러 파일을 한 요청에 싣지 않고, 본문도 메모리에 다 올리지 않는다 — 조각마다 임시
    파일에 흘려 쓰고 상한을 넘으면 그 자리에서 끊는다. 변환이 끝나면 임시 파일은 지운다
    (원본은 서버에 남지 않는다). 변환(PDF OCR·구형 Office)은 수십 초가 걸릴 수 있어
    스레드에서 돌린다 — 이벤트 루프에서 돌리면 그동안 다른 사람의 요청이 전부 멈춘다."""
    limit = personal.MAX_FILE_BYTES
    too_big = HTTPException(status_code=413,
                            detail=f"파일이 상한({limit // (1024 * 1024)}MB)보다 큽니다.")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_big
    with tempfile.TemporaryDirectory(prefix="paas-personal-") as tmp:
        # 이름은 확장자만 살린다 — 변환기가 확장자로 형식을 고른다.
        source = Path(tmp) / ("upload" + Path(path).suffix.lower())
        size = 0
        with source.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise too_big
                out.write(chunk)
        try:
            return await run_in_threadpool(
                personal.save_file, db, key.name, folder, path, source, mtime)
        except personal.PersonalError as e:
            raise _personal_error(e)


@router.delete("/personal/folders/{folder}", status_code=204)
def folder_remove(folder: str, db: Session = Depends(get_db),
                  key: ApiKey = Depends(require_api_key)):
    try:
        personal.remove_folder(db, key.name, folder)
    except personal.PersonalError as e:
        raise _personal_error(e)


class MailIn(BaseModel):
    account: str = ""
    # Graph 메시지 그대로(id·subject·from·toRecipients·receivedDateTime·body·webLink)
    messages: list[dict]


@router.post("/personal/mail/messages")
def mail_save(body: MailIn, db: Session = Depends(get_db),
              key: ApiKey = Depends(require_api_key)):
    """브라우저가 Graph에서 읽어 온 메일을 받는다 — 서버는 토큰을 보지 않는다."""
    try:
        result = personal.mail_save(db, key.name, body.account, body.messages)
    except personal.PersonalError as e:
        raise _personal_error(e)
    audit.record(db, key.name, "smartwork.personal.mail.sync", body.account, {"new": result["new"]})
    return result


@router.delete("/personal/mail", status_code=204)
def mail_disconnect(db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    try:
        personal.mail_disconnect(db, key.name)
    except personal.PersonalError as e:
        raise _personal_error(e)
    audit.record(db, key.name, "smartwork.personal.mail.disconnect", key.name)
