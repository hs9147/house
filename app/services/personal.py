"""스마트워크의 개인 업무 맥락 — 한 사람이 동의하고 올린 문서와 메일.

**사내 문서 저장소와 섞지 않는다.** 사용자마다 숨긴 저장소 하나(`_me-<해시>`)를 두고
색인·.ready·온톨로지 추출은 기존 것(services/docsearch.py)을 그대로 쓴다. 이름이 밑줄로
시작해서 저장소 이름 규칙(storage._NAME_RE)과 겹칠 수 없고, storage.stores()에 들어가지
않으므로 /mcp/docs·/mcp/graph·온톨로지 화면에는 처음부터 보이지 않는다 — 닿는 길은
스마트워크가 **그 사람의 세션으로** 묶어 주는 도구뿐이다.

서버는 사용자 PC의 드라이브를 읽을 수 없다. 로컬 문서는 브라우저의 폴더 선택으로
올라오고(프로젝트 폴더 업로드와 같은 방식), 폴더를 고르는 행위가 곧 동의다.

**원본은 남기지 않는다.** 올라온 파일은 임시 폴더에서 마크다운으로 바꾸고 그 폴더째
지운다 — 저장소에는 변환본(`원래경로.md`)만 있고 색인·온톨로지는 그것에서 만든다. 원본이
없으니 바뀐 파일 판정은 DB에 적어 둔 원본의 크기·수정 시각(PersonalContext.files)으로 한다.
"""
import hashlib
import os
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import PersonalContext, utcnow
from . import docready, docsearch, doctext
from . import storage as storage_service

# 개인 문서로 받는 형식. 사내 문서 폴더는 무엇이 들어 있든 훑지만(SKIP_SUFFIXES로 거른다),
# 여기는 사람의 PC에서 **올라오는** 것이라 문서임이 확실한 것만 받는다 — 폴더 하나를
# 고르면 그 안의 사진·설치 파일·소스 트리까지 딸려 온다.
DOC_SUFFIXES = {
    ".pdf", ".docx", ".xlsx", ".pptx", ".hwpx", ".doc", ".xls", ".ppt", ".hwp",
    ".txt", ".md", ".csv", ".html", ".htm", ".json", ".eml",
}
MAX_FILE_BYTES = docsearch.MAX_FILE_BYTES
# 변환본 확장자. 원래 이름 뒤에 **늘** 붙인다 — .md 원본만 빼 주면 "a.txt"와 "a.txt.md"가
# 같은 변환본 자리를 두고 겹친다.
CONVERTED_SUFFIX = ".md"
# 메일 폴더 이름 — 사용자가 고른 로컬 폴더와 같은 자리에 놓이므로 그 이름은 막는다.
MAIL_DIR = "outlook"
# 한 번 동기화에서 받는 받은편지함 메일 수(최신순) — 브라우저가 이만큼 읽어 보낸다.
MAIL_SYNC_COUNT = 100
# 업로드 직후 큰 파일(색인 즉시 경로가 미루는 것)을 마저 읽는 시간 예산(초).
# 사내 저장소는 주기 색인이 맡지만 개인 저장소는 주기 색인 대상이 아니다.
_REINDEX_BUDGET = 10.0


class PersonalError(Exception):
    """사람에게 그대로 보여 줄 수 있는 이유를 담는다(API가 400/409로 바꾼다)."""


def store_for(email: str) -> storage_service.Store:
    """이 사람의 개인 저장소. 이메일을 그대로 쓰지 않고 해시로 — 폴더·색인 파일 이름에
    계정이 드러나지 않고, 이메일에 든 문자(@·.)가 경로 규칙에 걸리지 않는다."""
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]
    root = (Path(get_settings().personal_root) / digest).resolve()
    return storage_service.Store(name=f"_me-{digest}", root=root, read_only=False, hidden=True)


def get(db: Session, email: str) -> PersonalContext | None:
    return db.execute(
        select(PersonalContext).where(PersonalContext.email == email)
    ).scalar_one_or_none()


