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
import os
import re
import sqlite3
import time
from pathlib import Path
from stat import S_ISREG

from ..config import get_settings
from . import docready, doctext, ontology

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
    # 오피스가 남기는 백업·잠금 부산물 — 본문의 사본이거나 사용자 이름 몇 바이트다.
    ".wbk", ".xlk", ".bak", ".laccdb", ".ldb",
    # 메일 보관 파일. 문서가 아니고 크기도 GB 단위다.
    ".pst", ".ost",
}
# 이름 앞자리만 보고 거르는 것들.
#   ~$규정.docx  워드·엑셀·파워포인트가 **문서를 열고 있는 동안** 만드는 잠금 파일.
#                162바이트짜리 바이너리에 연 사람의 계정명만 들어 있다.
#   ~WRL0001.tmp 워드 임시 파일.
#   .DS_Store    맥에서 공유 폴더를 열면 생긴다. ._규정.docx(리소스 포크)도 같이 걸린다.
# 걸러 두지 않으면 색인이 이것들을 열어 보다 실패하고, index_status의 실패 목록이
# 이걸로 덮여 진짜 문제(스캔 PDF, 97-2003 파일)가 묻힌다.
SKIP_PREFIXES = ("~$", ".")
SKIP_NAMES = {"desktop.ini", "thumbs.db"}
# 들어가지도 않을 폴더(소문자 비교). 지운 파일이 통째로 들어 있는 휴지통을 훑는 것만으로
# 색인 예산을 다 쓴다.
SKIP_DIR_NAMES = {"$recycle.bin", "system volume information", "found.000"}
_DEFAULT_BUDGET = 20.0

