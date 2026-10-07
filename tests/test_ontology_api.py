"""온톨로지 전환 현황 — 색인 DB를 센 값이고, 퍼널은 **단조**여야 읽힌다.

전환은 파이프라인이다(색인 → 본문 추출 → 노드 → 관계). 단계가 직전 단계의 부분집합이
아니면 "어디서 떨어졌나"를 읽을 수 없다 — 실측에서 바로 그 일이 났다: 지금은 본문을 못 읽는
문서의 옛 노드가 남아 "노드 생성"이 "추출 성공"보다 컸다.
"""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import docsearch, ontology_status

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


def _store(monkeypatch, tmp_path, name="docs"):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAAS_DOC_ROOTS", str(root))
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    get_settings.cache_clear()
    return root


def test_funnel_is_monotone_even_with_stale_nodes(monkeypatch, tmp_path, fresh_settings):
    """옛 노드가 남아 있어도 퍼널은 단조롭다 — 그 노드는 stale_nodes로 따로 센다."""
    root = _store(monkeypatch, tmp_path)
    (root / "규정.md").write_text(
        "# 복무규정\n## 제2조(정의)\n'연차'란 쉬는 날을 말한다.\n\n"
        "| 구분 | 대상 |\n|---|---|\n| 가 | 나 |\n", encoding="utf-8")
    docsearch.reindex("docs", root)

    # 본문을 못 읽는 상태로 바꿔 놓는다(스캔본으로 교체된 상황) — 노드는 그대로 남는다.
    conn = docsearch._connect("docs")
    try:
        conn.execute("UPDATE docs SET body = NULL, error = '추출 실패(테스트)'")
        conn.commit()
    finally:
        conn.close()

    summary = ontology_status.store_summary("docs")
    assert summary["extracted"] == 0
    assert summary["with_nodes"] == 0          # 본문이 없으면 전환으로 세지 않는다
    assert summary["stale_nodes"] > 0          # 남은 옛 노드는 숨기지 않는다
    assert summary["nodes"] > 0

    counts = [stage["count"] for stage in ontology_status.overview()["funnel"]]
    assert counts == sorted(counts, reverse=True), counts


def test_overview_counts_conversion_and_gaps(monkeypatch, tmp_path, fresh_settings):
    """구조가 없는 평문은 검색에는 걸리지만 그래프에는 아무것도 남지 않는다 —
    그 간격(no_nodes)과 해당 문서 경로를 함께 준다. 숫자만으로는 고칠 수 없다."""
    root = _store(monkeypatch, tmp_path)
    (root / "규정.md").write_text(
        "# 복무규정\n## 제2조(정의)\n'연차'란 쉬는 날을 말한다.\n", encoding="utf-8")
    (root / "메모.txt").write_text("제목도 표도 정의문도 없는 평문입니다.\n", encoding="utf-8")
    docsearch.reindex("docs", root)

    out = ontology_status.overview()
    t = out["totals"]
    assert t["indexed"] == 2 and t["extracted"] == 2
    assert t["with_nodes"] == 1
    assert t["no_nodes"] == 1
    assert any("메모.txt" in p for s in out["stores"] for p in s["gap_paths"])
    assert t["node_kinds"].get("term", 0) >= 1      # 정의문 → 용어
    assert t["edge_kinds"].get("defines", 0) >= 1
    assert t["conversion_rate"] == 50.0
    assert t["node_buckets"]["0 (전환 안 됨)"] == 1


def test_endpoints_read_only_and_admin_reindex(monkeypatch, tmp_path, fresh_settings):
    root = _store(monkeypatch, tmp_path)
    (root / "규정.md").write_text("# 규정\n## 제1조(목적)\n목적.\n", encoding="utf-8")
    c = TestClient(create_app())

    body = c.get(f"{API}/ontology/overview", headers=ADMIN).json()
    assert [s["stage"] for s in body["funnel"]] == ["색인된 문서", "본문 추출 성공", "구조 추출", "관계 생성"]
    # 조회는 아무것도 바꾸지 않는다 — 색인하지 않았으면 0이다.
    assert body["totals"]["indexed"] == 0

    done = c.post(f"{API}/ontology/stores/docs/reindex", headers=ADMIN)
    assert done.status_code == 200, done.text
    after = c.get(f"{API}/ontology/overview", headers=ADMIN).json()
    assert after["totals"]["indexed"] == 1
    assert after["totals"]["with_nodes"] == 1

    assert c.get(f"{API}/ontology/stores/없는저장소", headers=ADMIN).status_code == 404
    assert c.get(f"{API}/ontology/overview").status_code == 401