def require(db: Session, email: str) -> PersonalContext:
    row = get(db, email)
    if row is None:
        raise PersonalError("개인 업무 맥락 사용에 동의하지 않았습니다 — 먼저 동의하세요.")
    return row


def consent(db: Session, email: str) -> PersonalContext:
    row = get(db, email)
    if row is None:
        row = PersonalContext(email=email, folders=[])
        db.add(row)
        db.commit()
        db.refresh(row)
    store_for(email).root.mkdir(parents=True, exist_ok=True)
    return row


def revoke(db: Session, email: str) -> None:
    """동의 철회 = **전부 지운다**. 변환본·색인·.ready·메일 사본까지.

    휴지통으로 옮기지 않는다(storage.delete_file과 반대). 철회한 사람의 문서가 서버
    어딘가에 남아 있으면 철회가 아니다.
    """
    store = store_for(email)
    shutil.rmtree(store.root, ignore_errors=True)
    shutil.rmtree(docready.root() / store.name, ignore_errors=True)
    docsearch.index_path(store.name).unlink(missing_ok=True)
    row = get(db, email)
    if row is not None:
        db.delete(row)
        db.commit()


def status(db: Session, email: str) -> dict:
    row = get(db, email)
    settings = get_settings()
    mail = {"configured": bool(settings.ms_graph_client_id), "connected": False, "account": "", "synced_at": None}
    if row is None:
        return {"consented": False, "folders": [], "index": None, "mail": mail}
    mail.update(connected=row.mail_synced_at is not None, account=row.mail_account,
                synced_at=row.mail_synced_at.isoformat() if row.mail_synced_at else None)
    index = docsearch.status(store_for(email).name)
    # 변환에 실패한 파일은 저장소에 없어 색인이 모른다 — 기록해 둔 실패를 더한다.
    unconverted = sum(1 for f in (row.files or {}).values() if f.get("error"))
    return {
        "consented": True,
        "consented_at": row.consented_at.isoformat(),
        "folders": row.folders or [],
        "index": {"total": index["total"] + unconverted, "indexed": index["indexed"],
                  "failed": index["failed"] + unconverted},
        "mail": mail,
    }


# --- 로컬 폴더 ---

def _folder_name(name: str) -> str:
    name = name.strip()
    if (not name or "/" in name or "\\" in name or name in (".", "..")
            or name.startswith((".", "_")) or name.lower() == MAIL_DIR):
        raise PersonalError(f"쓸 수 없는 폴더 이름입니다: {name!r}")
    return name


def accepts(rel: str, size: int) -> bool:
    """올릴 대상인가 — 브라우저가 먼저 거르지만 서버가 다시 본다(요청은 꾸밀 수 있다)."""
    path = Path(rel)
    return (path.suffix.lower() in DOC_SUFFIXES and 0 < size <= MAX_FILE_BYTES
            and not docsearch.skip_file(path.name)
            and not any(docsearch.skip_dir(p) for p in path.parts[:-1]))


