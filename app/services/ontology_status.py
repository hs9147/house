"""온톨로지 전환 현황 — 문서가 그래프로 **얼마나 옮겨졌는지**를 센다.

온톨로지는 문서 색인의 파생물이다(services/ontology.extract → docsearch._write_graph). 그래서
"전환 현황"은 추측이 아니라 색인 DB에 이미 있는 수치다: 색인된 문서 몇 건이 본문 추출에
성공했고, 그중 몇 건이 노드를 만들었고, 그중 몇 건이 관계를 만들었는가.

**단계가 떨어지는 자리가 곧 고칠 자리다.**
  - 색인 → 추출 실패: 스캔 PDF·97-2003 바이너리(status의 failure_reasons가 이유를 말한다).
  - 추출 → 구조 0건: 본문은 읽혔지만 절·용어·표가 없다(제목도 표도 정의문도 없는 평문).
    검색에는 걸리지만 그래프에는 문서 노드 하나만 남는다 — 이 간격이 가장 조용한 실패다.
    **추출기는 문서마다 `document` 노드를 언제나 만든다**: "노드가 있다"로 전환을 세면 평문
    코퍼스도 100%로 보인다(실측에서 그렇게 나왔다). 그래서 구조 노드로만 센다.
  - 노드 → 관계 0건: 절·용어가 따로 떠 있고 인용·정의 관계가 없다.

**LLM을 쓰지 않는다.** 전부 SQL로 세는 사실이고, 화면이 숫자를 말할 때 근거를 파일 경로로
댈 수 있어야 한다(노드가 0건인 문서 목록을 함께 준다).
"""
from collections import Counter
from pathlib import Path

from . import docsearch
from . import storage as storage_service

# 화면에 실을 상한. 목록이 길어도 사람이 고치는 것은 앞쪽 몇 건이고, 나머지는 수치로 충분하다.
MAX_GAP_PATHS = 30
MAX_TABLE_SCHEMAS = 20
MAX_FAILURE_REASONS = 10
MAX_DAILY_DAYS = 30

# 문서당 **구조 노드**(절·용어·표) 수를 묶는 구간. 문서 노드는 세지 않는다 — 그것은 모든
# 문서에 하나씩 있으므로 "전환됐다"의 근거가 되지 못한다. 0은 별도 구간이다("적다"와 "없다").
NODE_BUCKETS = ((0, 0, "0 (전환 안 됨)"), (1, 5, "1-5"), (6, 20, "6-20"), (21, None, "21+"))


def _bucket(count: int) -> str:
    for low, high, label in NODE_BUCKETS:
        if count >= low and (high is None or count <= high):
            return label
    return "21+"


def store_summary(store_name: str) -> dict:
    """저장소 하나의 전환 현황. 색인 DB만 읽는다(디스크를 훑지 않는다)."""
    status = docsearch.status(store_name)
    conn = docsearch._connect(store_name)
    try:
        extracted = conn.execute(
            "SELECT COUNT(*) FROM docs WHERE body IS NOT NULL").fetchone()[0]
        truncated = conn.execute(
            "SELECT COUNT(*) FROM docs WHERE truncated = 1").fetchone()[0]
        last_indexed = conn.execute("SELECT MAX(indexed_at) FROM docs").fetchone()[0]
        node_kinds = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM nodes GROUP BY kind ORDER BY n DESC")}
        edge_kinds = {r["rel"]: r["n"] for r in conn.execute(
            "SELECT rel, COUNT(*) AS n FROM edges GROUP BY rel ORDER BY n DESC")}
        # **본문이 지금 읽히는 문서로 제한한다.** 노드는 문서 단위로 지우고 다시 쓰는데,
        # 예전에 성공했다가 지금은 추출이 실패하는 문서(스캔본으로 교체 등)의 노드가 남아
        # 있으면 "노드 생성"이 "추출 성공"보다 커진다 — 퍼널이 단조롭지 않으면 읽을 수 없다
        # (실측에서 바로 그렇게 나왔다). 남은 옛 노드는 숨기지 않고 stale_nodes로 따로 센다.
        # 전환 기준은 **구조 노드**다(kind != 'document').
        with_nodes = conn.execute(
            "SELECT COUNT(DISTINCT n.path) FROM nodes n JOIN docs d ON d.path = n.path"
            " AND d.body IS NOT NULL WHERE n.kind != 'document'").fetchone()[0]
        # 관계도 **구조가 있는 문서** 안에서만 센다. 구조 없이 관계만 생기는 경우가 실제로
        # 있다: 절이 없는 문서에서 인용이 발견되면 문서→(인용된)문서 관계가 생기는데, 그
        # 문서에는 구조 노드가 없다. 그걸 그대로 세면 "관계 생성"이 "구조 추출"보다 커져
        # 퍼널이 또 뒤집힌다(실측: 3,039 > 3,023).
        with_edges = conn.execute(
            "SELECT COUNT(DISTINCT e.path) FROM edges e JOIN docs d ON d.path = e.path"
            " AND d.body IS NOT NULL"
            " WHERE EXISTS (SELECT 1 FROM nodes n WHERE n.path = e.path"
            "               AND n.kind != 'document')").fetchone()[0]
        stale_nodes = conn.execute(
            "SELECT COUNT(*) FROM nodes n LEFT JOIN docs d ON d.path = n.path"
            " WHERE d.path IS NULL OR d.body IS NULL").fetchone()[0]
        # 문서당 구조 노드 수 — 본문이 읽힌 문서만 센다(추출 실패는 다른 단계의 문제다).
        per_doc = conn.execute(
            "SELECT d.path AS path,"
            " COUNT(CASE WHEN n.kind != 'document' THEN 1 END) AS n FROM docs d"
            " LEFT JOIN nodes n ON n.path = d.path"
            " WHERE d.body IS NOT NULL GROUP BY d.path"
        ).fetchall()
        buckets = Counter(_bucket(int(r["n"])) for r in per_doc)
        gaps = [r["path"] for r in per_doc if int(r["n"]) == 0][:MAX_GAP_PATHS]
        tables = [
            {"columns": r["name"].split(" | "), "documents": r["n"], "store": store_name}
            for r in conn.execute(
                "SELECT name, COUNT(DISTINCT path) AS n FROM nodes WHERE kind = 'table'"
                " GROUP BY name ORDER BY n DESC, name LIMIT ?", (MAX_TABLE_SCHEMAS,))
        ]
        daily = [
            {"date": r["day"], "documents": r["n"]}
            for r in conn.execute(
                "SELECT date(indexed_at, 'unixepoch') AS day, COUNT(*) AS n FROM docs"
                " GROUP BY day ORDER BY day DESC LIMIT ?", (MAX_DAILY_DAYS,))
        ][::-1]
    finally:
        conn.close()
    return {
        "store": store_name,
        "indexed": status["total"],
        "extracted": extracted,
        "extract_failed": status["failed"],
        "truncated": truncated,
        "with_nodes": with_nodes,
        "with_edges": with_edges,
        # 본문은 읽혔는데 **구조**가 없는 문서 — 가장 조용한 실패다(검색은 되고 그래프에는
        # 문서 노드 하나만 남는다).
        "no_nodes": max(extracted - with_nodes, 0),
        "nodes": sum(node_kinds.values()),
        "edges": sum(edge_kinds.values()),
        # 지금은 본문을 읽을 수 없는 문서에 매달린 노드 — 재색인하면 정리된다.
        "stale_nodes": stale_nodes,
        "node_kinds": node_kinds,
        "edge_kinds": edge_kinds,
        "node_buckets": {label: buckets.get(label, 0) for _, _, label in NODE_BUCKETS},
        "gap_paths": gaps,
        "table_schemas": tables,
        "failure_reasons": status["failure_reasons"],
        "by_suffix": status["by_suffix"],
        "last_indexed_at": last_indexed,
        "daily": daily,
    }


