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