def manifest(db: Session, email: str, folder: str, entries: list[dict]) -> dict:
    """폴더의 현재 모습을 받아 **올려야 할 파일만** 돌려준다.

    entries = [{"path": 폴더 안 상대경로, "size": 바이트, "mtime": 초}]. 크기와 수정 시각이
    같은 파일은 다시 올리지 않는다 — 폴더를 다시 고를 때마다 전부 올리면 수백 MB가 매번
    오간다. 목록에 없는 파일은 PC에서 지운 것이니 여기서도 지운다(동기화).
    비교 대상은 DB에 적어 둔 원본의 크기·수정 시각이다(원본 파일은 서버에 없다).
    """
    row = require(db, email)
    folder = _folder_name(folder)
    store = store_for(email)
    prefix = f"{folder}/"
    known = dict(row.files or {})
    wanted: dict[str, dict] = {}
    for entry in entries:
        rel = str(entry.get("path", "")).replace("\\", "/").strip("/")
        size = int(entry.get("size") or 0)
        if rel and accepts(rel, size):
            wanted[rel] = {"size": size, "mtime": float(entry.get("mtime") or 0)}

    removed = 0
    for key in [k for k in known if k.startswith(prefix)]:
        if key[len(prefix):] not in wanted:
            _forget_converted(store, key)
            del known[key]
            removed += 1

    needed = []
    for rel, meta in wanted.items():
        prev = known.get(prefix + rel)
        # 수정 시각은 초 단위로만 비교한다 — 브라우저(lastModified)는 밀리초라 그대로 비교하면
        # 같은 파일이 늘 "바뀜"이 된다. 변환에 실패한 파일도 원본이 그대로면 다시 받지 않는다.
        if prev is None or prev["size"] != meta["size"] or int(prev["mtime"]) != int(meta["mtime"]):
            needed.append(rel)

    row.files = known
    folders = [f for f in (row.folders or []) if f.get("name") != folder]
    folders.append({"name": folder, "files": len(wanted), "synced_at": utcnow().isoformat()})
    row.folders = sorted(folders, key=lambda f: f["name"])
    db.commit()
    return {"folder": folder, "files": len(wanted), "needed": sorted(needed), "removed": removed}


def save_file(db: Session, email: str, folder: str, rel: str, source: Path, mtime: float) -> dict:
    """올라온 파일 하나를 마크다운으로 바꿔 남기고 바로 색인한다.

    source는 요청 본문을 흘려 받은 임시 파일이다(api가 받고 api가 지운다) — 파일을 통째로
    메모리에 올리지 않으려고 바이트가 아니라 경로로 받는다. 변환은 요청 안에서 끝낸다 —
    원본을 두지 않으니 큰 파일을 주기 색인에 미뤄 둘 수 없다.
    돌려주는 status: saved | skipped(대상이 아님) | failed(변환 실패, error에 사유).
    """
    row = require(db, email)
    folder = _folder_name(folder)
    store = store_for(email)
    rel = rel.replace("\\", "/").strip("/")
    key = f"{folder}/{rel}"
    size = source.stat().st_size
    if not accepts(rel, size):
        return {"path": rel, "status": "skipped"}
    try:
        target = storage_service.resolve(store.root, key + CONVERTED_SUFFIX)
    except storage_service.StorageError:
        return {"path": rel, "status": "skipped"}
    known = dict(row.files or {})
    meta = {"size": size, "mtime": int(mtime), "error": None}
    try:
        markdown = doctext.extract(source)[0]
    except doctext.ExtractError as e:
        # 옛 변환본은 지운다 — 바뀐 원본과 맞지 않는 내용이 검색되면 안 된다.
        _forget_converted(store, key)
        known[key] = {**meta, "error": str(e)[:300]}
        row.files = known
        db.commit()
        return {"path": rel, "status": "failed", "error": str(e)[:300]}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    if mtime > 0:
        # 원본의 수정 시각을 변환본에 옮긴다 — "최근 문서"(smartwork._recent)가 이 값으로 줄 세운다.
        os.utime(target, (mtime, mtime))
    result = docsearch.index_one(store.name, store.root, key + CONVERTED_SUFFIX)
    known[key] = meta
    row.files = known
    db.commit()
    if result["status"] == "deferred":
        docsearch.reindex(store.name, store.root, budget_seconds=_REINDEX_BUDGET)
    return {"path": rel, "status": "saved"}


def _forget_converted(store: storage_service.Store, key: str) -> None:
    try:
        storage_service.resolve(store.root, key + CONVERTED_SUFFIX).unlink(missing_ok=True)
    except storage_service.StorageError:
        pass
    docsearch.forget_one(store.name, key + CONVERTED_SUFFIX)


