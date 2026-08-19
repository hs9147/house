"""사내 문서 검색 — 색인(증분·예산·정리), 한국어 부분 일치, 커버리지 보고."""
import time

import pytest

from app.config import get_settings
from app.services import docsearch
from tests.test_doctext import _docx, _pdf, _xlsx_inline


@pytest.fixture
def docs(monkeypatch, tmp_path, fresh_settings):
    """문서 폴더와 색인 자리를 tmp_path 안으로 옮긴다."""
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    get_settings.cache_clear()
    root = tmp_path / "docs"
    (root / "2025규정").mkdir(parents=True)
    _docx(root / "매출정산규정.docx", [["매출 정산 규정"], ["분기 마감 후 5일 내 제출"]])
    _xlsx_inline(root / "반출대장.xlsx", [["항목", "금액"], ["반출 승인", 1500]])
    (root / "2025규정" / "휴가규정.txt").write_bytes("휴가규정 개정 안내\n총무팀".encode("cp949"))
    return root


def test_reindex_and_search_finds_text_inside_words(docs):
    """FTS5가 못 하는 것 — "규정"으로 "휴가규정"·"매출정산규정"을 찾는다."""
    result = docsearch.reindex("m1", docs)
    assert result["indexed"] == 3 and result["failed"] == 0 and result["done"] is True

    hit = docsearch.search("m1", "규정")
    paths = [h["path"] for h in hit["hits"]]
    assert "2025규정/휴가규정.txt" in paths      # 어절 안쪽(휴가+규정)
    assert "매출정산규정.docx" in paths          # 복합어 안쪽
    assert hit["truncated"] is False


def test_search_returns_snippets_around_the_match(docs):
    docsearch.reindex("m2", docs)
    hits = docsearch.search("m2", "분기")["hits"]
    assert len(hits) == 1
    assert "분기 마감 후 5일 내 제출" in hits[0]["snippets"][0]


def test_search_requires_all_terms(docs):
    """공백으로 끊은 낱말은 AND다 — 한쪽만 있는 문서는 걸리지 않는다."""
    docsearch.reindex("m3", docs)
    assert [h["path"] for h in docsearch.search("m3", "반출 승인")["hits"]] == ["반출대장.xlsx"]
    assert docsearch.search("m3", "반출 휴가")["hits"] == []


def test_search_finds_extracted_binary_formats(docs):
    """xlsx는 셀 문자열까지, docx는 본문까지 — 원본 바이트로는 검색되지 않는 것들."""
    docsearch.reindex("m4", docs)
    assert [h["path"] for h in docsearch.search("m4", "1500")["hits"]] == ["반출대장.xlsx"]
    assert [h["path"] for h in docsearch.search("m4", "총무팀")["hits"]] == \
        ["2025규정/휴가규정.txt"]


def test_like_metacharacters_are_not_wildcards(docs):
    """사용자가 넣은 %·_가 와일드카드로 동작하면 전부 걸린다."""
    docsearch.reindex("m5", docs)
    assert docsearch.search("m5", "%")["hits"] == []
    assert docsearch.search("m5", "_")["hits"] == []


def test_empty_query_is_not_a_match_all(docs):
    docsearch.reindex("m6", docs)
    assert docsearch.search("m6", "   ") == {
        "query": "   ", "terms": [], "hits": [], "truncated": False}


def test_search_limit_reports_truncation(docs):
    for i in range(5):
        (docs / f"사본{i}.txt").write_text("반출 승인 절차", encoding="utf-8")
    docsearch.reindex("m7", docs)
    result = docsearch.search("m7", "반출", limit=2)
    assert len(result["hits"]) == 2 and result["truncated"] is True


# --- 증분 색인 ---

def test_reindex_skips_unchanged_and_picks_up_edits(docs):
    first = docsearch.reindex("i1", docs)
    assert first["indexed"] == 3

    second = docsearch.reindex("i1", docs)
    assert second["indexed"] == 0 and second["skipped"] == 3

    target = docs / "2025규정" / "휴가규정.txt"
    target.write_bytes("휴가규정 폐지 안내".encode("cp949"))
    # mtime 해상도가 낮은 파일시스템에서도 변경으로 잡히게 명시적으로 밀어 준다
    import os
    os.utime(target, (time.time() + 5, time.time() + 5))
    third = docsearch.reindex("i1", docs)
    assert third["indexed"] == 1 and third["skipped"] == 2
    assert docsearch.search("i1", "폐지")["hits"][0]["path"] == "2025규정/휴가규정.txt"
    assert docsearch.search("i1", "개정")["hits"] == []


def test_reindex_forgets_deleted_files(docs):
    docsearch.reindex("i2", docs)
    (docs / "반출대장.xlsx").unlink()
    result = docsearch.reindex("i2", docs)
    assert result["removed"] == 1
    assert docsearch.search("i2", "반출")["hits"] == []


