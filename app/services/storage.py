"""파일 저장소 — 환경변수로 정한 디렉터리를 이름 뒤에 감춘다.

저장소는 모듈 레지스트리에 등록하는 것이 아니라 **환경변수로 정한다**:

  PAAS_STORAGE_ROOT  내부 저장소 한 곳(쓰기 가능). 이름은 `internal`.
  PAAS_DOC_ROOTS     사내 문서 폴더(읽기 전용, 쉼표 구분). `이름=경로` 또는 경로만.

왜 모듈이 아니게 됐나: 저장소가 어느 디렉터리에 얹혀 있는지는 서버를 설치한 사람이
이미 아는 사실이지 콘솔에서 등록할 일이 아니었다. 모듈로 두면 같은 폴더가 이름만 달리
두 번 등록되거나, 존재하지 않는 경로가 등록돼도 열어 보기 전까지 아무도 모른다.
접근은 사내 MCP 서버(/mcp/docs, /mcp/storage/{저장소})와 /storage 창구가 맡는다.
"""
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import get_settings

# 내부 저장소(PAAS_STORAGE_ROOT)의 이름. 문서 폴더가 이 이름을 다시 쓰면 거부한다.
INTERNAL_STORE = "internal"


class StorageError(Exception):
    """경로 탈출·저장소 설정 오류 등 저장소 규약 위반."""


@dataclass(frozen=True)
class Store:
    """이름이 붙은 저장소 하나. root는 호출자 밖으로 새어 나가도 되는 값이 아니다 —
    운영자에게 되비추는 자리(list_sources·/storage/stores)에서만 보여 준다."""

    name: str
    root: Path
    read_only: bool


# 저장소 이름은 URL 조각(/mcp/storage/{이름})이자 색인 파일 이름이고, 모듈로 가져올 때
# 모듈 이름이 되기도 한다 — 그래서 모듈 이름과 같은 규칙(ModuleCreate)을 쓴다. 한글을
# 허용하지 않는 이유가 하나 더 있다: 이 플랫폼은 IIS/ARR 서브패스 뒤에 놓이는데, 경로에
# 한글이 들어가면 인코딩이 한 번 더 겹치는 자리가 생긴다.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


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
    """지금 열 수 있는 저장소 전부 — 내부 저장소 하나 + 사내 문서 폴더들.

    설정이 잘못돼 있으면 조용히 빼지 않고 StorageError를 낸다: 목록에서 사라지는 것과
    "그 폴더에 문서가 없다"는 구분이 되지 않아 원인을 찾는 데 시간을 다 쓰게 된다.
    """
    settings = get_settings()
    found = [Store(
        INTERNAL_STORE,
        Path(settings.storage_root or "./data/storage").resolve(),
        read_only=False,
    )]
    seen = {INTERNAL_STORE}
    for entry in settings.doc_roots.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, path = entry.partition("=")
        if sep:
            name, path = name.strip(), path.strip()
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
        found.append(Store(name, Path(path).resolve(), read_only=True))
    return found


def store(name: str) -> Store | None:
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
            if p.is_file()
        ),
        key=lambda f: f["path"],
    )


def write_file(root: Path, rel: str, data: bytes) -> str:
    target = resolve(root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target.relative_to(root).as_posix()


def delete_file(root: Path, rel: str) -> None:
    target = resolve(root, rel)
    if not target.is_file():
        raise FileNotFoundError(rel)
    target.unlink()