def test_relations_are_counted_inside_documents_that_have_structure(monkeypatch, tmp_path,
                                                                    fresh_settings):
    """실측: 절이 없는 문서에서 인용만 발견되면 문서→문서 관계가 생긴다 — 구조는 없는데
    관계는 있는 상태다. 그걸 그대로 세면 퍼널이 뒤집힌다(관계 3,039 > 구조 3,023)."""
    root = _store(monkeypatch, tmp_path)
    # 제목·표·정의문은 없고 인용만 있는 평문
    (root / "인용만.md").write_text(
        "별첨 「구매 지침」을 따른다.\n", encoding="utf-8")
    docsearch.reindex("docs", root)

    summary = ontology_status.store_summary("docs")
    assert summary["with_nodes"] == 0           # 구조 노드가 없다
    assert summary["with_edges"] == 0           # 그러니 관계도 세지 않는다
    counts = [stage["count"] for stage in ontology_status.overview()["funnel"]]
    assert counts == sorted(counts, reverse=True), counts


def test_search_groups_the_same_name_across_documents(monkeypatch, tmp_path, fresh_settings):
    """같은 양식이 여러 문서에 되풀이되는 것이 사내 문서의 기본 모양이다 — 묶지 않으면
    검색 결과가 한 이름으로 가득 찬다(실측: 견적서 표 스키마 하나가 320개 문서)."""
    root = _store(monkeypatch, tmp_path)
    table = "| 구분 | 금액 |\n|---|---|\n| 자재 | 100 |\n"
    for i in range(3):
        (root / f"견적{i}.md").write_text(f"# 견적 {i}\n{table}", encoding="utf-8")
    docsearch.reindex("docs", root)
    c = TestClient(create_app())

    body = c.get(f"{API}/ontology/stores/docs/graph",
                 params={"kind": "table"}, headers=ADMIN).json()
    assert len(body["nodes"]) == 1, body
    assert body["nodes"][0]["name"] == "구분 | 금액"
    assert body["nodes"][0]["documents"] == 3


def test_neighbors_expand_one_step_and_report_truncation(monkeypatch, tmp_path, fresh_settings):
    """탐색은 한 걸음씩이다 — 상한을 넘으면 조용히 버리지 않고 알린다."""
    root = _store(monkeypatch, tmp_path)
    sections = "".join(f"## 제{i}조(항목{i})\n내용.\n" for i in range(1, 6))
    (root / "규정.md").write_text(f"# 복무규정\n{sections}", encoding="utf-8")
    docsearch.reindex("docs", root)
    c = TestClient(create_app())

    body = c.get(f"{API}/ontology/stores/docs/graph/neighbors",
                 params={"kind": "document", "name": "규정"}, headers=ADMIN).json()
    assert body["node"]["documents"] == 1
    assert body["node"]["paths"] == ["규정.md"]
    # 문서가 바로 품는 것은 최상위 절 하나다 — 조문은 그 절 아래로 접힌다(계층이 남는다).
    assert [(n["rel"], n["kind"], n["name"]) for n in body["out"]] \
        == [("contains", "section", "복무규정")]
    assert body["in"] == [] and body["truncated"] is False

    step = c.get(f"{API}/ontology/stores/docs/graph/neighbors",
                 params={"kind": "section", "name": "복무규정"}, headers=ADMIN).json()
    assert [n["rel"] for n in step["out"]] == ["contains"] * 5
    # 들어오는 쪽도 함께 준다 — 어디에서 왔는지가 보여야 길을 되짚을 수 있다.
    assert [(n["rel"], n["kind"]) for n in step["in"]] == [("contains", "document")]

    few = c.get(f"{API}/ontology/stores/docs/graph/neighbors",
                params={"kind": "section", "name": "복무규정", "limit": 2}, headers=ADMIN).json()
    assert len(few["out"]) == 2 and few["truncated"] is True

    # 이름 없이는 부를 수 없고(422), 없는 저장소는 404다.
    assert c.get(f"{API}/ontology/stores/docs/graph/neighbors",
                 params={"kind": "document", "name": ""}, headers=ADMIN).status_code == 422
    assert c.get(f"{API}/ontology/stores/x/graph/neighbors",
                 params={"kind": "document", "name": "규정"}, headers=ADMIN).status_code == 404
