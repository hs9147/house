"""파일 저장소 — 환경변수로 정한 디렉터리를 이름 뒤에 감춘다.

저장소는 모듈 레지스트리에 등록하는 것이 아니라 **환경변수로 정한다**:

  PAAS_STORAGE_ROOT       플랫폼 자신의 저장소. 이름은 `internal`이고 목록에 안 나온다.
  PAAS_DOC_ROOTS          사내 문서 폴더(쉼표 구분). `이름=경로` 또는 경로만.
  PAAS_DOC_ROOTS_READONLY 그중 **잠글** 폴더 이름. 기본은 비어 있다 = 전부 읽기/쓰기.

**전체 읽기는 /mcp/docs, 쓰기는 폴더별.** 읽기만 필요하면 저장소별 서버를 등록할 필요가
없다 — /mcp/docs 하나가 전 폴더를 가로질러 본문을 찾는다. 저장소별
서버(/mcp/storage/{이름})는 그 폴더의 파일을 다루는 자리이고, **쓰기 도구는 잠기지 않은
폴더의 서버에만 광고된다**(api/mcp_servers.py) — 폴더가 URL에 있기 때문에 성립하는
성질이라, 이 서버를 하나로 합치면 잃는다.

**internal은 숨긴다(hidden).** 플랫폼이 자기 파일을 두는 자리이지 사람이 파일 관리
화면에서 고를 폴더가 아니다. 목록(GET /storage/stores)과 MCP 서버 디렉터리에서 빠지되,
이름으로는 그대로 닿고 /mcp/docs 검색에도 포함된다 — 예전에 여기 올린 파일을 못 꺼내게
만들면 숨긴 것이 아니라 잃은 것이다.

왜 모듈이 아니게 됐나: 저장소가 어느 디렉터리에 얹혀 있는지는 서버를 설치한 사람이
이미 아는 사실이지 콘솔에서 등록할 일이 아니었다. 모듈로 두면 같은 폴더가 이름만 달리
두 번 등록되거나, 존재하지 않는 경로가 등록돼도 열어 보기 전까지 아무도 모른다.
접근은 사내 MCP 서버(/mcp/docs, /mcp/storage/{저장소})와 /storage 창구가 맡는다.
"""
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import get_settings

# 내부 저장소(PAAS_STORAGE_ROOT)의 이름. 문서 폴더가 이 이름을 다시 쓰면 거부한다.
INTERNAL_STORE = "internal"
# 저장소 안 휴지통. 점으로 시작해서 색인(docsearch.skip_dir)과 목록에서 함께 빠진다.
TRASH_DIRNAME = ".trash"


class StorageError(Exception):
    """경로 탈출·저장소 설정 오류 등 저장소 규약 위반."""


@dataclass(frozen=True)
class Store:
    """이름이 붙은 저장소 하나. root는 호출자 밖으로 새어 나가도 되는 값이 아니다 —
    운영자에게 되비추는 자리(list_sources·/storage/stores)에서만 보여 준다.

    hidden은 "고를 목록에 올리지 않는다"는 뜻이지 "닿지 않는다"가 아니다 — 접근을
    막는 것은 read_only가 하는 일이고, 둘을 섞으면 숨긴 저장소의 파일을 꺼낼 방법이
    없어진다.
    """

    name: str
    root: Path
    read_only: bool
    hidden: bool = False


# 저장소 이름은 URL 조각(/mcp/storage/{이름})이자 색인 파일 이름이고, 모듈로 가져올 때
# 모듈 이름이 되기도 한다 — 그래서 모듈 이름과 같은 규칙(ModuleCreate)을 쓴다. 한글을
# 허용하지 않는 이유가 하나 더 있다: 이 플랫폼은 IIS/ARR 서브패스 뒤에 놓이는데, 경로에
# 한글이 들어가면 인코딩이 한 번 더 겹치는 자리가 생긴다.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


# 곧은 따옴표와 굽은 따옴표 모두. 굽은 쪽은 문서·메신저에서 경로를 복사해 오면 붙는다.
_QUOTES = "\"'\u201c\u201d\u2018\u2019"


def _unquote(value: str) -> str:
    """윈도우 경로를 따옴표로 감싸 적는 습관을 받아 준다.

    벗기지 않으면 따옴표가 경로의 일부가 되어 없는 폴더를 가리키고, 목록에는
    exists: false로만 나온다 — "폴더가 비었다"와 구분되지 않아 원인을 찾기 어렵다.
    윈도우 파일 이름에는 따옴표를 쓸 수 없으므로 벗겨서 잃는 것도 없다.
    """
    value = value.strip()
    while len(value) >= 2 and value[0] in _QUOTES and value[-1] in _QUOTES:
        value = value[1:-1].strip()
    return value


