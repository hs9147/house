"""사내 문서 검색 — 추출한 본문을 캐시해 두고 부분 일치로 찾는다.

**왜 FTS5가 아니라 LIKE인가.** 측정해 보면 SQLite FTS5는 한국어에서 깨진다. 기본
unicode61 토크나이저는 공백으로 끊은 토큰만 잡아 `규정`으로 "규정은"을 못 찾고(조사가
붙으면 다른 토큰이다), trigram은 3글자 미만 질의를 아예 받지 못해 `정산`·`휴가`·`계약`
같은 2음절 키워드가 전부 탈락한다. 추출 텍스트를 한 테이블에 넣고 LIKE로 훑으면 부분
일치가 정확히 되고, 본문 6MB/5,000건 전체 스캔이 30ms였다(100MB급도 1초 안).

**색인은 파생 데이터다.** 원본은 디스크의 문서이고 이 파일은 언제든 지우고 다시 만들 수
있다. 그래서 플랫폼 DB(마이그레이션 대상)에 넣지 않고 저장소별 sqlite 파일로 분리한다.

**추출은 비싸다.** PDF 한 건에 수십~수백 ms가 든다. 그래서 색인은 한 번에 끝내지 않고
호출마다 시간 예산만큼만 진행하고 남은 개수를 돌려준다 — MCP 클라이언트의 요청 타임아웃
(30초)을 넘기지 않으면서 여러 번 불러 수렴시킨다.
"""
import re
import sqlite3
import time
from pathlib import Path

from ..config import get_settings
from . import docready, doctext

