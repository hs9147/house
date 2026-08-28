"""문서 온톨로지 — `.ready`를 만들 때 함께 뽑는 그래프와 스키마(정적 파싱, LLM 없음)."""
import json

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import docsearch, ontology

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"

REGULATION = """# 임직원 복무규정

## 제1조(목적)

이 규정은 임직원의 복무에 관한 사항을 정함을 목적으로 한다.

## 제2조(정의)

"연차유급휴가"란 근로기준법에 따라 부여되는 유급휴가를 말한다.
인사위원회(이하 "위원회"라 한다)는 이 규정의 해석을 담당한다.

## 제3조(적용범위)

세부 절차는 「복무관리 지침」에 따른다.

### 제3조의2(예외)

| 구분 | 대상 | 비고 |
|---|---|---|
| 정규직 | 전원 | |
"""


def _store(monkeypatch, tmp_path, name="rules", body=REGULATION, filename="복무규정.md"):
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / filename).write_text(body, encoding="utf-8")
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("PAAS_DOC_ROOTS", f"{name}={docs}")
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    get_settings.cache_clear()
    docsearch.reindex(name, docs)
    return docs


# --- 추출 ---

def test_headings_become_a_section_tree(monkeypatch, tmp_path, fresh_settings):
    """docx 제목 단계가 마크다운 `#`으로 남아 있어서 계층은 추정이 아니라 사실이다."""
    _store(monkeypatch, tmp_path)
    sections = {n["name"]: n for n in docsearch.find_nodes("rules", kind="section")}
    assert "제2조(정의)" in sections
    assert sections["제2조(정의)"]["detail"] == "제2조"       # 조문 번호를 읽어 둔다
    assert sections["제3조의2(예외)"]["depth"] == 3

    # 하위 절은 문서가 아니라 상위 절에 달린다
    nb = docsearch.neighbors("rules", "section", "제3조(적용범위)")
    assert any(e["name"] == "제3조의2(예외)" and e["rel"] == "contains" for e in nb["out"])


def test_korean_definition_shapes_become_terms(monkeypatch, tmp_path, fresh_settings):
    """사내 규정의 정의문은 형태가 일정하다 — `"X"란 …`과 `(이하 "Y"라 한다)`."""
    _store(monkeypatch, tmp_path)
    terms = {n["name"]: n["detail"] for n in docsearch.find_nodes("rules", kind="term")}
    assert "연차유급휴가" in terms
    assert "유급휴가를 말한다" in terms["연차유급휴가"]
    assert "위원회" in terms          # 줄임말도 용어다 — 이후 본문은 이것만 쓴다

    # 용어는 그것을 정의한 절에 달린다 — "어느 규정이 이 말을 정의했나"가 그래프 질문이다
    nb = docsearch.neighbors("rules", "term", "연차유급휴가")
    assert [e["name"] for e in nb["in"]] == ["제2조(정의)"]


def test_table_headers_are_the_schema(monkeypatch, tmp_path, fresh_settings):
    """되풀이되는 표는 사실상 레코드 타입이고 그 머리글이 스키마다."""
    _store(monkeypatch, tmp_path)
    schema = docsearch.graph_schema("rules")
    assert schema["table_schemas"][0]["columns"] == ["구분", "대상", "비고"]
    assert schema["node_kinds"]["table"] == 1
    assert schema["edge_kinds"]["defines"] == 2


def test_quoted_document_names_become_references(monkeypatch, tmp_path, fresh_settings):
    """「」로 인용한 문서는 본문 검색으로는 관계가 안 보인다 — 엣지로 남긴다."""
    _store(monkeypatch, tmp_path)
    nb = docsearch.neighbors("rules", "section", "제3조(적용범위)")
    assert any(e["rel"] == "references" and e["name"] == "복무관리 지침" for e in nb["out"])


