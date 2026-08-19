"""문서 → 텍스트 추출 — 컨테이너 판별, docx·xlsx·pptx·pdf, cp949, 97-2003 폴백.

픽스처는 zipfile/바이트로 직접 만든다(런타임 의존성을 테스트에 끌어들이지 않기 위해).
구조는 실제 파일로 대조해 확인했다 — python-docx/openpyxl/python-pptx로 만든 진짜
파일에서 관찰한 모양 그대로이며, 특히 xlsx는 엑셀이 쓰는 sharedStrings 형과 라이브러리가
쓰는 inlineStr 형이 실제로 다르다는 것을 확인해 둘 다 넣었다.
"""
import io
import zipfile

import pytest

from app.services import doctext

OOXML_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _zip(path, entries: dict[str, str]):
    with zipfile.ZipFile(path, "w") as z:
        for name, body in entries.items():
            z.writestr(name, body)
    return path


def _docx(path, paragraphs: list[list[str]]):
    """단락마다 <w:p>, 서식이 갈리면 한 단락 안에 <w:r>이 여러 개 — 실제 파일과 같은 모양."""
    body = "".join(
        "<w:p>" + "".join(f"<w:r><w:t>{run}</w:t></w:r>" for run in runs) + "</w:p>"
        for runs in paragraphs
    )
    return _zip(path, {"word/document.xml": f"<w:document {OOXML_NS}><w:body>{body}</w:body></w:document>"})


def _pptx(path, slides: dict[str, list[str]]):
    ns = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    return _zip(path, {
        name: f"<sld {ns}>" + "".join(
            f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in lines) + "</sld>"
        for name, lines in slides.items()
    })


_SHEET_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def _xlsx_inline(path, rows: list[list[str]]):
    """라이브러리(openpyxl)가 쓰는 형 — 문자열이 셀 안에 t="inlineStr"로 들어간다."""
    sheet = "".join(
        f'<row r="{i}">' + "".join(
            (f'<c t="inlineStr"><is><t>{v}</t></is></c>' if isinstance(v, str)
             else f'<c t="n"><v>{v}</v></c>')
            for v in row
        ) + "</row>"
        for i, row in enumerate(rows, start=1)
    )
    return _zip(path, {
        "xl/workbook.xml": f"<workbook {_SHEET_NS}/>",
        "xl/worksheets/sheet1.xml": f"<worksheet {_SHEET_NS}><sheetData>{sheet}</sheetData></worksheet>",
    })


def _xlsx_shared(path, rows: list[list[str]]):
    """엑셀이 쓰는 형 — 문자열은 sharedStrings.xml에 모이고 셀은 t="s"로 번호만 든다."""
    strings: list[str] = []
    for row in rows:
        for value in row:
            if isinstance(value, str) and value not in strings:
                strings.append(value)
    sheet = "".join(
        f'<row r="{i}">' + "".join(
            (f'<c t="s"><v>{strings.index(v)}</v></c>' if isinstance(v, str)
             else f'<c t="n"><v>{v}</v></c>')
            for v in row
        ) + "</row>"
        for i, row in enumerate(rows, start=1)
    )
    return _zip(path, {
        "xl/workbook.xml": f"<workbook {_SHEET_NS}/>",
        "xl/sharedStrings.xml": f"<sst {_SHEET_NS}>" + "".join(
            f"<si><t>{s}</t></si>" for s in strings) + "</sst>",
        "xl/worksheets/sheet1.xml": f"<worksheet {_SHEET_NS}><sheetData>{sheet}</sheetData></worksheet>",
    })


def _pdf(path, text: str | None):
    """최소 PDF. text=None이면 그리는 것이 없는(=스캔 이미지 같은) PDF."""
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET\n".encode() if text else b"\n"
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(content)).encode() + b">>stream\n" + content + b"endstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj".encode() + body + b"endobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer<</Root 1 0 R/Size " + str(len(objs) + 1).encode() + b">>\n")
    out.write(b"startxref\n" + str(xref).encode() + b"\n%%EOF\n")
    path.write_bytes(out.getvalue())
    return path


# --- zip + XML 계열 ---

def test_docx_paragraphs_become_lines_and_entities_are_unescaped(tmp_path):
    path = _docx(tmp_path / "a.docx", [
        ["매출 정산 규정"],
        ["분기 마감 후 ", "5일 내에 제출한다"],  # 한 단락이 여러 run으로 쪼개진 경우
        ["담당: 재무팀 &amp; 감사팀"],
    ])
    assert doctext.extract_text(path) == (
        "매출 정산 규정\n분기 마감 후 5일 내에 제출한다\n담당: 재무팀 & 감사팀")


def test_pptx_slides_are_ordered_numerically(tmp_path):
    """slide2가 slide10보다 앞이다 — 문자열 정렬이면 순서가 뒤집힌다."""
    path = _pptx(tmp_path / "a.pptx", {
        "ppt/slides/slide10.xml": ["열번째"],
        "ppt/slides/slide2.xml": ["두번째"],
        "ppt/slides/slide1.xml": ["첫번째"],
    })
    assert doctext.extract_text(path).splitlines() == ["첫번째", "두번째", "열번째"]


@pytest.mark.parametrize("builder", [_xlsx_inline, _xlsx_shared])
def test_xlsx_rows_from_both_string_layouts(tmp_path, builder):
    """엑셀이 쓰는 sharedStrings 형과 라이브러리가 쓰는 inlineStr 형 둘 다 읽어야 한다 —
    한쪽만 보면 나머지 형식은 통째로 빈 텍스트가 나온다(실제 파일에서 확인한 차이)."""
    path = builder(tmp_path / "a.xlsx", [["항목", "금액"], ["반출 승인", 1500], ["휴가규정", 200]])
    assert doctext.extract_text(path).splitlines() == [
        "항목\t금액", "반출 승인\t1500", "휴가규정\t200"]


