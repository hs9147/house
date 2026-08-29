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


def _xlsx_named(path, sheet_name, rows):
    """시트 이름이 있는 형 — workbook.xml의 <sheet name=…>에서만 알 수 있다."""
    strings, cells_xml = [], []
    for value in (v for row in rows for v in row):
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
    assert cells_xml == []
    return _zip(path, {
        "xl/workbook.xml":
            f"<workbook {_SHEET_NS}><sheets><sheet name=\"{sheet_name}\" sheetId=\"1\"/></sheets></workbook>",
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


# --- 구조 보존(마크다운) ---

def _docx_rich(path, blocks):
    """blocks: ("h1"|"p", 텍스트) 또는 ("tbl", 행들[, 병합]) — 실제 파일과 같은 모양."""
    out = []
    for block in blocks:
        kind = block[0]
        if kind == "tbl":
            rows, merges = block[1], (block[2] if len(block) > 2 else {})
            out.append("<w:tbl>")
            for r, row in enumerate(rows):
                out.append("<w:tr>")
                for c, cell in enumerate(row):
                    span = merges.get((r, c))
                    pr = f'<w:tcPr><w:gridSpan w:val="{span}"/></w:tcPr>' if span else ""
                    out.append(f"<w:tc>{pr}<w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>")
                out.append("</w:tr>")
            out.append("</w:tbl>")
        else:
            style = f'<w:pPr><w:pStyle w:val="Heading{kind[1]}"/></w:pPr>' if kind != "p" else ""
            out.append(f"<w:p>{style}<w:r><w:t>{block[1]}</w:t></w:r></w:p>")
    return _zip(path, {
        "word/document.xml":
            f"<w:document {OOXML_NS}><w:body>{''.join(out)}</w:body></w:document>"})


def test_docx_table_keeps_rows_and_columns(tmp_path):
    """평문으로 뽑으면 셀이 한 줄씩 나열돼 어느 값이 어느 열인지 복원할 수 없다."""
    path = _docx_rich(tmp_path / "a.docx", [
        ("h1", "원가 산정 지침"),
        ("p", "2026년 1분기부터 적용한다."),
        ("h2", "1. 재료비"),
        ("tbl", [["구분", "산정 기준"], ["국내 자재", "직전 분기 평균 매입가"]]),
    ])
    assert doctext.extract_markdown(path) == (
        "# 원가 산정 지침\n\n"
        "2026년 1분기부터 적용한다.\n\n"
        "## 1. 재료비\n\n"
        "| 구분 | 산정 기준 |\n"
        "|---|---|\n"
        "| 국내 자재 | 직전 분기 평균 매입가 |"
    )
    # 색인용 평문에는 표시 문자가 남지 않는다 — 발췌에 파이프가 섞이면 값이 안 보인다
    assert doctext.extract_text(path).splitlines() == [
        "원가 산정 지침", "2026년 1분기부터 적용한다.", "1. 재료비",
        "구분\t산정 기준", "국내 자재\t직전 분기 평균 매입가"]


def test_horizontally_merged_table_falls_back_to_inline_html(tmp_path):
    """마크다운 표는 행마다 칸 수가 같아야 해서 colspan을 담을 수 없다 — 억지로 넣으면
    열이 어긋나 값이 다른 열로 읽힌다. GFM이 인라인 HTML을 허용하므로 그 표만 넘긴다."""
    path = _docx_rich(tmp_path / "a.docx", [
        ("tbl", [["직급", "임률", "비고"], ["기사", "38,000", ""]], {(0, 1): 2}),
    ])
    md = doctext.extract_markdown(path)
    assert md.startswith("<table>")
    assert '<th colspan="2">임률</th>' in md
    # 평문 투영에서는 태그가 사라진다(td·tr·th가 질의에 걸리면 전 문서가 오탐이다)
    plain = doctext.extract_text(path)
    assert plain.splitlines() == ["직급\t임률\t비고", "기사\t38,000"]
    assert "<" not in plain and "td" not in plain


def test_cell_pipes_are_escaped_so_columns_do_not_shift(tmp_path):
    path = _docx_rich(tmp_path / "a.docx", [("tbl", [["구분", "값"], ["범위", "a|b"]])])
    assert "| 범위 | a\\|b |" in doctext.extract_markdown(path)
    assert doctext.extract_text(path).splitlines()[-1] == "범위\ta|b"


def test_xlsx_becomes_a_table_with_the_sheet_name(tmp_path):
    path = _xlsx_named(tmp_path / "a.xlsx", "분기원가",
                       [["항목", "금액"], ["재료비", 1500]])
    assert doctext.extract_markdown(path) == (
        "## 분기원가\n\n| 항목 | 금액 |\n|---|---|\n| 재료비 | 1500 |")
    assert doctext.extract_text(path).splitlines() == ["분기원가", "항목\t금액", "재료비\t1500"]


def test_xlsx_without_a_sheet_name_does_not_invent_one(tmp_path):
    """없는 이름을 지어내면 검색에 잡히는 가짜 낱말이 생긴다."""
    path = _xlsx_shared(tmp_path / "a.xlsx", [["항목", "금액"], ["재료비", 1500]])
    assert doctext.extract_markdown(path).startswith("| 항목 |")


def test_plain_files_are_never_stripped(tmp_path):
    """공유 폴더에 있는 .md나 파이프가 든 csv를 벗기면 원문을 망가뜨린다."""
    path = tmp_path / "표.csv"
    path.write_text("# 제목\n| 항목 | 금액 |\n|---|---|\n", encoding="utf-8")
    assert doctext.extract_text(path) == "# 제목\n| 항목 | 금액 |\n|---|---|\n"


def test_xlsx_skipped_cells_stay_in_their_column(tmp_path):
    """엑셀은 빈 셀을 XML에 아예 쓰지 않는다 — 셀 참조(r="C2")를 무시하고 순서대로
    받으면 금액이 담당자 열로 밀려, 틀린 값이 조용히 맞는 값처럼 보인다."""
    ns = _SHEET_NS
    sheet = (
        f'<worksheet {ns}><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>항목</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>담당</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>금액</t></is></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>반출승인</t></is></c>'
        '<c r="C2" t="n"><v>1500</v></c></row>'
        '</sheetData></worksheet>'
    )
    path = _zip(tmp_path / "대장.xlsx", {
        "xl/workbook.xml": f'<workbook {ns}><sheets><sheet name="대장"/></sheets></workbook>',
        "xl/worksheets/sheet1.xml": sheet,
    })
    assert "| 반출승인 |  | 1500 |" in doctext.extract_markdown(path)


def test_docx_vertical_merge_fills_down(tmp_path):
    """세로 병합의 이어지는 셀은 XML에서 빈 셀이다 — 그대로 두면 병합 아래 행들이
    부서 없는 값이 된다. 위 행의 값을 내려 채워 행마다 온전한 레코드로 만든다."""
    body = (
        '<w:tbl>'
        '<w:tr><w:tc><w:p><w:r><w:t>부서</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>항목</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>'
        '<w:p><w:r><w:t>총무팀</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>비품</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>'
        '<w:tc><w:p><w:r><w:t>인장</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>'
        '<w:tc><w:p><w:r><w:t>차량</w:t></w:r></w:p></w:tc></w:tr>'
        '</w:tbl>'
    )
    path = _zip(tmp_path / "a.docx", {
        "word/document.xml": f"<w:document {OOXML_NS}><w:body>{body}</w:body></w:document>"})
    md = doctext.extract_markdown(path)
    assert "| 총무팀 | 비품 |" in md
    assert "| 총무팀 | 인장 |" in md   # 이어지는 행에도 부서가 내려온다
    assert "| 총무팀 | 차량 |" in md   # 3행 병합도 한 행씩 내려온다


HWPX_NS = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'


def test_hwpx_tables_keep_rows_and_columns(tmp_path):
    """hwpx 표를 단락처럼 흘리면 셀이 세로 나열로 무너져 "2025-01"이 어느 관리번호의
    값인지 복원할 수 없다 — 규정·대장류의 지배적 형식이 hwpx라 표가 곧 본문이다."""
    section = (
        f'<sec {HWPX_NS}>'
        '<hp:p><hp:run><hp:t>인장 관리 대장</hp:t></hp:run></hp:p>'
        '<hp:p><hp:run><hp:tbl>'
        '<hp:tr><hp:tc><hp:p><hp:run><hp:t>관리번호</hp:t></hp:run></hp:p></hp:tc>'
        '<hp:tc><hp:p><hp:run><hp:t>사용부서</hp:t></hp:run></hp:p></hp:tc></hp:tr>'
        '<hp:tr><hp:tc><hp:p><hp:run><hp:t>2025-01</hp:t></hp:run></hp:p></hp:tc>'
        '<hp:tc><hp:p><hp:run><hp:t>총무팀</hp:t></hp:run></hp:p></hp:tc></hp:tr>'
        '</hp:tbl></hp:run></hp:p>'
        '<hp:p><hp:run><hp:t>연 1회 실사한다.</hp:t></hp:run></hp:p>'
        '</sec>'
    )
    md = doctext.extract_markdown(_zip(tmp_path / "대장.hwpx", {"Contents/section0.xml": section}))
    assert "| 관리번호 | 사용부서 |" in md
    assert "| 2025-01 | 총무팀 |" in md
    assert "인장 관리 대장" in md and "연 1회 실사한다." in md
    # 셀 텍스트가 단락 나열로 한 번 더 나오지 않는다 — 두 번 나오면 검색에 두 번 걸린다
    assert md.count("총무팀") == 1


def test_hwpx_merged_cells_fall_back_to_inline_html(tmp_path):
    section = (
        f'<sec {HWPX_NS}><hp:p><hp:run><hp:tbl>'
        '<hp:tr><hp:tc><hp:cellSpan colSpan="2" rowSpan="1"/>'
        '<hp:p><hp:run><hp:t>결재</hp:t></hp:run></hp:p></hp:tc>'
        '<hp:tc><hp:p><hp:run><hp:t>승인자</hp:t></hp:run></hp:p></hp:tc></hp:tr>'
        '</hp:tbl></hp:run></hp:p></sec>'
    )
    md = doctext.extract_markdown(_zip(tmp_path / "양식.hwpx", {"Contents/section0.xml": section}))
    assert '<th colspan="2">결재</th>' in md


# --- PDF OCR 폴백 ---

def test_scanned_pdf_error_names_the_ocr_setting(tmp_path, monkeypatch):
    """OCR 도구가 없을 때의 오류가 무엇을 설치하면 되는지 말해야 한다(soffice와 같은 규칙)."""
    monkeypatch.setattr(doctext, "_tesseract", lambda: None)
    with pytest.raises(doctext.ExtractError, match="PAAS_TESSERACT_PATH"):
        doctext.extract(_pdf(tmp_path / "스캔.pdf", None))


def test_scanned_pdf_falls_back_to_ocr(tmp_path, monkeypatch):
    """텍스트 레이어가 없으면 OCR로 넘어간다 — 출처는 마크다운에만 남고 검색 평문은 깨끗하다."""
    monkeypatch.setattr(doctext, "_ocr_pdf", lambda path: "물품 반출 관리 규정")
    markdown, plain = doctext.extract(_pdf(tmp_path / "스캔.pdf", None))
    assert plain == "물품 반출 관리 규정"
    assert markdown.startswith("<!-- 이미지에서 OCR로 추출한 텍스트입니다 -->")
    # 마크다운을 평문으로 벗겨도 출처 주석은 검색에 안 들어간다
    assert "OCR" not in doctext.to_plain(markdown)


def test_normal_text_pdf_never_touches_ocr(tmp_path, monkeypatch):
    def _boom(path):
        raise AssertionError("텍스트가 멀쩡한 PDF가 OCR을 탔다")

    monkeypatch.setattr(doctext, "_ocr_pdf", _boom)
    markdown, plain = doctext.extract(_pdf(tmp_path / "a.pdf", "export approval"))
    assert "export approval" in plain
    assert "OCR" not in markdown


def test_garbled_pua_text_is_not_silently_indexed(tmp_path, monkeypatch):
    """HWP 계열 PDF는 한글을 사설영역(PUA) 코드로 심는 경우가 있다 — pypdf가 "성공"해도
    검색 불가능한 쓰레기다. 예전에는 그대로 색인됐다(읽었다고 착각하게 만드는 상태)."""
    garbage = "\ue0a1\ue0b2\ue0c3 \ue0d4\ue0e5\ue0f6" * 40
    monkeypatch.setattr(doctext, "_ocr_pdf", lambda path: "복구된 본문")
    fake_pages = [type("P", (), {"extract_text": lambda self: garbage})()]

    import pypdf

    class _FakeReader:
        def __init__(self, _path):
            self.is_encrypted = False
            self.pages = fake_pages

    monkeypatch.setattr(pypdf, "PdfReader", _FakeReader)
    markdown, plain = doctext.extract(_pdf(tmp_path / "깨짐.pdf", "x"))
    assert plain == "복구된 본문"

    # OCR이 없으면 쓰레기를 색인하지 않고 실패로 알린다
    monkeypatch.setattr(doctext, "_ocr_pdf", lambda path: None)
    with pytest.raises(doctext.ExtractError, match="깨져"):
        doctext.extract(_pdf(tmp_path / "깨짐.pdf", "x"))


def test_looks_garbled_spares_english_and_korean():
    """한글 없음은 깨짐의 근거가 아니다 — 영어 문서는 한글 0%가 정상이다."""
    assert doctext._looks_garbled("\ue000\ue001\ue002" * 100) is True
    assert doctext._looks_garbled("Quarterly report: revenue grew 12%.") is False
    assert doctext._looks_garbled("반출 승인 절차를 정한다.") is False
    assert doctext._looks_garbled("") is False


def _ocr_ready() -> bool:
    import importlib.util
    import shutil as sh
    from pathlib import Path

    return bool(
        sh.which("tesseract")
        and importlib.util.find_spec("pypdfium2")
        and importlib.util.find_spec("PIL")
        and Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf").exists()
    )


@pytest.mark.skipif(not _ocr_ready(), reason="tesseract/pypdfium2/한글 폰트 없음")
def test_ocr_end_to_end_reads_a_scanned_korean_pdf(tmp_path):
    """이미지로만 담긴 한글 PDF가 실제 OCR 경로로 읽힌다 — 도구가 깔린 환경에서만 돈다."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", 42)
    img = Image.new("RGB", (1240, 400), "white")
    d = ImageDraw.Draw(img)
    d.text((80, 80), "물품 반출 관리 규정", font=font, fill=(20, 20, 20))
    d.text((80, 180), "제1조(목적) 반출 절차와 승인 기준을 정한다.", font=font, fill=(20, 20, 20))
    pdf = tmp_path / "스캔.pdf"
    img.save(pdf)

    markdown, plain = doctext.extract(pdf)
    assert "반출" in plain and "승인" in plain
    assert markdown.startswith("<!-- 이미지에서 OCR로 추출한 텍스트입니다 -->")
