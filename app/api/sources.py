"""정보 출처 창구 — 등록·스캔·저장(admin 전용).

**헤더 값은 어디로도 돌려주지 않는다.** 응답에는 헤더 이름만 싣고, 감사에도 이름만 남긴다.
수정할 때 headers를 비워 보내면(None) 그대로 두고, 빈 문자열이면 지운다 — 값을 다시 보여 줄
수 없으니 "그대로 둔다"를 따로 표현해야 한다.

저장은 사람이 결정한다. 스캔이 저장 유형(조회 정보 정리·문서·표·발표 파일)마다 제안(기존
저장소 / 새 저장소와 폴더)을 내고, admin이 그대로 받거나 고쳐서(또는 저장 안 함) 저장을 누른다.
새 저장소면 폴더를 만들고 .env를 고쳐 재시작 없이 반영한다.
"""
import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, InfoSource
from ..security import require_admin
from ..services import infosource, llm, storage

router = APIRouter(tags=["sources"])


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: str
    url: str = Field(min_length=1, max_length=1024)
    headers: str = ""
    note: str = ""


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    url: str | None = Field(default=None, min_length=1, max_length=1024)
    note: str | None = None
    # None = 그대로 둔다 · "" = 지운다 · 그 밖 = 바꾼다(값을 다시 보여 줄 수 없어서다)
    headers: str | None = None


class SaveTarget(BaseModel):
    mode: str            # existing | new | skip
    store: str = ""
    path: str = ""


class SourceSave(BaseModel):
    # {유형: 위치} — 유형은 스캔이 낸 제안의 키(pages·documents·sheets·slides)
    targets: dict[str, SaveTarget]


def _host(url: str) -> str:
    # 감사에는 주소의 출처만 — 경로·쿼리에 무엇이 실려 있을지 모른다.
    return infosource._origin(url)


def _out(row: InfoSource, *, full: bool = False) -> dict:
    data = {
        "id": row.id, "name": row.name, "kind": row.kind, "url": row.url, "note": row.note,
        "headers": infosource.header_names(row), "status": row.status, "error": row.error,
        "target_store": row.target_store, "created_at": row.created_at,
        "scanned_at": row.scanned_at, "saved_at": row.saved_at,
        "counts": _counts(row),
    }
    if full:
        data["scan"] = row.scan
        data["proposal"] = row.proposal
    return data


def _counts(row: InfoSource) -> dict:
    scan = row.scan or {}
    return {"pages": len(scan.get("pages") or []), "endpoints": len(scan.get("endpoints") or []),
            "tools": len(scan.get("tools") or []), "files": len(scan.get("files") or [])}


def _row_or_404(db: Session, source_id: int) -> InfoSource:
    row = db.get(InfoSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="정보 출처를 찾을 수 없습니다.")
    return row


def _check_name(db: Session, name: str, own_id: int | None = None) -> str:
    name = name.strip()
    other = db.execute(select(InfoSource).where(InfoSource.name == name)).scalar_one_or_none()
    if other is not None and other.id != own_id:
        raise HTTPException(status_code=409, detail=f"같은 이름의 출처가 있습니다: {name}")
    return name


@router.get("/sources/capabilities")
def capabilities(db: Session = Depends(get_db), _: ApiKey = Depends(require_admin)):
    """스캔에 쓸 수 있는 수단 — 화면이 "무엇으로 읽는지"를 미리 말한다."""
    return {"browser": infosource.browser_available(),
            "llm": llm.default_provider(db) is not None,
            "limits": {"pages": infosource.MAX_PAGES, "depth": infosource.MAX_DEPTH,
                       "shots": infosource.MAX_SHOTS}}


@router.get("/sources")
def list_sources(db: Session = Depends(get_db), _: ApiKey = Depends(require_admin)):
    rows = db.execute(select(InfoSource).order_by(InfoSource.id.desc())).scalars()
    return [_out(r) for r in rows]


@router.post("/sources", status_code=201)
def create_source(body: SourceCreate, db: Session = Depends(get_db),
                  admin: ApiKey = Depends(require_admin)):
    if body.kind not in infosource.KINDS:
        raise HTTPException(status_code=400, detail="종류는 web·api·mcp 중 하나입니다.")
    try:
        url = infosource.check_url(body.url)
        headers = infosource.parse_headers(body.headers)
    except infosource.SourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    row = InfoSource(name=_check_name(db, body.name), kind=body.kind, url=url, note=body.note,
                     headers_encrypted=infosource.encrypt_headers(headers))
    db.add(row)
    db.commit()
    audit.record(db, admin.name, "source.create", row.name,
                 {"kind": row.kind, "origin": _host(url), "headers": sorted(headers)})
    return _out(row, full=True)


@router.get("/sources/{source_id}")
def get_source(source_id: int, db: Session = Depends(get_db), _: ApiKey = Depends(require_admin)):
    return _out(_row_or_404(db, source_id), full=True)