def remove_folder(db: Session, email: str, folder: str) -> None:
    row = require(db, email)
    folder = _folder_name(folder)
    store = store_for(email)
    shutil.rmtree(store.root / folder, ignore_errors=True)
    shutil.rmtree(docready.root() / store.name / folder, ignore_errors=True)
    # 지운 파일을 색인에서 빼는 일은 reindex가 한다(디스크에 없는 것 정리는 예산과 무관).
    docsearch.reindex(store.name, store.root, budget_seconds=0)
    row.folders = [f for f in (row.folders or []) if f.get("name") != folder]
    row.files = {k: v for k, v in (row.files or {}).items() if not k.startswith(f"{folder}/")}
    db.commit()


# --- 아웃룩 메일 (Microsoft Graph) ---
#
# **로그인과 메일 읽기는 사용자 브라우저가 한다.** 브라우저가 팝업으로 Microsoft에 로그인해
# 받은 토큰으로 Graph에서 받은편지함을 읽고, 메일 내용만 여기로 보낸다 — 서버는 토큰을
# 받지도 두지도 않고, Graph와 통신하지도 않는다. 메일함 열쇠(갱신 토큰)를 서버에 두면
# 그것 하나로 그 사람의 메일 전체가 열리고, 서버가 graph.microsoft.com에 나가는 길도
# 필요 없어진다. 대가는 동기화가 사람이 화면에서 누를 때만 일어난다는 것이다.


def mail_save(db: Session, email: str, account: str, messages: list[dict]) -> dict:
    """Graph에서 읽어 온 메일(services/msgraph)을 메일 한 통 = 마크다운 한 파일로 남기고 색인한다.

    파일로 두는 이유: 문서와 **같은 길**(색인·.ready·온톨로지)을 타게 하려는 것이다.
    메일 전용 검색을 따로 두면 "문서와 메일에서 함께 찾아라"가 두 번의 검색이 된다.
    """
    row = require(db, email)
    store = store_for(email)
    written = 0
    for msg in messages[:MAIL_SYNC_COUNT]:
        received = str(msg.get("receivedDateTime") or "")
        digest = hashlib.sha256(str(msg.get("id", "")).encode()).hexdigest()[:12]
        rel = f"{MAIL_DIR}/{received[:10] or 'unknown'}_{digest}.md"
        target = store.root / rel
        if target.exists():
            continue  # 받은 메일은 바뀌지 않는다
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_mail_markdown(msg), encoding="utf-8")
        docsearch.index_one(store.name, store.root, rel)
        written += 1
    row.mail_account = account.strip()[:255]
    row.mail_synced_at = utcnow()
    db.commit()
    return {"fetched": len(messages[:MAIL_SYNC_COUNT]), "new": written}


def mail_disconnect(db: Session, email: str) -> None:
    """받아 둔 메일 사본을 지운다. 토큰은 저장하지 않으니 "연결"은 이 사본이 전부다."""
    row = require(db, email)
    store = store_for(email)
    shutil.rmtree(store.root / MAIL_DIR, ignore_errors=True)
    shutil.rmtree(docready.root() / store.name / MAIL_DIR, ignore_errors=True)
    docsearch.reindex(store.name, store.root, budget_seconds=0)
    row.mail_account = ""
    row.mail_synced_at = None
    db.commit()


def _address(entry: dict | None) -> str:
    addr = (entry or {}).get("emailAddress") or {}
    name, mail = addr.get("name", ""), addr.get("address", "")
    return f"{name} <{mail}>" if name and mail and name != mail else (mail or name)


def _mail_markdown(msg: dict) -> str:
    subject = (msg.get("subject") or "(제목 없음)").strip()
    body = ((msg.get("body") or {}).get("content") or "").strip()
    to = ", ".join(_address(r) for r in msg.get("toRecipients") or [])
    lines = [
        f"# {subject}", "",
        f"- 보낸 사람: {_address(msg.get('from'))}",
        f"- 받는 사람: {to}",
        f"- 받은 시각: {msg.get('receivedDateTime', '')}",
    ]
    if msg.get("webLink"):
        lines.append(f"- 원문: {msg['webLink']}")
    return "\n".join([*lines, "", body, ""])

