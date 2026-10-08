"""스마트워크 — 모든 사용자에게 열린 메뉴(내비게이션 맨 위). 왼쪽 대화, 오른쪽 대시보드·에이전트·보고서.

개인 업무 맥락(/smartwork/personal/*)은 **로그인한 그 사람의 것만** 다룬다 — 경로에
누구의 것인지 고르는 자리가 없다(키의 주체가 곧 대상이다). 관리자도 남의 것은 못 본다.
"""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..features import is_enabled
from ..models import ApiKey
from ..security import require_api_key
from ..services import bedrock, personal, smartwork
from ..services import llm as llm_service
from . import mcp_servers as mcp

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

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    # 비어 있으면 여는 턴 — 업무 맥락을 보고 할 일을 제안하며 시작한다(services/smartwork).
    messages: list[ChatMessage] = []


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
        ("docs", _read_only(mcp._DOCS_TOOLS), lambda n, a: mcp._docs_call(db, actor, n, a)),
        ("graph", mcp._GRAPH_TOOLS, lambda n, a: mcp._graph_call(n, a)),
        ("storage", [_STORAGE_LIST_TOOL], lambda n, a: mcp._storage_call(
            db, actor, mcp._doc_source(str(a.get("source", ""))), n, a)),
        ("apis", _read_only(mcp._APIS_TOOLS), lambda n, a: mcp._apis_call(db, actor, n, a)),
    ]
    if is_enabled("workspace"):
        # 프로젝트별 접근 검사는 코드 서버의 것을 그대로 탄다(남의 조직 코드는 안 읽힌다).
        sets.append(("code", mcp._CODE_TOOLS, lambda n, a: mcp._code_dispatch(db, key, n, a)))
    if key.is_admin and is_enabled("deploy"):
        # 운영 도구는 조직을 가리지 않고 로그·감사를 보여 준다 — 관리자에게만 붙인다.
        sets.append(("ops", _read_only(mcp._OPS_TOOLS), lambda n, a: mcp._ops_call(db, actor, n, a)))
    return sets


@router.post("/chat")
def chat(body: ChatIn, db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    provider = llm_service.default_provider(db)
    if provider is None:
        raise HTTPException(status_code=503, detail=(
            "기본 LLM 프로바이더가 없습니다 — 관리자가 LLM 메뉴에서 기본 프로바이더를 지정해야 합니다."))
    if body.messages and body.messages[-1].role != "user":
        raise HTTPException(status_code=422, detail="마지막 메시지는 사용자 메시지여야 합니다.")
    try:
        return smartwork.chat(db, key, provider, [m.model_dump() for m in body.messages],
                              _toolsets(db, key))
    except (bedrock.BedrockError, llm_service.LlmTimeout, llm_service.LlmCallFailed,
            llm_service.LlmTruncated) as e:
        # 프로바이더 쪽 사유를 그대로 올린다(api/workflows.py와 같은 이유 — SSO 만료 안내 등).
        raise HTTPException(status_code=502, detail=str(e))


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


@router.post("/personal/folders/{folder}/files")
async def folder_files(
    folder: str,
    files: list[UploadFile] = File(...),
    paths: list[str] = Form(...),
    mtimes: list[float] = Form(...),
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """manifest가 needed로 돌려준 파일을 올린다. paths·mtimes는 files와 같은 순서다
    (브라우저의 File.name에는 폴더 경로가 없어서 상대경로를 따로 싣는다).

    변환(PDF OCR·구형 Office)은 수십 초가 걸릴 수 있어 스레드에서 돌린다 — 이벤트 루프에서
    돌리면 그동안 다른 사람의 요청이 전부 멈춘다."""
    if not (len(files) == len(paths) == len(mtimes)):
        raise HTTPException(status_code=422, detail="files·paths·mtimes 개수가 다릅니다.")
    batch = []
    for upload, rel, mtime in zip(files, paths, mtimes):
        # 크기 상한은 personal.accepts가 다시 보지만, 그 전에 다 읽어 들이지 않는다.
        data = await upload.read(personal.MAX_FILE_BYTES + 1)
        batch.append((rel, data, mtime))
    try:
        return await run_in_threadpool(personal.save_files, db, key.name, folder, batch)
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