def test_zip_that_is_not_a_document_says_so(tmp_path):
    path = _zip(tmp_path / "a.zip", {"readme.txt": "hi"})
    with pytest.raises(doctext.ExtractError, match="문서 형식이 아닙니다"):
        doctext.extract_text(path)


def test_broken_zip_is_reported(tmp_path):
    path = tmp_path / "a.docx"
    path.write_bytes(b"PK\x03\x04broken")
    with pytest.raises(doctext.ExtractError, match="압축"):
        doctext.extract_text(path)


# --- 확장자를 믿지 않는다 ---

def test_extension_is_ignored_in_favour_of_container(tmp_path):
    """사내 공유 폴더에는 확장자가 실제 형식과 다른 파일이 섞여 있다."""
    docx_named_doc = _docx(tmp_path / "규정.doc", [["실제로는 docx"]])
    assert doctext.extract_text(docx_named_doc) == "실제로는 docx"

    pdf_named_txt = _pdf(tmp_path / "memo.txt", "actually a pdf")
    assert "actually a pdf" in doctext.extract_text(pdf_named_txt)


# --- 평문 인코딩 ---

def test_cp949_text_is_decoded(tmp_path):
    """한국어 윈도우에서 만든 txt·csv는 cp949다 — utf-8로 읽으면 전부 깨진다."""
    path = tmp_path / "a.txt"
    path.write_bytes("반출 승인 절차\n담당: 총무팀\n".encode("cp949"))
    assert doctext.extract_text(path).startswith("반출 승인 절차")


def test_utf8_bom_is_stripped(tmp_path):
    path = tmp_path / "a.csv"
    path.write_bytes("﻿항목,금액\n".encode("utf-8"))
    assert doctext.extract_text(path) == "항목,금액\n"


def test_text_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(doctext, "MAX_TEXT_CHARS", 10)
    path = tmp_path / "a.txt"
    path.write_text("가" * 100)
    assert len(doctext.extract_text(path)) == 10


# --- PDF ---

def test_pdf_text_is_extracted(tmp_path):
    pytest.importorskip("pypdf")
    path = _pdf(tmp_path / "a.pdf", "export approval")
    assert doctext.extract_text(path) == "export approval"


def test_pdf_without_text_points_at_ocr(tmp_path):
    pytest.importorskip("pypdf")
    path = _pdf(tmp_path / "a.pdf", None)
    with pytest.raises(doctext.ExtractError, match="OCR"):
        doctext.extract_text(path)


def test_pdf_without_driver_says_what_to_install(tmp_path, monkeypatch):
    """pypdf는 선택 의존성이다 — 없을 때 조용히 실패하면 원인을 못 찾는다."""
    import sys

    monkeypatch.setitem(sys.modules, "pypdf", None)
    path = _pdf(tmp_path / "a.pdf", "export approval")
    with pytest.raises(doctext.ExtractError, match="pip install pypdf"):
        doctext.extract_text(path)


# --- 97-2003 바이너리(OLE) ---

def _ole(path, name="a.doc"):
    target = path / name
    target.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
    return target


def test_legacy_office_without_soffice_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setattr(doctext, "_soffice", lambda: None)
    with pytest.raises(doctext.ExtractError, match="PAAS_SOFFICE_PATH"):
        doctext.extract_text(_ole(tmp_path))


def test_legacy_office_uses_soffice_conversion(tmp_path, monkeypatch):
    """변환 결과 txt를 읽어 오는 경로 — 인자·출력 디렉터리·디코딩까지 확인한다.

    실제 LibreOffice를 부르지 않는다: 이 코드가 책임지는 것은 호출 규약과 결과 읽기이고,
    변환 품질은 LibreOffice의 몫이다.
    """
    from pathlib import Path

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs.get("timeout")
        outdir = Path(argv[argv.index("--outdir") + 1])
        # LibreOffice는 원본 이름에 .txt를 붙여 내놓고, 한국어 환경 출력은 cp949일 수 있다
        (outdir / "a.txt").write_bytes("반출 승인 절차".encode("cp949"))
        return type("R", (), {"stdout": b"", "stderr": b""})()

    monkeypatch.setattr(doctext, "_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(doctext.subprocess, "run", fake_run)
    assert doctext.extract_text(_ole(tmp_path)) == "반출 승인 절차"
    assert "--headless" in seen["argv"] and "txt:Text" in seen["argv"]
    assert seen["timeout"] == doctext._SOFFICE_TIMEOUT  # 서버 스레드를 무한정 잡지 않는다


def test_legacy_office_conversion_failure_includes_soffice_output(tmp_path, monkeypatch):
    """대개 원인이 "필터 미설치"라서, 그 출력을 문구에 실어야 되짚을 수 있다."""
    def fake_run(argv, **kwargs):
        return type("R", (), {"stdout": b"", "stderr": b"Error: no export filter"})()

    monkeypatch.setattr(doctext, "_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(doctext.subprocess, "run", fake_run)
    with pytest.raises(doctext.ExtractError, match="no export filter"):
        doctext.extract_text(_ole(tmp_path))


def test_soffice_path_setting_is_used(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_SOFFICE_PATH", "/opt/libreoffice/soffice")
    from app.config import get_settings

    get_settings.cache_clear()
    seen = {}
    monkeypatch.setattr(doctext.shutil, "which", lambda name: seen.setdefault("name", name))
    doctext._soffice()
    assert seen["name"] == "/opt/libreoffice/soffice"