def test_extraction_makes_no_llm_call(monkeypatch, tmp_path, fresh_settings):
    """색인 루프는 20초 예산으로 수천 건을 돈다 — 문서마다 LLM을 부르면 처리량이 무너진다."""
    from app.services import httpx_retry

    def boom(*a, **kw):
        raise AssertionError("온톨로지 추출이 밖으로 나갔다")

    monkeypatch.setattr(httpx_retry.httpx, "post", boom)
    monkeypatch.setattr(httpx_retry.httpx, "get", boom)
    _store(monkeypatch, tmp_path)
    assert docsearch.find_nodes("rules", kind="term")


# --- 색인과 같은 수명 ---

def test_graph_is_rewritten_and_removed_with_the_document(monkeypatch, tmp_path,
                                                          fresh_settings):
    """그래프는 파생 데이터다 — 문서가 바뀌면 갈아 끼우고, 사라지면 함께 지운다."""
    docs = _store(monkeypatch, tmp_path)
    assert docsearch.find_nodes("rules", kind="term")

    (docs / "복무규정.md").write_text('# 개정판\n\n"성과급"이란 상여를 말한다.\n',
                                      encoding="utf-8")
    docsearch.reindex("rules", docs)
    terms = [n["name"] for n in docsearch.find_nodes("rules", kind="term")]
    assert terms == ["성과급"]        # 옛 용어가 남아 있으면 갈아 끼운 것이 아니다

    (docs / "복무규정.md").unlink()
    docsearch.reindex("rules", docs)
    assert docsearch.find_nodes("rules") == []


# --- MCP 서버 ---

def _rpc(c, tool, args=None):
    return c.post(f"{API}/mcp/graph", headers=ADMIN, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args or {}},
    }).json()


def _text(reply):
    return reply["result"]["content"][0]["text"]


def test_graph_server_answers_across_stores(monkeypatch, tmp_path, fresh_settings):
    _store(monkeypatch, tmp_path)
    c = TestClient(create_app())

    tools = {t["name"] for t in c.post(f"{API}/mcp/graph", headers=ADMIN, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
    }).json()["result"]["tools"]}
    assert tools == {"graph_schema", "find_nodes", "neighbors"}

    schema = json.loads(_text(_rpc(c, "graph_schema")))
    assert schema["rules"]["table_schemas"][0]["columns"] == ["구분", "대상", "비고"]

    hits = json.loads(_text(_rpc(c, "find_nodes", {"kind": "term", "q": "연차"})))
    assert hits[0]["source"] == "rules"
    assert hits[0]["path"] == "복무규정.md"

    nb = json.loads(_text(_rpc(c, "neighbors", {"kind": "term", "name": "연차유급휴가"})))
    assert nb[0]["in"][0]["name"] == "제2조(정의)"


def test_graph_server_distinguishes_empty_from_missing(monkeypatch, tmp_path,
                                                       fresh_settings):
    """색인하지 않은 것과 "그런 노드가 없는 것"은 다른 문제다."""
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    get_settings.cache_clear()
    c = TestClient(create_app())
    assert "reindex_docs" in _rpc(c, "graph_schema")["error"]["message"]

    _store(monkeypatch, tmp_path)
    c = TestClient(create_app())
    assert json.loads(_text(_rpc(c, "find_nodes", {"q": "없는말"}))) == []
    assert "정확히 확인" in _rpc(c, "neighbors",
                              {"kind": "term", "name": "없는말"})["error"]["message"]


def test_graph_server_is_listed_in_the_directory(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_MCP_INTERNAL_BASE_URL", "http://localhost:7000/paas")
    _store(monkeypatch, tmp_path)
    c = TestClient(create_app())
    items = {i["id"]: i for i in c.get(f"{API}/mcp/search", headers=ADMIN).json()}
    assert items["paas-graph"]["url"].endswith("/mcp/graph")


def test_one_document_cannot_flood_the_graph():
    """표가 수백 개인 엑셀 한 건이 그래프를 통째로 먹으면 안 된다."""
    body = "\n\n".join(f"| a{i} | b{i} |\n|---|---|\n| x | y |" for i in range(600))
    nodes, _edges = ontology.extract("big.xlsx", "big", body)
    assert len(nodes) == ontology.MAX_NODES_PER_DOC