# 온톨로지는 색인과 **같은 수명**을 갖는다 — 같은 추출에서 나오고, 문서가 지워지면 함께
# 지워지며, 통째로 지우고 다시 만들 수 있다. 그래서 같은 파일에 둔다(services/ontology.py).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  path       TEXT PRIMARY KEY,
  size       INTEGER NOT NULL,
  mtime      REAL NOT NULL,
  body       TEXT,           -- NULL이면 추출 실패
  truncated  INTEGER NOT NULL DEFAULT 0,
  error      TEXT,           -- 실패 이유(사용자에게 그대로 보여 준다)
  indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
  path   TEXT NOT NULL,      -- 이 노드를 만든 문서 — 삭제·재색인의 단위다
  key    TEXT NOT NULL,      -- 문서 안에서의 식별자(ontology._key)
  kind   TEXT NOT NULL,      -- document | section | term | table
  name   TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  depth  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (path, key)
);
CREATE TABLE IF NOT EXISTS edges (
  path TEXT NOT NULL,
  src  TEXT NOT NULL,
  rel  TEXT NOT NULL,        -- contains | defines | references
  dst  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS ix_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS ix_edges_path ON edges(path)
"""


def index_path(store_name: str) -> Path:
    directory = Path(get_settings().doc_index_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{store_name}.db"


def _connect(store_name: str) -> sqlite3.Connection:
    conn = sqlite3.connect(index_path(store_name))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def skip_dir(name: str) -> bool:
    """들어가지 않을 폴더. 점으로 시작하는 폴더에는 .ready 캐시가 들어갈 수 있는데,
    그걸 색인하면 **자기 캐시를 다시 색인**해 문서마다 결과가 둘씩 나온다."""
    return name.startswith(".") or name.lower() in SKIP_DIR_NAMES


def skip_file(name: str) -> bool:
    """열어 보기 전에 이름만으로 거를 수 있는 것."""
    return (name.startswith(SKIP_PREFIXES)
            or name.lower() in SKIP_NAMES
            or Path(name).suffix.lower() in SKIP_SUFFIXES)


def _candidates(root: Path) -> tuple[list[tuple[str, int, float]], list[str]]:
    """색인 대상 파일 목록 — (상대경로, 크기, mtime)과 **훑지 못한 폴더 경로들**.

    rglob이 아니라 os.walk을 쓰는 이유는 **가지치기**다. 걸러야 할 폴더는 목록에서 빼는
    것으로는 부족하고 들어가지 않아야 한다 — 휴지통에는 지운 파일이 통째로 들어 있어서
    훑는 것만으로 색인 예산을 다 쓴다.

    훑지 못한 폴더는 세지 말고 **어디인지** 돌려줘야 한다. "목록에 없다"를 그대로 "지웠다"로
    받으면, 네트워크 드라이브가 잠깐 끊기거나 하위 폴더 권한이 막힌 것만으로 그 아래
    문서가 색인에서 통째로 사라진다(그리고 다시 붙었을 때 수천 건을 다시 추출해야 한다).
    호출자는 이 목록으로 "못 본 것"과 "없어진 것"을 갈라 낸다.
    """
    found: list[tuple[str, int, float]] = []
    unreadable: list[str] = []
    if not root.is_dir():
        # 루트 자체가 안 보인다 — 드라이브가 끊겼거나 경로가 틀렸다. 빈 목록을 "전부
        # 지워졌다"로 읽으면 안 되므로 루트를 못 읽은 폴더로 올린다.
        return found, [str(root)]

    def note(error: OSError) -> None:
        unreadable.append(str(getattr(error, "filename", "") or root))

    for dirpath, dirnames, filenames in os.walk(root, onerror=note):
        dirnames[:] = [d for d in dirnames if not skip_dir(d)]
        for name in filenames:
            if skip_file(name):
                continue
            path = Path(dirpath) / name
            try:
                info = path.stat()
            except OSError:  # 권한·잠긴 파일은 건너뛴다(다음 색인에서 다시 시도)
                continue
            if not S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                continue
            found.append((path.relative_to(root).as_posix(), info.st_size, info.st_mtime))
    # 순서를 고정한다 — 예산이 모자라 중간에 멈춰도 다음 호출이 같은 자리에서 이어진다.
    found.sort()
    return found, unreadable


def _vanished(known: dict, candidates: list, root: Path,
              unreadable: list[str]) -> list[str]:
    """색인에는 있는데 디스크에서 **정말로 사라진** 것.

    훑지 못한 폴더 아래는 제외한다 — 못 본 것과 없어진 것은 다르다. 이 구분이 없으면
    공유 드라이브가 잠깐 끊긴 사이에 색인이 통째로 비워지고, 다시 붙었을 때 수천 건을
    처음부터 다시 추출해야 한다(문서 하나에 수십~수백 ms다).
    """
    missing = set(known) - {item[0] for item in candidates}
    if not unreadable:
        return sorted(missing)

    blocked: list[str] = []
    for raw in unreadable:
        try:
            rel = Path(raw).resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return []  # 어디인지 특정할 수 없으면 아무것도 지우지 않는다
        if rel == Path("."):
            return []  # 루트를 통째로 못 읽었다
        blocked.append(rel.as_posix() + "/")
    return sorted(rel for rel in missing if not rel.startswith(tuple(blocked)))


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
        candidates, unreadable = _candidates(root)
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
                # 추출은 한 번만 하고 세 곳에 쓴다 — 색인에는 평문, .ready에는 마크다운,
                # 그리고 그 마크다운에서 온톨로지(그래프)를 뽑는다. 같은 추출을 세 번
                # 하지 않으려고 여기서 함께 처리한다.
                markdown, body = doctext.extract(root / rel)
                docready.write(store_name, rel, root / rel, markdown)
                _write_graph(conn, rel, markdown)
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

        gone = _vanished(known, candidates, root, unreadable)
        for rel in gone:
            conn.execute("DELETE FROM docs WHERE path = ?", (rel,))
            _forget_graph(conn, rel)
            docready.forget(store_name, rel)
        remaining = len(todo) - (indexed + failed)
        conn.commit()
        result = {
            "files": len(candidates), "indexed": indexed, "failed": failed,
            "skipped": len(candidates) - len(todo), "removed": len(gone),
            "remaining": remaining, "done": remaining == 0,
        }
        if unreadable:
            # 훑지 못한 폴더가 있으면 알린다. 그 아래 문서는 색인에 그대로 남겨 두므로
            # (지운 것이 아니라 못 본 것이다) files 수치는 실제보다 작게 나온다.
            result["unreadable_dirs"] = len(unreadable)
        return result
    finally:
        conn.close()


def _forget_graph(conn: sqlite3.Connection, rel: str) -> None:
    conn.execute("DELETE FROM nodes WHERE path = ?", (rel,))
    conn.execute("DELETE FROM edges WHERE path = ?", (rel,))


def _write_graph(conn: sqlite3.Connection, rel: str, markdown: str) -> None:
    """이 문서의 그래프를 갈아 끼운다 — 부분 갱신이 아니라 통째로 다시 쓴다.

    문서가 바뀌면 절이 통째로 사라지거나 이름이 바뀐다. 남은 것만 지우려 들면 어느 노드가
    옛것인지 판정하는 규칙이 하나 더 필요해지는데, 문서 하나의 노드는 수백 개 이하라
    지우고 다시 넣는 편이 싸고 정확하다.
    """
    _forget_graph(conn, rel)
    title = Path(rel).stem
    nodes, edges = ontology.extract(rel, title, markdown)
    conn.executemany(
        "INSERT OR REPLACE INTO nodes (path, key, kind, name, detail, depth)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(rel, f"{n['kind']}:{n['name']}", n["kind"], n["name"], n["detail"], n["depth"])
         for n in nodes],
    )
    conn.executemany(
        "INSERT INTO edges (path, src, rel, dst) VALUES (?, ?, ?, ?)",
        [(rel, e["src"], e["rel"], e["dst"]) for e in edges],
    )


def graph_schema(store_name: str) -> dict:
    """이 저장소 그래프에 **무엇이 있는지**. 찾기 전에 먼저 보는 자리다.

    표의 컬럼 이름 묶음을 함께 준다 — 사내 문서에서 되풀이되는 표(점검표·대장·양식)는
    사실상 레코드 타입이고, 그 머리글이 곧 스키마다.
    """
    conn = _connect(store_name)
    try:
        kinds = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM nodes GROUP BY kind ORDER BY n DESC")}
        rels = {r["rel"]: r["n"] for r in conn.execute(
            "SELECT rel, COUNT(*) AS n FROM edges GROUP BY rel ORDER BY n DESC")}
        tables = [
            {"columns": r["name"].split(" | "), "documents": r["n"]}
            for r in conn.execute(
                "SELECT name, COUNT(DISTINCT path) AS n FROM nodes WHERE kind = 'table'"
                " GROUP BY name ORDER BY n DESC, name LIMIT 50")
        ]
        return {"store": store_name, "node_kinds": kinds, "edge_kinds": rels,
                "documents": kinds.get("document", 0), "table_schemas": tables}
    finally:
        conn.close()


def find_nodes(store_name: str, kind: str = "", q: str = "", limit: int = 20) -> list[dict]:
    """이름으로 노드를 찾는다. kind를 주면 그 종류만."""
    sql = "SELECT path, kind, name, detail, depth FROM nodes WHERE 1=1"
    args: list = []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if q:
        sql += " AND name LIKE ? ESCAPE '\\'"
        args.append(_like_pattern(q))
    sql += " ORDER BY kind, name LIMIT ?"
    args.append(limit)
    conn = _connect(store_name)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def neighbors(store_name: str, kind: str, name: str, limit: int = 30) -> dict:
    """이 노드에 붙은 엣지 — 나가는 것과 들어오는 것. 문서 경로를 함께 준다."""
    key = f"{kind}:{name}"
    conn = _connect(store_name)
    try:
        def rows(column: str, other: str):
            return [
                {"path": r["path"], "rel": r["rel"], "kind": r["k"], "name": r["n"],
                 "detail": r["d"]}
                for r in conn.execute(
                    f"SELECT e.path, e.rel, n.kind AS k, n.name AS n, n.detail AS d"
                    f" FROM edges e LEFT JOIN nodes n"
                    f"   ON n.path = e.path AND n.key = e.{other}"
                    f" WHERE e.{column} = ? LIMIT ?", (key, limit))
            ]
        return {"node": {"kind": kind, "name": name},
                "out": rows("src", "dst"), "in": rows("dst", "src")}
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
