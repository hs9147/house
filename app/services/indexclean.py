"""색인 클린징 — 색인 디렉터리에 남은 **파생 데이터 찌꺼기**를 찾아 지운다(원본 문서는 건드리지 않는다).

색인·.ready·형식 그래프·내용 그래프는 모두 문서에서 다시 만들 수 있는 파생물이다. 그런데 만든 근거가 사라져도
저절로 지워지지 않는 것이 있다:

  orphan_index   색인 대상이 아닌 저장소의 색인 파일(<이름>.db) — 내부 저장소(internal)를 색인 대상에서 뺀 뒤의
                 internal.db, 지우거나 이름을 바꾼 저장소의 것. 그 안의 내용 분석(LLM 결과)도 함께 사라진다
  orphan_ready   그런 저장소의 .ready 캐시 폴더
  stale_nodes    본문을 읽을 수 없거나 색인에서 빠진 문서에 남은 형식 그래프 노드·관계(전환 현황의 「옛 노드」)
  stale_semantic 색인에서 빠진 문서에 남은 내용 그래프(sem_*) — 원본이 지워졌는데 분석을 다시 돌리지 않은 경우
  stale_ready    색인 대상 저장소 안에서, 색인에 없는 문서의 .ready 파일

preview와 apply가 **같은 함수**다(apply=False면 세기만 한다) — 화면이 보여 준 목록과 실제로 지우는 것이 어긋나지
않게.
"""
import shutil
from pathlib import Path

from ..config import get_settings
from . import docready, docsearch
from . import storage as storage_service


def _index_dir() -> Path:
    return Path(get_settings().doc_index_dir)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


def cleanse(apply: bool = False) -> dict:
    """찌꺼기를 센다(apply=False) 또는 지운다(apply=True). 저장소별·종류별 개수와 크기를 돌려준다."""
    names = {s.name for s in storage_service.indexed_stores()}
    index_dir = _index_dir()
    out: dict = {"applied": apply, "orphan_index": [], "orphan_ready": [], "stores": {}}

    # 1. 색인 대상이 아닌 저장소의 색인 파일 — WAL·SHM 파일도 함께
    for db_file in sorted(index_dir.glob("*.db")) if index_dir.is_dir() else []:
        if db_file.stem in names:
            continue
        files = [db_file] + [db_file.with_name(db_file.name + tail) for tail in ("-wal", "-shm", "-journal")]
        files = [f for f in files if f.exists()]
        entry = {"store": db_file.stem, "bytes": sum(_size(f) for f in files)}
        out["orphan_index"].append(entry)
        if apply:
            try:
                for f in files:
                    f.unlink(missing_ok=True)
            except OSError as e:   # 윈도에서는 열려 있는 파일을 지울 수 없다 — 숨기지 않고 알린다
                entry["error"] = f"지우지 못했습니다(사용 중일 수 있습니다): {e}"[:200]

    # 2. 그런 저장소의 .ready 폴더
    ready_root = docready.root()
    for folder in sorted(p for p in ready_root.iterdir() if p.is_dir()) if ready_root.is_dir() else []:
        if folder.name in names:
            continue
        out["orphan_ready"].append({"store": folder.name, "bytes": _size(folder)})
        if apply:
            shutil.rmtree(folder, ignore_errors=True)

    # 3·4·5. 색인 대상 저장소 안의 찌꺼기
    for name in sorted(names):
        if not docsearch.index_path(name).exists():
            continue
        conn = docsearch._connect(name)
        try:
            gone_docs = "(SELECT path FROM docs WHERE body IS NOT NULL)"
            stale_nodes = conn.execute(f"SELECT COUNT(*) FROM nodes WHERE path NOT IN {gone_docs}").fetchone()[0]
            stale_edges = conn.execute(f"SELECT COUNT(*) FROM edges WHERE path NOT IN {gone_docs}").fetchone()[0]
            have_semantic = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sem_nodes'").fetchone() is not None
            stale_semantic = 0
            if have_semantic:
                stale_semantic = sum(conn.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE path NOT IN (SELECT path FROM docs)").fetchone()[0]
                    for t in ("sem_nodes", "sem_edges", "sem_docs"))
            known = {docready.path_for(name, r["path"]).resolve() for r in conn.execute("SELECT path FROM docs")}
            folder = ready_root / name
            stale_ready = [f for f in folder.rglob("*.md") if f.resolve() not in known] if folder.is_dir() else []
            stale_ready_bytes = sum(f.stat().st_size for f in stale_ready)
            if apply:
                conn.execute(f"DELETE FROM nodes WHERE path NOT IN {gone_docs}")
                conn.execute(f"DELETE FROM edges WHERE path NOT IN {gone_docs}")
                if have_semantic:
                    for t in ("sem_nodes", "sem_edges", "sem_docs"):
                        conn.execute(f"DELETE FROM {t} WHERE path NOT IN (SELECT path FROM docs)")
                conn.commit()
                for f in stale_ready:
                    f.unlink(missing_ok=True)
        finally:
            conn.close()
        found = {"stale_nodes": stale_nodes, "stale_edges": stale_edges, "stale_semantic": stale_semantic,
                 "stale_ready": len(stale_ready), "stale_ready_bytes": stale_ready_bytes}
        if any(found.values()):
            out["stores"][name] = found
    out["total"] = (len(out["orphan_index"]) + len(out["orphan_ready"])
                    + sum(v["stale_nodes"] + v["stale_edges"] + v["stale_semantic"] + v["stale_ready"]
                          for v in out["stores"].values()))
    return out
