""".ready 캐시 — 추출 결과를 마크다운 파일로 남기고, 원본이 바뀌면 다시 만든다."""
import zipfile

import pytest

from app.config import get_settings
from app.services import docready, docsearch, doctext

OOXML_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


@pytest.fixture(autouse=True)
def _index_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _docx(path, paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml",
                   f"<w:document {OOXML_NS}><w:body>{body}</w:body></w:document>")
    return path


def test_read_writes_the_cache_and_serves_it_next_time(tmp_path):
    source = _docx(tmp_path / "docs" / "규정.docx", ["연차 규정", "월 1일 발생"])

    assert docready.read("rules", "규정.docx", source) == "연차 규정\n\n월 1일 발생"

    cached = docready.path_for("rules", "규정.docx")
    assert cached.exists()
    # 원본이 어느 것인지 파일만 열어도 알 수 있어야 한다(운영자가 확인하는 경로다)
    assert "source: 규정.docx" in cached.read_text(encoding="utf-8")

    # 두 번째 읽기는 원본을 열지 않는다 — 원본을 지워도 같은 값이 나온다
    source_bytes = source.read_bytes()
    assert docready.read("rules", "규정.docx", source) == "연차 규정\n\n월 1일 발생"
    assert source.read_bytes() == source_bytes


def test_changed_source_invalidates_the_cache(tmp_path):
    source = _docx(tmp_path / "docs" / "규정.docx", ["연차 규정"])
    assert docready.read("rules", "규정.docx", source) == "연차 규정"

    _docx(source, ["연차 규정", "2026년 개정"])
    assert docready.read("rules", "규정.docx", source) == "연차 규정\n\n2026년 개정"


def test_cache_ignores_a_stale_file_whose_fingerprint_does_not_match(tmp_path):
    """mtime만 보면 백업에서 되돌린 문서를 놓친다 — 원본의 (크기, mtime)을 적어 두고 본다."""
    source = _docx(tmp_path / "docs" / "규정.docx", ["연차 규정"])
    docready.read("rules", "규정.docx", source)

    cached = docready.path_for("rules", "규정.docx")
    cached.write_text("---\nsource: 규정.docx\nsize: 1\nmtime: 1.000000\n---\n\n엉뚱한 내용",
                      encoding="utf-8")
    assert docready.read("rules", "규정.docx", source) == "연차 규정"


def test_cache_without_front_matter_is_ignored_not_served(tmp_path):
    source = _docx(tmp_path / "docs" / "규정.docx", ["연차 규정"])
    cached = docready.path_for("rules", "규정.docx")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text("머리말 없는 옛 캐시", encoding="utf-8")
    assert docready.read("rules", "규정.docx", source) == "연차 규정"


def test_very_long_paths_fall_back_to_a_hashed_name(tmp_path, monkeypatch):
    """윈도우 MAX_PATH(260)를 넘기면 쓰기가 실패한다 — 사내 공유 폴더는 한글 폴더명이
    길게 겹쳐서 실제로 넘는 경우가 있다."""
    deep = "/".join(["아주-긴-부서-이름-폴더"] * 12) + "/보고서.docx"
    target = docready.path_for("rules", deep)
    assert len(str(target)) <= 240
    assert target.name.endswith(".md") and "/" not in target.name
    # 짧은 경로는 그대로 미러링해 사람이 찾아 들어갈 수 있다
    assert docready.path_for("rules", "규정/연차.docx").as_posix().endswith(
        "rules/규정/연차.docx.md")


def test_reindex_fills_the_cache_and_prunes_deleted_files(tmp_path):
    root = tmp_path / "docs"
    _docx(root / "규정.docx", ["연차 규정"])
    (root / "메모.txt").write_text("총무팀 확인", encoding="utf-8")

    result = docsearch.reindex("rules", root)
    assert result["indexed"] == 2
    assert docready.path_for("rules", "규정.docx").exists()
    assert docready.path_for("rules", "메모.txt").exists()

    (root / "메모.txt").unlink()
    docsearch.reindex("rules", root)
    assert not docready.path_for("rules", "메모.txt").exists()
    assert docready.path_for("rules", "규정.docx").exists()


def test_a_write_failure_does_not_break_reading(tmp_path, monkeypatch):
    """캐시는 없어도 되는 것이다 — 여기서 예외를 올리면 색인과 읽기가 통째로 실패한다."""
    source = _docx(tmp_path / "docs" / "규정.docx", ["연차 규정"])

    def boom(*args, **kwargs):
        raise OSError("디스크 없음")

    monkeypatch.setattr(docready.Path, "write_text", boom)
    assert docready.read("rules", "규정.docx", source) == "연차 규정"
    assert not docready.path_for("rules", "규정.docx").exists()


def test_unreadable_document_still_reports_why(tmp_path):
    bad = tmp_path / "docs" / "깨진.docx"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"PK\x03\x04broken")
    with pytest.raises(doctext.ExtractError, match="압축"):
        docready.read("rules", "깨진.docx", bad)


def test_a_ready_folder_inside_a_store_is_not_indexed(tmp_path):
    """캐시가 어쩌다 저장소 안에 놓여도 자기 자신을 색인하면 안 된다 — 그러면 문서마다
    검색 결과가 둘씩 나오고 색인이 두 배가 된다."""
    root = tmp_path / "docs"
    (root / ".ready").mkdir(parents=True)
    (root / "규정.txt").write_text("연차 규정", encoding="utf-8")
    (root / ".ready" / "규정.txt.md").write_text("연차 규정", encoding="utf-8")

    assert docsearch.reindex("rules", root)["files"] == 1
    assert [h["path"] for h in docsearch.search("rules", "연차")["hits"]] == ["규정.txt"]
