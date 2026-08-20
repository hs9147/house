"""`.ready` — 문서를 LLM이 읽을 마크다운으로 바꿔 둔 캐시.

**왜 필요한가.** read_doc은 부를 때마다 원본을 다시 추출했다. PDF 한 건에 수십~수백
ms가 들고, 대화 한 번에 같은 문서를 여러 번 읽는 일이 흔하다. 색인은 이미 추출을
하고 있으므로, 그때 나온 마크다운을 파일로 남겨 두면 읽기는 파일 읽기 한 번이 된다.

**왜 파일인가.** 색인 sqlite에 넣어도 되지만, 파일이면 운영자가 열어서 **모델이 실제로
보는 것**을 그대로 확인할 수 있다. "붙였는데 검색이 안 된다"의 원인이 추출 결과에 있을
때 그게 유일하게 빠른 확인 경로다.

**왜 색인 폴더 아래인가.** 사내 문서 폴더(PAAS_DOC_ROOTS)는 읽기 전용이다 — 플랫폼이
만든 폴더가 아니라서 그렇게 정했고, 거기에 캐시를 쓰면 남의 공유 드라이브에 정체불명
폴더가 생기고 백업·동기화에도 딸려 간다. 게다가 .md는 색인 제외 확장자가 아니라서
문서 폴더 안에 두면 **자기 캐시를 다시 색인**한다(문서마다 검색 결과가 둘씩 나온다).
그래서 파생 데이터가 이미 모여 있는 PAAS_DOC_INDEX_DIR 아래에 둔다.
"""
import hashlib
import re
from pathlib import Path

from ..config import get_settings
from . import doctext

READY_DIRNAME = ".ready"

# 윈도우 MAX_PATH(260)에 여유를 둔 값. 사내 공유 폴더는 한글 폴더명이 길게 겹쳐서
# 원본 경로를 그대로 미러링하면 넘기는 경우가 있다 — 넘으면 해시 이름으로 떨어진다.
# (그래도 파일을 열면 front matter의 source로 어느 문서인지 알 수 있다.)
_MAX_PATH_CHARS = 240

_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n+", re.S)


def root() -> Path:
    return Path(get_settings().doc_index_dir) / READY_DIRNAME


def path_for(store_name: str, rel: str) -> Path:
    """캐시 파일 자리. 원본 경로를 그대로 미러링해 사람이 찾아 들어갈 수 있게 한다."""
    base = root() / store_name
    target = base / f"{rel}.md"
    if len(str(target)) > _MAX_PATH_CHARS:
        digest = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:20]
        target = base / f"{digest}.md"
    return target


def read(store_name: str, rel: str, source: Path) -> str:
    """마크다운 본문. 캐시가 원본과 맞으면 그대로, 아니면 추출하고 남긴다."""
    try:
        stat = source.stat()
    except OSError as e:
        raise doctext.ExtractError(f"파일을 읽을 수 없습니다: {e}")

    cached = _load(path_for(store_name, rel), stat.st_size, stat.st_mtime)
    if cached is not None:
        return cached

    markdown, _plain = doctext.extract(source)
    write(store_name, rel, source, markdown)
    return markdown


def write(store_name: str, rel: str, source: Path, markdown: str) -> Path | None:
    """캐시를 남긴다. 실패해도 조용히 넘어간다 — 캐시는 없어도 되는 것이고, 여기서
    예외를 올리면 색인·읽기가 통째로 실패한다."""
    try:
        stat = source.stat()
        target = path_for(store_name, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"---\nsource: {rel}\nsize: {stat.st_size}\nmtime: {stat.st_mtime:.6f}\n---\n\n"
            f"{markdown}\n",
            encoding="utf-8",
        )
        return target
    except OSError:
        return None


def forget(store_name: str, rel: str) -> None:
    """원본이 사라진 문서의 캐시를 지운다."""
    try:
        path_for(store_name, rel).unlink(missing_ok=True)
    except OSError:
        pass


def _load(target: Path, size: int, mtime: float) -> str | None:
    """front matter가 원본과 맞을 때만 본문을 돌려준다.

    파일 mtime만 보고 판정하지 않는 이유: 백업에서 되돌린 문서는 내용이 바뀌었는데도
    mtime이 캐시보다 옛날일 수 있다. 원본의 (크기, mtime)을 캐시 안에 적어 두고 그대로
    맞는지 본다 — 색인이 쓰는 판정과 같은 기준이다.
    """
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = _FRONT_MATTER_RE.match(raw)
    if not match:
        return None
    fields = dict(
        line.split(": ", 1) for line in match.group(1).split("\n") if ": " in line
    )
    if fields.get("size") != str(size) or fields.get("mtime") != f"{mtime:.6f}":
        return None
    return raw[match.end():].rstrip("\n")