@router.patch("/sources/{source_id}")
def update_source(source_id: int, body: SourceUpdate, db: Session = Depends(get_db),
                  admin: ApiKey = Depends(require_admin)):
    row = _row_or_404(db, source_id)
    changed: list[str] = []
    try:
        if body.name is not None and body.name.strip() != row.name:
            row.name = _check_name(db, body.name, row.id)
            changed.append("name")
        if body.url is not None and body.url.strip() != row.url:
            row.url = infosource.check_url(body.url)
            changed.append("url")
        if body.headers is not None:
            row.headers_encrypted = infosource.encrypt_headers(infosource.parse_headers(body.headers))
            changed.append("headers")
    except infosource.SourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if body.note is not None:
        row.note = body.note
    db.commit()
    audit.record(db, admin.name, "source.update", row.name,
                 {"changed": changed, "headers": infosource.header_names(row)})
    return _out(row, full=True)


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: int, db: Session = Depends(get_db),
                  admin: ApiKey = Depends(require_admin)):
    row = _row_or_404(db, source_id)
    name = row.name
    infosource.forget(row)
    db.delete(row)
    db.commit()
    # 저장한 문서는 지우지 않는다 — 이미 저장소의 문서이고, 지우는 것은 파일 관리의 일이다.
    audit.record(db, admin.name, "source.delete", name, None)


@router.post("/sources/{source_id}/scan", status_code=202)
def scan_source(source_id: int, db: Session = Depends(get_db),
                admin: ApiKey = Depends(require_admin)):
    row = _row_or_404(db, source_id)
    try:
        infosource.start_scan(db, row)
    except infosource.SourceError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit.record(db, admin.name, "source.scan", row.name,
                 {"kind": row.kind, "origin": _host(row.url),
                  "browser": infosource.browser_available() and row.kind == "web"})
    db.refresh(row)
    return _out(row, full=True)


@router.post("/sources/{source_id}/browser-result", status_code=202)
def browser_result(source_id: int, file: UploadFile = File(...), db: Session = Depends(get_db),
                   admin: ApiKey = Depends(require_admin)):
    """북마크릿이 사용자 브라우저에서 만든 JSON 파일을 받는다(services/infosource.from_browser)."""
    row = _row_or_404(db, source_id)
    raw = file.file.read(infosource.MAX_UPLOAD_BYTES + 1)
    if len(raw) > infosource.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=(
            f"파일이 너무 큽니다({infosource.MAX_UPLOAD_BYTES // 1_000_000}MB 이하)."))
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="JSON 파일이 아닙니다.")
    try:
        counts = infosource.accept_browser_result(db, row, payload)
    except infosource.ScanBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    except infosource.SourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(db, admin.name, "source.browser_scan", row.name,
                 {"origin": _host(row.url), "bytes": len(raw), **counts})
    db.refresh(row)
    return _out(row, full=True)


@router.post("/sources/{source_id}/save")
def save_source(source_id: int, body: SourceSave, db: Session = Depends(get_db),
                admin: ApiKey = Depends(require_admin)):
    row = _row_or_404(db, source_id)
    targets = {c: {**t.model_dump(), "store": t.store.strip()} for c, t in body.targets.items()}
    try:
        result = infosource.save(db, row, targets)
    except (infosource.SourceError, storage.StorageError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    proposed = (row.proposal or {}).get("targets") or {}
    audit.record(db, admin.name, "source.save", row.name, {
        "targets": {c: t["store"] if t["mode"] != "skip" else "-" for c, t in targets.items()},
        "written": len(result["files"]), "same": len(result["same"]), "removed": len(result["removed"]),
        # 유형마다 제안을 그대로 받았는지 — 제안이 자주 틀리면 여기서 보인다.
        "as_proposed": {c: (proposed.get(c) or {}).get("mode") == t["mode"]
                        and (proposed.get(c) or {}).get("store") == t["store"]
                        for c, t in targets.items()},
        "created": [s["root"] for s in result["stores"] if s["created"]],
    })
    return {**result, "source": _out(row, full=True)}


@router.get("/sources/{source_id}/scans")
def list_scans(source_id: int, db: Session = Depends(get_db), _: ApiKey = Depends(require_admin)):
    """스캔 이력 — 언제 무엇으로 몇 쪽·몇 파일을 받았고, 지난번과 무엇이 달랐는지."""
    return infosource.history(db, _row_or_404(db, source_id))


@router.get("/sources/{source_id}/shots/{n}")
def get_shot(source_id: int, n: int, db: Session = Depends(get_db),
             _: ApiKey = Depends(require_admin)):
    path = infosource.shot_path(_row_or_404(db, source_id), n)
    if path is None:
        raise HTTPException(status_code=404, detail="캡처가 없습니다.")
    return FileResponse(path, media_type="image/jpeg")