def overview() -> dict:
    """모든 저장소의 전환 현황 + 합계·퍼널.

    저장소 하나가 막혀도(색인 파일 손상 등) 나머지는 그대로 보여 준다 — 한 칸의 실패가
    화면 전체를 비우면 무엇이 되고 무엇이 안 되는지 알 수 없다.
    """
    summaries: list[dict] = []
    errors: list[dict] = []
    for store in storage_service.stores():
        try:
            summaries.append(store_summary(store.name))
        except Exception as e:  # noqa: BLE001
            errors.append({"store": store.name, "error": str(e)[:200]})

    totals = {
        key: sum(s[key] for s in summaries)
        for key in ("indexed", "extracted", "extract_failed", "truncated",
                    "with_nodes", "with_edges", "no_nodes", "nodes", "edges",
                    "stale_nodes")
    }
    node_kinds: Counter = Counter()
    edge_kinds: Counter = Counter()
    buckets: Counter = Counter()
    reasons: Counter = Counter()
    daily: Counter = Counter()
    for s in summaries:
        node_kinds.update(s["node_kinds"])
        edge_kinds.update(s["edge_kinds"])
        buckets.update(s["node_buckets"])
        reasons.update(s["failure_reasons"])
        for day in s["daily"]:
            daily[day["date"]] += day["documents"]

    tables = sorted(
        (t for s in summaries for t in s["table_schemas"]),
        key=lambda t: -t["documents"],
    )[:MAX_TABLE_SCHEMAS]

    # 퍼널 — 단계마다 **직전 단계의 부분집합**이어야 읽힌다(그래서 추출 성공 밑에 노드·관계).
    funnel = [
        {"stage": "색인된 문서", "count": totals["indexed"],
         "detail": "색인 DB에 행이 있는 문서(폴더에 있지만 아직 색인하지 않은 파일은 제외)"},
        {"stage": "본문 추출 성공", "count": totals["extracted"],
         "detail": "텍스트를 읽어 낸 문서 — 실패는 스캔 PDF·97-2003 바이너리가 대부분이다"},
        {"stage": "구조 추출", "count": totals["with_nodes"],
         "detail": "절·용어·표가 하나라도 뽑힌 문서 — 문서 노드는 모든 문서에 하나씩 생기므로 "
                   "전환의 근거로 세지 않는다"},
        {"stage": "관계 생성", "count": totals["with_edges"],
         "detail": "포함·정의·인용 관계가 하나라도 만들어진 문서"},
    ]
    return {
        "stores": summaries,
        "errors": errors,
        "totals": {
            **totals,
            "stores": len(summaries),
            "node_kinds": dict(node_kinds),
            "edge_kinds": dict(edge_kinds),
            "node_buckets": {label: buckets.get(label, 0) for _, _, label in NODE_BUCKETS},
            "conversion_rate": (
                round(totals["with_nodes"] / totals["extracted"] * 100, 1)
                if totals["extracted"] else 0.0
            ),
        },
        "funnel": funnel,
        "table_schemas": tables,
        "failure_reasons": dict(reasons.most_common(MAX_FAILURE_REASONS)),
        "daily": [{"date": d, "documents": n} for d, n in sorted(daily.items())],
    }


def index_file_size(store_name: str) -> int:
    """색인 파일 크기 — 화면이 "색인이 얼마나 커졌나"를 말할 때 쓴다."""
    path: Path = docsearch.index_path(store_name)
    return path.stat().st_size if path.exists() else 0