def test_failed_extraction_is_not_retried_until_the_file_changes(docs, monkeypatch):
    """실패도 캐시해야 한다 — 97-2003 파일 하나를 LibreOffice로 열어 보는 데 2초쯤 걸리고,
    그런 파일이 수백 개면 색인마다 그 시간을 다시 쓴다."""
    (docs / "구버전.doc").write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 32)
    from app.services import doctext as dt

    calls = []
    monkeypatch.setattr(dt, "_soffice", lambda: calls.append(1) or None)

    first = docsearch.reindex("f1", docs)
    assert first["failed"] == 1 and len(calls) == 1

    second = docsearch.reindex("f1", docs)
    assert second["skipped"] == 4 and len(calls) == 1  # 다시 열어 보지 않는다

    forced = docsearch.reindex("f1", docs, force=True)
    assert forced["failed"] == 1 and len(calls) == 2   # force일 때만 다시 시도


def test_reindex_force_reextracts_everything(docs):
    docsearch.reindex("i3", docs)
    forced = docsearch.reindex("i3", docs, force=True)
    assert forced["indexed"] == 3 and forced["skipped"] == 0


def test_reindex_respects_time_budget_and_resumes(docs):
    """MCP 요청 타임아웃(30초)을 넘기지 않으려면 한 번에 끝내지 않아야 한다."""
    partial = docsearch.reindex("i4", docs, budget_seconds=0)
    assert partial["done"] is False
    assert partial["remaining"] == 3 and partial["indexed"] == 0

    rest = docsearch.reindex("i4", docs)
    assert rest["done"] is True and rest["indexed"] == 3


def test_partial_reindex_prunes_deletions_but_keeps_the_rest(docs):
    """목록 훑기는 예산과 무관하게 전체를 보므로, 중간에 멈춰도 삭제 판정은 정확하다.
    아직 추출하지 못한 새 파일이 있어도 기존 색인을 건드리지 않는다."""
    docsearch.reindex("i5", docs)
    (docs / "새문서.txt").write_text("신규", encoding="utf-8")
    (docs / "반출대장.xlsx").unlink()

    partial = docsearch.reindex("i5", docs, budget_seconds=0)
    assert partial["done"] is False and partial["remaining"] == 1  # 새문서는 아직 미추출
    assert partial["removed"] == 1                                 # 지워진 것은 바로 반영
    assert docsearch.search("i5", "반출")["hits"] == []
    assert docsearch.search("i5", "규정")["hits"], "남은 색인은 그대로여야 한다"


# --- 커버리지 보고 ---

def test_status_reports_coverage_and_failure_reasons(docs, monkeypatch):
    (docs / "스캔본.pdf").write_bytes(_pdf(docs / "스캔본.pdf", None).read_bytes())
    (docs / "구버전.doc").write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 32)
    from app.services import doctext as dt

    monkeypatch.setattr(dt, "_soffice", lambda: None)
    pytest.importorskip("pypdf")

    docsearch.reindex("s1", docs)
    report = docsearch.status("s1")
    assert report["total"] == 5
    assert report["indexed"] == 3 and report["failed"] == 2
    assert report["by_suffix"][".docx"] == {"indexed": 1, "failed": 0}
    assert report["by_suffix"][".doc"] == {"indexed": 0, "failed": 1}
    # 왜 못 읽었는지가 이유별로 묶여 나온다 — "붙였는데 검색이 안 된다"의 원인이 여기 있다
    reasons = " ".join(report["failure_reasons"])
    assert "97-2003" in reasons and "OCR" in reasons
    assert report["index_chars"] > 0


def test_skipped_suffixes_are_not_opened(docs):
    """이미지·압축까지 열어 보면 색인 시간의 대부분을 실패에 쓴다."""
    (docs / "사진.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    (docs / "묶음.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 40)
    docsearch.reindex("s2", docs)
    assert docsearch.status("s2")["total"] == 3  # png·zip은 색인에 들어오지 않는다


def test_oversized_file_is_skipped(docs, monkeypatch):
    # 상한은 기존 문서(가장 큰 docx가 약 36KB)는 통과하고 아래 파일만 걸리게 잡는다
    monkeypatch.setattr(docsearch, "MAX_FILE_BYTES", 100_000)
    (docs / "덩치.txt").write_text("가" * 150_000, encoding="utf-8")
    docsearch.reindex("s3", docs)
    assert docsearch.status("s3")["total"] == 3   # 원래 3건만 — 덩치.txt는 열지 않았다
    assert docsearch.search("s3", "가가가")["hits"] == []


def test_long_document_is_truncated_and_marked(docs, monkeypatch):
    monkeypatch.setattr(docsearch, "MAX_INDEX_CHARS", 20)
    (docs / "긴문서.txt").write_text("머리말 " + "가" * 100 + " 꼬리말", encoding="utf-8")
    docsearch.reindex("s4", docs)
    head = docsearch.search("s4", "머리말")["hits"]
    assert head and head[0]["truncated"] is True
    # 잘린 뒷부분은 검색되지 않는다 — 상한을 두는 대가이고, truncated로 드러난다
    assert docsearch.search("s4", "꼬리말")["hits"] == []


def test_missing_root_is_not_an_error(monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    get_settings.cache_clear()
    result = docsearch.reindex("s5", tmp_path / "없는폴더")
    assert result == {"files": 0, "indexed": 0, "failed": 0, "skipped": 0,
                      "removed": 0, "remaining": 0, "done": True}