def _leaf(path: str) -> str:
    """경로의 마지막 조각. 윈도우 경로(D:\\공유\\규정)를 리눅스에서 파싱할 때도 맞아야
    한다 — Path().name은 posix에서 역슬래시를 구분자로 보지 않는다."""
    return re.split(r"[\\/]+", path.rstrip("/\\"))[-1]


def _slug(leaf: str) -> str:
    """폴더 이름에서 저장소 이름을 만든다("Company Docs" → "company-docs")."""
    return re.sub(r"[^a-z0-9]+", "-", leaf.lower()).strip("-")


def _check_name(name: str, entry: str) -> str:
    if not _NAME_RE.match(name):
        raise StorageError(
            f"PAAS_DOC_ROOTS 항목에서 저장소 이름을 정할 수 없습니다: {entry!r}"
            " — `이름=경로` 형식으로 이름을 직접 지정하세요"
            " (소문자·숫자·하이픈, 예: rules=D:\\공유\\사내규정).")
    return name


def stores() -> list[Store]:
    """지금 열 수 있는 저장소 전부 — 숨긴 internal 하나 + 사내 문서 폴더들.

    목록에 보일 것만 필요하면 visible_stores()를 쓴다. 여기서는 숨긴 것도 함께 준다:
    /mcp/docs의 "전체 읽기"와 이름으로 하는 직접 접근이 이 목록을 쓴다.

    설정이 잘못돼 있으면 조용히 빼지 않고 StorageError를 낸다: 목록에서 사라지는 것과
    "그 폴더에 문서가 없다"는 구분이 되지 않아 원인을 찾는 데 시간을 다 쓰게 된다.
    """
    settings = get_settings()
    # 잠그는 것이 별도의 행위다 — 경로 옆이 아니라 목록에서 정한다.
    locked = {n.strip() for n in settings.doc_roots_readonly.split(",") if n.strip()}
    found = [Store(
        INTERNAL_STORE,
        Path(settings.storage_root or "./data/storage").resolve(),
        read_only=False,
        hidden=True,
    )]
    seen = {INTERNAL_STORE}
    for entry in settings.doc_roots.split(","):
        entry = _unquote(entry)
        if not entry:
            continue
        name, sep, path = entry.partition("=")
        if sep:
            name, path = _unquote(name), _unquote(path)
        else:
            path, name = entry, _slug(_leaf(entry))
        if not path:
            raise StorageError(f"PAAS_DOC_ROOTS 항목에 경로가 없습니다: {entry!r}")
        _check_name(name, entry)
        if name in seen:
            raise StorageError(
                f"PAAS_DOC_ROOTS의 저장소 이름이 겹칩니다: {name!r}"
                f" ({INTERNAL_STORE}은 내부 저장소가 쓰는 이름입니다)")
        seen.add(name)
        found.append(Store(name, Path(path).resolve(), read_only=name in locked))

    # 목록에 없는 이름이 남았다 = 오타이거나 지운 폴더를 가리킨다. 조용히 넘기면 잠근
    # 줄 알았던 폴더가 열린 채로 남고, 그 사실을 알아낼 방법이 없다.
    unknown = locked - seen
    if unknown:
        raise StorageError(
            f"PAAS_DOC_ROOTS_READONLY에 없는 저장소 이름이 있습니다: {', '.join(sorted(unknown))}"
            f" — PAAS_DOC_ROOTS에 있는 이름이어야 합니다(현재: {', '.join(sorted(seen))}).")
    return found


def visible_stores() -> list[Store]:
    """사람이 고를 수 있는 저장소 — 숨긴 것을 뺀 목록(파일 관리 화면·MCP 서버 디렉터리)."""
    return [s for s in stores() if not s.hidden]


def store(name: str) -> Store | None:
    """이름으로 찾는다 — 숨긴 저장소도 찾힌다. 숨긴 것은 고르는 목록에서 뺀 것이지
    닿지 못하게 한 것이 아니다."""
    return next((s for s in stores() if s.name == name), None)


def url_for(store_name: str) -> str:
    """저장소의 공개 창구 주소 — 콘솔 파일 관리 화면이 쓰는 주소다."""
    settings = get_settings()
    base = settings.platform_public_url.rstrip("/") or f"https://{settings.base_domain}"
    return f"{base}/paas/api/v1/storage/{store_name}"