# 한 문서에서 색인에 담을 최대 글자 수. 뒷부분은 검색되지 않는 대신 색인 크기와 스캔
# 시간이 문서 수에 비례해서만 늘어난다(120,000자 ≈ 60쪽 분량).
MAX_INDEX_CHARS = 120_000
# 이보다 큰 파일은 열지 않는다 — 문서가 아니라 데이터·미디어일 가능성이 크고, 열어 보는
# 값이 비용을 넘지 못한다.
MAX_FILE_BYTES = 50 * 1024 * 1024
# 문서가 아닌 것이 확실한 확장자. 공유 폴더에는 이미지·미디어·압축이 섞여 있고, 이걸
# 걸러 두지 않으면 색인 시간의 대부분을 "열어 보고 실패하는 데" 쓴다.
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".ico", ".svg", ".webp",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".mkv", ".wav", ".flac",
    ".zip", ".7z", ".rar", ".gz", ".tar", ".iso",
    ".exe", ".dll", ".msi", ".bin", ".dat", ".db", ".lnk", ".tmp",
}
_DEFAULT_BUDGET = 20.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  path       TEXT PRIMARY KEY,
  size       INTEGER NOT NULL,
  mtime      REAL NOT NULL,
  body       TEXT,           -- NULL이면 추출 실패
  truncated  INTEGER NOT NULL DEFAULT 0,
  error      TEXT,           -- 실패 이유(사용자에게 그대로 보여 준다)
  indexed_at REAL NOT NULL
)
"""


def index_path(store_name: str) -> Path:
    directory = Path(get_settings().doc_index_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{store_name}.db"


def _connect(store_name: str) -> sqlite3.Connection:
    conn = sqlite3.connect(index_path(store_name))
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def _candidates(root: Path) -> list[tuple[str, int, float]]:
    """색인 대상 파일 목록 — (상대경로, 크기, mtime). stat만 하므로 싸다."""
    found: list[tuple[str, int, float]] = []
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        # 점으로 시작하는 폴더는 문서가 아니다(.ready 캐시, .git 등). .ready가 어쩌다
        # 저장소 안에 놓이면 **자기 캐시를 다시 색인**해 문서마다 결과가 둘씩 나온다.
        if any(part.startswith(".") for part in path.relative_to(root).parts[:-1]):
            continue
        try:
            stat = path.stat()
        except OSError:  # 권한·잠긴 파일은 조용히 건너뛴다(다음 색인에서 다시 시도)
            continue
        if stat.st_size > MAX_FILE_BYTES:
            continue
        found.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime))
    return found


def reindex(store_name: str, root: Path, *, force: bool = False,
            budget_seconds: float = _DEFAULT_BUDGET) -> dict:
    """바뀐 파일만 다시 추출해 색인을 갱신한다.

    예산을 넘기면 남은 개수를 `remaining`으로 돌려주고 멈춘다 — `done`이 false면 다시
    호출한다. 지워진 파일 정리는 예산과 무관하게 매번 한다: 목록 훑기(stat)는 싼 작업이라
    호출마다 전체를 보므로, 중간에 멈춰도 "디스크에 없는데 색인에 있는 것"은 정확히 알 수
    있다(비싼 것은 추출뿐이다).
    """
    started = time.monotonic()
    conn = _connect(store_name)
    try:
        known = {
            row["path"]: (row["size"], row["mtime"])
            for row in conn.execute("SELECT path, size, mtime FROM docs")
        }
        candidates = _candidates(root)
        todo = [
            item for item in candidates
            if force or known.get(item[0]) != (item[1], item[2])
        ]

        indexed = failed = 0
        for rel, size, mtime in todo:
            if time.monotonic() - started > budget_seconds:
                break
            body: str | None = None
            error: str | None = None
            try:
                # 추출은 한 번만 하고 두 곳에 쓴다 — 색인에는 평문, .ready에는 마크다운.
                markdown, body = doctext.extract(root / rel)
                docready.write(store_name, rel, root / rel, markdown)
            except doctext.ExtractError as e:
                error = str(e)
            except OSError as e:  # 색인 중 파일이 사라지거나 잠긴 경우
                error = f"파일을 읽을 수 없습니다: {e}"
            truncated = bool(body and len(body) > MAX_INDEX_CHARS)
            conn.execute(
                "INSERT INTO docs (path, size, mtime, body, truncated, error, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                "size=excluded.size, mtime=excluded.mtime, body=excluded.body, "
                "truncated=excluded.truncated, error=excluded.error, "
                "indexed_at=excluded.indexed_at",
                (rel, size, mtime, body[:MAX_INDEX_CHARS] if body else None,
                 int(truncated), error, time.time()),
            )
            if error:
                failed += 1
            else:
                indexed += 1

        gone = set(known) - {item[0] for item in candidates}
        for rel in gone:
            conn.execute("DELETE FROM docs WHERE path = ?", (rel,))
            docready.forget(store_name, rel)
        remaining = len(todo) - (indexed + failed)
        conn.commit()
        return {
            "files": len(candidates), "indexed": indexed, "failed": failed,
            "skipped": len(candidates) - len(todo), "removed": len(gone),
            "remaining": remaining, "done": remaining == 0,
        }
    finally:
        conn.close()


def status(store_name: str) -> dict:
    """색인 커버리지 — 확장자별로 몇 건이 읽혔고 못 읽은 것은 왜인지.

    "붙였는데 검색이 안 된다"의 원인이 대개 추출 실패(스캔 PDF, 97-2003 바이너리)라서,
    실패 이유를 묶어 함께 돌려준다.
    """
    conn = _connect(store_name)
    try:
        rows = conn.execute("SELECT path, body IS NOT NULL AS ok, error FROM docs").fetchall()
        by_suffix: dict[str, dict] = {}
        reasons: dict[str, int] = {}
        for row in rows:
            suffix = Path(row["path"]).suffix.lower() or "(확장자 없음)"
            bucket = by_suffix.setdefault(suffix, {"indexed": 0, "failed": 0})
            if row["ok"]:
                bucket["indexed"] += 1
            else:
                bucket["failed"] += 1
                # 이유 문구에 파일별 값(크기·경로)이 섞여 있어 앞부분만 묶는다
                reasons[str(row["error"])[:60]] = reasons.get(str(row["error"])[:60], 0) + 1
        total_chars = conn.execute(
            "SELECT coalesce(sum(length(body)), 0) FROM docs").fetchone()[0]
        return {
            "total": len(rows),
            "indexed": sum(b["indexed"] for b in by_suffix.values()),
            "failed": sum(b["failed"] for b in by_suffix.values()),
            "index_chars": total_chars,
            "by_suffix": dict(sorted(by_suffix.items())),
            "failure_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        }
    finally:
        conn.close()


def _like_pattern(term: str) -> str:
    """LIKE 메타문자를 중립화한다 — 사용자가 넣은 %·_가 와일드카드로 동작하면 안 된다."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search(store_name: str, query: str, limit: int = 10, *, width: int = 60,
           snippets_per_file: int = 3) -> dict:
    """공백으로 끊은 낱말을 **모두** 포함하는 문서를 찾아 발췌와 함께 돌려준다."""
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    if not terms:
        return {"query": query, "terms": [], "hits": [], "truncated": False}

    conn = _connect(store_name)
    try:
        where = " AND ".join(["body LIKE ? ESCAPE '\\'"] * len(terms))
        rows = conn.execute(
            f"SELECT path, body, truncated FROM docs WHERE body IS NOT NULL AND {where} "
            "ORDER BY path LIMIT ?",
            (*[_like_pattern(t) for t in terms], limit + 1),
        ).fetchall()
        hits = [
            {
                "path": row["path"],
                "truncated": bool(row["truncated"]),
                "snippets": _snippets(row["body"], terms[0], snippets_per_file, width),
            }
            for row in rows[:limit]
        ]
        return {
            "query": query, "terms": terms, "hits": hits,
            "truncated": len(rows) > limit,
        }
    finally:
        conn.close()


def _snippets(body: str, term: str, count: int, width: int) -> list[str]:
    """일치 지점 주변을 잘라 낸다 — 경로만 주면 어느 대목인지 확인하러 또 열어야 한다."""
    out: list[str] = []
    lowered, needle = body.lower(), term.lower()
    start = 0
    while len(out) < count:
        found = lowered.find(needle, start)
        if found < 0:
            break
        left, right = max(0, found - width), min(len(body), found + len(term) + width)
        text = " ".join(body[left:right].split())
        out.append(f"{'…' if left else ''}{text}{'…' if right < len(body) else ''}")
        start = found + len(term)
    return out