def resolve(root: Path, rel: str) -> Path:
    """root 안의 경로로만 해석한다. 벗어나거나 절대 경로면 StorageError.

    절대 경로를 조용히 상대 경로로 바꾸지 않는다 — "/etc/passwd"를 저장소 안의
    "etc/passwd"로 받아 주면 호출자가 무엇을 쓴 건지 알 수 없다.
    """
    if rel.startswith(("/", "\\")) or Path(rel).is_absolute():
        raise StorageError(f"절대 경로는 쓸 수 없습니다: {rel}")
    target = (root / rel).resolve()
    if target != root and not target.is_relative_to(root):
        raise StorageError(f"경로가 저장소를 벗어납니다: {rel}")
    return target


def list_files(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    return sorted(
        (
            {"path": p.relative_to(root).as_posix(), "size": p.stat().st_size}
            for p in root.rglob("*")
            # 휴지통은 목록에 넣지 않는다 — 지운 것이 계속 보이면 지운 것이 아니다.
            # (docsearch는 점으로 시작하는 폴더를 통째로 건너뛰므로 검색에도 안 잡힌다.)
            if p.is_file() and TRASH_DIRNAME not in p.relative_to(root).parts
        ),
        key=lambda f: f["path"],
    )


def _touch_index(store: "Store", rel: str, *, removed: bool = False) -> None:
    """방금 바뀐 파일 하나를 색인·그래프에 반영한다 — **실패는 삼킨다**.

    파일은 이미 디스크에 있다(또는 이미 휴지통으로 갔다). 여기서 예외를 올리면 저장에
    성공한 업로드가 500이 되고, 사용자는 같은 파일을 다시 올린다. 색인은 파생 데이터고
    주기 작업이 어차피 맞추므로, 못 하고 넘어가면 늦어질 뿐 틀리지 않는다.
    """
    from . import docsearch  # noqa: PLC0415 — 색인은 저장소의 파생 데이터다(역참조 최소화)

    try:
        if removed:
            docsearch.forget_one(store.name, rel)
        else:
            docsearch.index_one(store.name, store.root, rel)
    except Exception as e:  # noqa: BLE001
        # 조용히 넘기지는 않는다 — 늘 늦는다면 원인을 찾을 실마리가 여기밖에 없다.
        print(f"[paas] 색인 즉시 갱신 실패({store.name}:{rel}) — 주기 색인에 맡깁니다: {e}")


# 아래 둘만 Store를 받는다(읽기 쪽은 root면 충분하다): 쓰기는 색인을 함께 건드려야 하고,
# 색인은 저장소 **이름**으로 갈린다. 인자를 root로 두면 새 창구가 생길 때마다 색인 갱신을
# 따로 기억해야 하는데, 그건 잊는다 — 실제로 업로드·삭제 창구 넷이 다 잊고 있었다.
def write_file(store: "Store", rel: str, data: bytes) -> str:
    target = resolve(store.root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    saved = target.relative_to(store.root).as_posix()
    _touch_index(store, saved)
    return saved


def delete_file(store: "Store", rel: str) -> str:
    """지우지 않고 저장소 안 휴지통으로 옮긴다. 옮겨진 자리(저장소 기준 상대경로)를 돌려준다.

    **사내 공유 폴더에는 되돌리기가 없다.** 서비스 계정이 SMB로 지우면 윈도우 휴지통에
    가지 않고 그대로 사라진다. 그런데 이 경로는 사람뿐 아니라 LLM도 부른다(MCP의
    delete_file) — 한 번의 잘못된 호출이 복구 불가능하면 안 된다.

    휴지통을 **저장소 안**에 두는 이유: 같은 볼륨이라 이동이 즉시 끝나고(다른 볼륨이면
    복사 후 삭제가 되어 큰 파일에서 실패할 여지가 생긴다), 사람이 그 폴더에서 바로 찾아
    되돌릴 수 있다. 점으로 시작해서 색인·목록에서 함께 빠진다.
    """
    root = store.root
    target = resolve(root, rel)
    if not target.is_file():
        raise FileNotFoundError(rel)

    grave = root / TRASH_DIRNAME / Path(rel)
    if grave.exists():
        # 같은 파일을 두 번 지웠다 — 먼저 지운 것을 덮어쓰면 되돌릴 것이 하나 사라진다.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        grave = grave.with_name(f"{grave.stem}.{stamp}{grave.suffix}")
    grave.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(grave))
    # 옮긴 자리는 점 폴더라 색인에 들어가지 않는다 — 원래 자리만 빼면 된다.
    _touch_index(store, target.relative_to(root).as_posix(), removed=True)
    return grave.relative_to(root).as_posix()
