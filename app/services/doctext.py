"""문서 파일 → 텍스트 추출 — 사내 문서 폴더를 LLM이 읽을 수 있게 만드는 유일한 경로.

바이트를 utf-8로 그냥 디코드하면 .pdf·.docx·.xlsx는 깨진 글자만 나온다. 여기서 형식을
판별해 본문 텍스트만 뽑는다.

**두 가지 모양으로 낸다** — 읽기용 마크다운과 검색 색인용 평문(extract).

평문만 뽑으면 표가 셀 나열로 무너진다: "구분 / 산정 기준 / 적용 시점 / 국내 자재 /
직전 분기 평균 매입가 / 분기 초"에서 "분기 초"가 어느 항목의 값인지 복원할 방법이 없다.
마크다운으로 내면 행·열이 남는다. HTML도 같은 일을 하지만 측정해 보니 표가 큰 문서에서
토큰이 1.6~1.9배였고(100행×8열: 11,660자 대 6,016자), 태그가 검색 발췌의 절반을 먹고,
`td`·`tr`·`th`를 포함한 질의가 전 문서에 오탐으로 걸린다. 그래서 마크다운을 쓰고,
마크다운이 표현하지 못하는 가로 병합 셀이 있는 표만 인라인 HTML로 떨어뜨린다
(GFM이 허용한다).

검색 색인에는 그 마크다운에서 표시 문자를 벗긴 평문을 넣는다 — 발췌가 사람과 모델
양쪽에 읽히고, 마크업이 질의에 걸리지 않는다.

형식 판별은 확장자가 아니라 **컨테이너 매직**으로 한다. 사내 공유 폴더에는 확장자가
실제 형식과 다른 파일이 섞여 있다(다른 이름으로 저장하면서 .doc로 붙인 docx, 확장자 없는
파일). 확장자를 믿으면 조용히 깨진 텍스트를 돌려주게 되고, 그건 "읽었다"고 착각하게
만들어서 못 읽는 것보다 나쁘다.

  zip(PK)        docx·xlsx·pptx·hwpx  표준 라이브러리만으로 된다(zipfile + ElementTree).
                                      이 형식들은 zip 안의 XML이다.
  %PDF           pdf                  pdfplumber로 **표를 복원**하고(괘선이 있는 표만),
                                      나머지 본문은 읽기 순서대로 흘린다. pdfplumber가
                                      없거나 본문을 못 뽑으면 pypdf 평문으로 떨어진다.
                                      텍스트가 없는 스캔 PDF와 깨진 텍스트(HWP 계열의
                                      사설영역 인코딩)는 Tesseract OCR 폴백(선택,
                                      PAAS_TESSERACT_PATH) — 없으면 무엇을 설치하면
                                      되는지 알린다.
  OLE(D0CF11E0)  97-2003 doc·xls·ppt  순수 파이썬으로 제대로 뽑을 수 없다 →
                                      LibreOffice(soffice) 변환 경유(선택).
  그 외           텍스트                utf-8 → cp949 순서로 디코드한다. 한국어 윈도우에서
                                      만든 txt·csv는 cp949인 경우가 많고, utf-8로 읽으면
                                      한글이 전부 깨진다.
"""
import re
import shutil
import subprocess
import tempfile
import zipfile
from email import policy as email_policy
from email.parser import BytesParser
from html import escape, unescape
from pathlib import Path
from xml.etree import ElementTree

from ..config import get_settings

# 한 파일에서 가져올 텍스트 상한. 표시용 자르기는 호출자가 따로 하고(더 짧다), 이건
# 병적으로 큰 파일이 메모리를 먹지 않게 하는 방어선이다.
MAX_TEXT_CHARS = 1_000_000
# LibreOffice 변환 상한 — 문서 하나가 서버 스레드를 무한정 잡고 있으면 안 된다.
_SOFFICE_TIMEOUT = 120

_OLE_MAGIC = b"\xd0\xcf\x11\xe0"

# 추출 능력 지문은 **프로세스 단위로 한 번** 계산한다. 값이 달라지는 조건(pip 설치, soffice·
# tesseract 설치)은 서비스 재시작을 동반하거나, 다음 재시작에서 반영되면 충분하다 — 문서마다
# shutil.which를 도는 비용을 치를 이유가 없다(색인은 수만 건을 돈다).
_CAPABILITY_FP: str | None = None


class ExtractError(RuntimeError):
    """추출할 수 없는 파일 — 형식 미지원, 드라이버 없음, 손상."""


def capability_fingerprint() -> str:
    """**지금 이 서버가 무엇을 추출할 수 있는지**를 한 줄로 적은 값.

    색인은 실패도 캐시한다(같은 파일을 매번 다시 열어 보면 97-2003 하나에 2초씩 쓴다).
    그런데 실패의 원인이 파일이 아니라 환경인 경우가 많다 — pypdf를 설치하면 어제 실패한
    PDF가 오늘은 읽힌다. 실측: pypdf 설치 뒤에도 PDF 6,600건이 "추출기가 없습니다"로 남아
    있었고, 재색인 버튼은 아무 일도 하지 않았다(크기·시각이 같아 건너뛴다).

    그래서 실패한 행에 이 값을 함께 적어 두고, 값이 **달라졌을 때만** 다시 시도한다.
    환경이 그대로면 재시도하지 않으므로 실패 캐시의 이점은 그대로다.
    """
    global _CAPABILITY_FP
    if _CAPABILITY_FP is not None:
        return _CAPABILITY_FP

    def has(module: str) -> str:
        try:
            __import__(module)
            return "1"
        except Exception:  # noqa: BLE001 — 설치 여부만 본다
            return "0"

    _CAPABILITY_FP = ";".join([
        f"pypdf={has('pypdf')}",
        f"pdfplumber={has('pdfplumber')}",
        f"pypdfium2={has('pypdfium2')}",
        f"pillow={has('PIL')}",
        f"soffice={'1' if _soffice() else '0'}",
        f"tesseract={'1' if _tesseract() else '0'}",
    ])
    return _CAPABILITY_FP


def extract(path: Path) -> tuple[str, str]:
    """문서 하나를 (마크다운, 검색용 평문)으로. 못 뽑으면 ExtractError(이유를 담아서).

    한 번만 열고 한 번만 파싱한다 — 두 모양이 어긋나면 "검색에는 걸리는데 읽으면 없는"
    상태가 된다. 구조를 담을 수 없는 형식(pdf·평문·97-2003)은 둘이 같은 값이다.
    """
    try:
        head = path.open("rb").read(8)
    except OSError as e:
        raise ExtractError(f"파일을 열 수 없습니다: {e}")

    if path.suffix.lower() == ".eml":
        text = _eml_markdown(path)[:MAX_TEXT_CHARS]
        return text, text

    if head[:4] == b"PK\x03\x04":
        markdown = _ooxml_markdown(path)[:MAX_TEXT_CHARS]
        # 벗기기는 **우리가 만든 마크다운에만** 적용한다. 공유 폴더에 있는 .md나
        # 파이프가 든 csv를 평문 취급하다 벗기면 원문을 망가뜨린다.
        return markdown, to_plain(markdown)

    if head[:4] == b"%PDF":
        blocks, from_ocr = _pdf_blocks(path)
        # 벗기기는 **우리가 만든 표 블록에만** 적용한다 — 본문 블록은 PDF 원문이라
        # 파이프나 #으로 시작하는 줄이 있어도 건드리지 않는다(zip 계열과 같은 규칙).
        markdown = "\n\n".join(text for text, _ in blocks)[:MAX_TEXT_CHARS]
        plain = "\n\n".join(
            to_plain(text) if is_table else text for text, is_table in blocks
        )[:MAX_TEXT_CHARS]
        if from_ocr:
            # 출처는 마크다운 쪽에만 남긴다 — to_plain이 이 줄을 떨어뜨려 검색은
            # 오염되지 않고, read_doc으로 열어 본 사람은 왜 글자가 어색한지 알 수 있다.
            return f"<!-- 이미지에서 OCR로 추출한 텍스트입니다 -->\n\n{markdown}", plain
        return markdown, plain

    if head[:4] == _OLE_MAGIC:
        text = _legacy_office_text(path)
    else:
        text = _plain_text(path)
    text = text[:MAX_TEXT_CHARS]
    return text, text


def extract_text(path: Path) -> str:
    """검색 색인용 평문."""
    return extract(path)[1]


def extract_markdown(path: Path) -> str:
    """LLM이 읽을 마크다운 — 제목 단계와 표의 행·열이 남는다."""
    return extract(path)[0]


# --- zip + XML 계열 (docx·xlsx·pptx·hwpx) ---

# 본문 엔트리 판별. 슬라이드·hwpx 구역은 단락만 있으므로 표·제목 처리가 필요 없다.
_SLIDE_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")
_HWPX_RE = re.compile(r"^Contents/section\d+\.xml$")
_SHEET_RE = re.compile(r"^xl/worksheets/sheet\d+\.xml$")
_HEADING_RE = re.compile(r"^Heading(\d)$", re.I)


def _ooxml_markdown(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if "xl/workbook.xml" in names:
                return _xlsx_markdown(zf, names)
            if "word/document.xml" in names:
                return _docx_markdown(zf)
            sections = sorted((n for n in names if _HWPX_RE.match(n)), key=_entry_order)
            if sections:
                return _hwpx_markdown(zf, sections)
            slides = sorted((n for n in names if _SLIDE_RE.match(n)), key=_entry_order)
            if slides:
                # 단락 사이는 빈 줄로 — 마크다운에서 줄바꿈 하나는 같은 문단의 이어짐이다.
                parts = [_xml_text(zf.read(n), "t", "p") for n in slides]
                return "\n".join(p for p in parts if p)
    except zipfile.BadZipFile as e:
        raise ExtractError(f"압축이 깨졌습니다: {e}")
    raise ExtractError(
        "zip 파일이지만 문서 형식이 아닙니다(docx·xlsx·pptx·hwpx가 아님).")


def _parse(data: bytes, what: str):
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        raise ExtractError(f"{what}을 읽을 수 없습니다: {e}")


def _attr(element, name: str) -> str:
    """네임스페이스가 붙은 속성 읽기 — w:val은 실제로 {…main}val이다."""
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1] == name:
            return value
    return ""


# --- docx: 제목 단계와 표를 살린다 ---

def _docx_markdown(zf: zipfile.ZipFile) -> str:
    root = _parse(zf.read("word/document.xml"), "word/document.xml")
    body = next((e for e in root.iter() if _local(e) == "body"), root)
    blocks: list[str] = []
    for node in body:
        local = _local(node)
        if local == "p":
            text = "".join(t.text or "" for t in node.iter() if _local(t) == "t")
            if not text.strip():
                continue
            style = next((_attr(s, "val") for s in node.iter() if _local(s) == "pStyle"), "")
            level = _HEADING_RE.match(style or "")
            blocks.append(f"{'#' * min(int(level.group(1)), 6)} {text}" if level else text)
        elif local == "tbl":
            table = _table_markdown(_docx_rows(node))
            if table:
                blocks.append(table)
    return "\n\n".join(blocks)


def _docx_rows(tbl) -> list[list[tuple[str, int]]]:
    """(셀 텍스트, 가로 병합 칸 수).

    세로 병합의 이어지는 셀(vMerge, restart 아님)은 XML에서 빈 셀이다 — 그대로 두면
    "총무팀"이 첫 행에만 남고 병합 아래 행들은 부서 없는 값이 된다. 위 행에서 같은
    자리를 덮는 셀의 텍스트를 내려 채워 행마다 온전한 레코드로 만든다."""
    rows = []
    for tr in (c for c in tbl if _local(c) == "tr"):
        cells = []
        grid = 0  # 이 셀이 시작하는 표 기준 열 자리(앞 셀들의 병합 칸 수 누적)
        for tc in (c for c in tr if _local(c) == "tc"):
            text = "".join(t.text or "" for t in tc.iter() if _local(t) == "t")
            span = next((_attr(g, "val") for g in tc.iter() if _local(g) == "gridSpan"), "")
            width = int(span) if span.isdigit() else 1
            merge = next((g for g in tc.iter() if _local(g) == "vMerge"), None)
            if merge is not None and _attr(merge, "val") != "restart" and not text.strip():
                text = _cell_above(rows, grid)
            cells.append((text, width))
            grid += width
        if cells:
            rows.append(cells)
    return rows


def _cell_above(rows: list[list[tuple[str, int]]], grid: int) -> str:
    """바로 위 행에서 grid 열 자리를 덮는 셀의 텍스트 — 위 행도 이미 채워져 있으므로
    3행 이상 병합도 한 행씩 내려온다."""
    if not rows:
        return ""
    pos = 0
    for text, span in rows[-1]:
        if pos <= grid < pos + span:
            return text
        pos += span
    return ""


# --- 표 렌더링 ---

def _table_markdown(rows: list[list[tuple[str, int]]]) -> str:
    """가로 병합이 없으면 마크다운 표, 있으면 그 표만 인라인 HTML.

    마크다운 표는 모든 행의 칸 수가 같아야 해서 colspan을 담을 수 없다 — 결재 양식처럼
    병합이 있는 표를 억지로 밀어 넣으면 열이 어긋나 값이 다른 열로 읽힌다.
    """
    if not rows:
        return ""
    if any(span > 1 for row in rows for _, span in row):
        return _table_html(rows)

    width = max(len(row) for row in rows)

    def line(row):
        values = [_md_cell(text) for text, _ in row] + [""] * (width - len(row))
        return "| " + " | ".join(values) + " |"

    return "\n".join([line(rows[0]), "|" + "---|" * width, *(line(r) for r in rows[1:])])


def _md_cell(value: str) -> str:
    """셀 안의 파이프는 열 구분자로 읽히고, 줄바꿈은 표를 끊는다."""
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _table_html(rows: list[list[tuple[str, int]]]) -> str:
    out = ["<table>"]
    for index, row in enumerate(rows):
        tag = "th" if index == 0 else "td"
        cells = []
        for text, span in row:
            attr = f' colspan="{span}"' if span > 1 else ""
            cells.append(f"<{tag}{attr}>{escape(text.strip())}</{tag}>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


# --- hwpx: 표를 살린다 ---

def _hwpx_markdown(zf: zipfile.ZipFile, entries: list[str]) -> str:
    """hwpx 구역 — 단락은 줄로, 표는 행·열이 남게 마크다운 표로.

    사내 규정·대장류의 지배적 형식인데, 표를 단락처럼 흘리면 셀이 세로 나열로 무너져
    "2025-01"이 어느 관리번호의 값인지 복원할 수 없다(평문 추출의 표 문제와 같다).

    hwpx의 표는 단락의 런 안에 컨트롤로 들어 있다(<hp:p><hp:run><hp:tbl>…). 문서
    순서로 걸으며 표를 만나면 통째로 렌더링하고 그 아래로는 들어가지 않는다 — 셀
    텍스트가 단락 나열로 한 번 더 나오면 같은 값이 검색에 두 번 걸린다.
    """
    blocks: list[str] = []
    for name in entries:
        root = _parse(zf.read(name), name)
        inside_table: set[int] = set()
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                blocks.append("".join(buffer))
                buffer.clear()

        for element in root.iter():
            if id(element) in inside_table:
                continue
            local = _local(element)
            if local == "tbl":
                inside_table.update(id(d) for d in element.iter())
                flush()
                table = _table_markdown(_hwpx_rows(element))
                if table:
                    blocks.append(table)
            elif local == "p":
                flush()
            elif local == "t" and element.text:
                buffer.append(element.text)
        flush()
    return "\n\n".join(blocks)


def _hwpx_rows(tbl) -> list[list[tuple[str, int]]]:
    """(셀 텍스트, 가로 병합 칸 수). 병합 칸 수는 <hp:cellSpan colSpan="…">에 있다."""
    rows = []
    for tr in (c for c in tbl if _local(c) == "tr"):
        cells = []
        for tc in (c for c in tr if _local(c) == "tc"):
            text = "".join(t.text or "" for t in tc.iter() if _local(t) == "t")
            span = next(
                (g.get("colSpan") or "" for g in tc.iter() if _local(g) == "cellSpan"), "")
            cells.append((text, int(span) if span.isdigit() else 1))
        if cells:
            rows.append(cells)
    return rows


# --- xlsx ---

def _xlsx_markdown(zf: zipfile.ZipFile, names: list[str]) -> str:
    """시트마다 표 하나. 첫 행을 머리글로 삼는다 — 스프레드시트의 통상적인 모양이다.

    문자열이 어디 있는지가 두 갈래다: 엑셀이 저장한 파일은 xl/sharedStrings.xml에 모아
    두고 셀에서 번호로 참조하지만(t="s"), 라이브러리로 만든 파일은 셀 안에 그대로
    넣는다(t="inlineStr"). 둘 다 받는다 — 실제 파일로 확인한 결과 이 차이 때문에 한쪽만
    보면 통째로 빈 텍스트가 나온다.
    """
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in _parse(zf.read("xl/sharedStrings.xml"), "sharedStrings.xml"):
            shared.append("".join(t.text or "" for t in si.iter() if _local(t) == "t"))

    titles = [
        _attr(s, "name") or s.get("name") or ""
        for s in _parse(zf.read("xl/workbook.xml"), "workbook.xml").iter()
        if _local(s) == "sheet"
    ]

    blocks: list[str] = []
    sheets = sorted((n for n in names if _SHEET_RE.match(n)), key=_entry_order)
    for index, sheet in enumerate(sheets):
        root = _parse(zf.read(sheet), sheet)
        rows = []
        for row in root.iter():
            if _local(row) != "row":
                continue
            # 엑셀은 빈 셀을 XML에 아예 쓰지 않는다 — 순서대로만 받으면 A·C에 값이 있는
            # 행에서 C의 값이 B 열로 밀려 들어온다(금액이 담당자 열로 읽히는 식으로,
            # 틀린 값이 조용히 맞는 값처럼 보인다). 셀 참조(r="C2")로 제자리에 놓는다.
            cells: list[str] = []
            for cell in row:
                if _local(cell) != "c":
                    continue
                at = _col_index(cell.get("r") or "")
                if at is not None and at > len(cells):
                    cells.extend([""] * (at - len(cells)))
                cells.append(_cell_text(cell, shared))
            if any(cells):
                rows.append([(value, 1) for value in cells])
        if not rows:
            continue
        # 시트 이름은 workbook.xml이 있어야 알 수 있다 — 없으면 머리글을 만들지 않는다
        # (없는 이름을 지어내면 검색에 잡히는 가짜 낱말이 생긴다).
        title = titles[index] if index < len(titles) else ""
        if title:
            blocks.append(f"## {title}")
        blocks.append(_table_markdown(rows))
    return "\n\n".join(blocks)


def _col_index(ref: str) -> int | None:
    """셀 참조의 열 자리 — "C2"면 2. 참조가 없는 파일(라이브러리 생성)은 None → 순서대로."""
    match = re.match(r"([A-Z]{1,3})\d+$", ref)
    if not match:
        return None
    index = 0
    for ch in match.group(1):
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def _cell_text(cell, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "s":  # sharedStrings 참조
        value = next((v.text for v in cell if _local(v) == "v"), None)
        try:
            return shared[int(value)]
        except (TypeError, ValueError, IndexError):
            return ""
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter() if _local(t) == "t")
    # 숫자·불리언·수식 결과·오류는 <v> 그대로. 날짜는 엑셀 일련번호로 나온다 —
    # 표시 서식까지 재현하려면 styles.xml을 해석해야 해서 검색 목적에는 과하다.
    return next((v.text or "" for v in cell if _local(v) == "v"), "")


# --- 마크다운 → 검색 색인용 평문 ---

# |---|---| 구분선. 사람에게도 모델에게도 뜻이 없고 발췌만 잡아먹는다.
_MD_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+")
# 열 구분자인 파이프만 — 셀 안의 `\|`는 값의 일부다.
_MD_PIPE_RE = re.compile(r"(?<!\\)\|")
_TAG_RE = re.compile(r"<[^>]+>")


def to_plain(markdown: str) -> str:
    """표시 문자를 벗겨 낸다 — 검색 발췌에 파이프·태그가 섞이면 값이 안 보인다.

    **우리가 만든 마크다운에만** 쓴다(extract 참고). 공유 폴더에 있는 .md 파일이나
    파이프가 든 csv에 이걸 돌리면 원문을 망가뜨린다.
    """
    lines: list[str] = []
    for line in markdown.split("\n"):
        line = line.strip()
        if not line or _MD_RULE_RE.match(line):
            continue
        if line.startswith("<"):  # 병합 표만 인라인 HTML로 나간다
            if line in ("<table>", "</table>"):
                continue
            line = unescape(_TAG_RE.sub("\t", line))
            line = re.sub(r"\t+", "\t", line).strip("\t").strip()
            if not line:
                continue
        elif line.startswith("|"):
            # 열 구분자로 쓰인 파이프에서만 끊는다 — 셀 안의 `\|`까지 끊으면 값이 쪼개진다.
            cells = _MD_PIPE_RE.split(line.strip("|"))
            line = "\t".join(c.strip().replace("\\|", "|") for c in cells)
        else:
            line = _MD_HEADING_RE.sub("", line)
        lines.append(line)
    return "\n".join(lines)


def _local(element) -> str:
    """네임스페이스를 떼어낸 태그 이름."""
    return element.tag.rsplit("}", 1)[-1]


def _entry_order(name: str) -> tuple:
    """slide2.xml이 slide10.xml보다 앞에 오게 — 문자열 정렬은 슬라이드 순서를 뒤집는다."""
    match = re.search(r"(\d+)", Path(name).stem)
    return (int(match.group(1)) if match else 0, name)


def _xml_text(data: bytes, text_tag: str, block_tag: str) -> str:
    """XML에서 텍스트 노드만 모은다. 블록 태그를 만나면 줄을 끊는다.

    ElementTree를 쓰는 이유: 정규식으로 태그를 벗기면 &amp;·&#xAC00; 같은 엔티티가
    그대로 남는다.
    """
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        raise ExtractError(f"본문 XML을 읽을 수 없습니다: {e}")

    lines: list[str] = []
    buffer: list[str] = []
    for element in root.iter():
        local = _local(element)
        # iter()는 문서 순서이고 블록 요소는 자기 자식보다 먼저 방문된다 — 그래서
        # 블록을 만난 시점에 "앞 블록에서 모은 것"을 흘려보내면 단락이 맞는다.
        if local == block_tag:
            if buffer:
                lines.append("".join(buffer))
                buffer = []
        elif local == text_tag and element.text:
            buffer.append(element.text)
    if buffer:
        lines.append("".join(buffer))
    return "\n".join(lines)


# --- PDF ---

# 띠(표 사이의 본문 구간)로 자를 최소 높이. 괘선 두께만큼 남는 틈에서 빈 텍스트를
# 뽑으려고 크롭을 한 번 더 도는 것을 막는다.
_MIN_BAND = 4.0


def _pdf_blocks(path: Path) -> tuple[list[tuple[str, bool]], bool]:
    """(블록 목록, OCR을 거쳤는가). 블록은 (텍스트, 이게 표인가).

    **표를 복원하는 이유.** pypdf의 PDF 텍스트는 평문이라 표가 셀 나열로 무너진다 —
    xlsx·docx에서 표를 살린 것과 같은 이유다. 실측: PDF 263건을 읽어 냈는데 구조가 생긴
    문서는 7건이었다(전환율 13.1%). 구조 추출기가 걸리는 자리는 제목·조문·표인데, 평문
    PDF에는 그 중 아무것도 글자로 남지 않는다.

    **표만 복원하고 제목은 추정하지 않는다.** 괘선이 있는 표는 PDF 안에 선으로 그려져
    있어서 판정이 결정론적이지만, 제목은 글자 크기·굵기로 **추측**해야 한다. 틀린 절
    계층은 없는 것보다 나쁘다 — 엉뚱한 절에 매달린 표·용어가 그래프에 남는다.

    pdfplumber가 없거나 이 파일에서 실패하면 기존 경로(pypdf + OCR 폴백)로 떨어진다.
    즉 이 함수가 하는 일은 **더하기만**이다 — 지금 읽히는 PDF가 이것 때문에 실패하지 않는다.
    """
    blocks = _pdfplumber_blocks(path)
    if blocks is not None:
        joined = "\n".join(text for text, _ in blocks).strip()
        # 비었거나(스캔) 깨졌으면(PUA) 판정과 안내는 기존 경로에 맡긴다 — 실패 문구가
        # 무엇을 설치하면 되는지 말해 주는 자리가 거기 하나다.
        if joined and not _looks_garbled(joined):
            return blocks, False
    text, from_ocr = _pdf_text(path)
    return [(text, False)], from_ocr


def _pdfplumber_blocks(path: Path) -> list[tuple[str, bool]] | None:
    """읽기 순서대로 (본문 | 마크다운 표). 못 하면 None(호출자가 pypdf로 떨어진다)."""
    try:
        import pdfplumber  # noqa: PLC0415 — 선택 의존성
    except ImportError:
        return None

    blocks: list[tuple[str, bool]] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                blocks.extend(_page_blocks(page))
                # pdfplumber는 페이지마다 글자·선 객체를 캐시한다 — 수백 쪽 문서에서
                # 그대로 쌓이면 색인 프로세스가 통째로 커진다.
                page.flush_cache()
    except Exception:  # noqa: BLE001 — 손상·암호·미지원은 pypdf 경로가 판정한다
        return None
    return blocks


def _page_blocks(page) -> list[tuple[str, bool]]:
    """한 쪽을 읽기 순서대로 쪼갠다 — 표 위 본문, 표, (표와 나란한 본문), 표 아래 본문.

    표 안의 글자는 본문 쪽에서 빼낸다(filter) — 그러지 않으면 같은 값이 평문으로 한 번,
    표로 또 한 번 색인돼 검색에 두 번 걸린다(hwpx에서 겪은 것과 같다). 반대로 표와 같은
    높이에 있는 **옆 본문**은 띠를 통째로 버리면 조용히 사라지므로, 표 글자만 뺀 같은 띠를
    한 번 더 읽어 뒤에 붙인다.
    """
    x0, top, x1, bottom = page.bbox
    tables = sorted(page.find_tables(), key=lambda t: (t.bbox[1], t.bbox[0]))
    if not tables:
        text = (page.extract_text() or "").strip()
        return [(text, False)] if text else []

    boxes = [t.bbox for t in tables]

    def outside(obj) -> bool:
        cx = (obj["x0"] + obj["x1"]) / 2
        cy = (obj["top"] + obj["bottom"]) / 2
        return not any(bx0 <= cx <= bx1 and btop <= cy <= bbot
                       for bx0, btop, bx1, bbot in boxes)

    out: list[tuple[str, bool]] = []

    def add_text(y0: float, y1: float) -> None:
        if y1 - y0 < _MIN_BAND:
            return
        try:
            band = page.crop((x0, y0, x1, y1)).filter(outside)
        except Exception:  # noqa: BLE001 — 좌표가 쪽 밖이면 그 띠만 건너뛴다
            return
        text = (band.extract_text() or "").strip()
        if text:
            out.append((text, False))

    y = top
    for table in tables:
        t_top, t_bottom = table.bbox[1], table.bbox[3]
        if t_top > y:
            add_text(y, t_top)
        rows = [[(cell or "", 1) for cell in row] for row in table.extract()]
        # 1열이거나 한 줄인 "표"는 쪽 테두리·머리말 상자다 — 스키마가 아니다.
        if len(rows) >= 2 and max((len(r) for r in rows), default=0) >= 2:
            markdown = _table_markdown(rows)
            if markdown:
                out.append((markdown, True))
        add_text(t_top, t_bottom)
        y = max(y, t_bottom)
    add_text(y, bottom)
    return out


def _pdf_text(path: Path) -> tuple[str, bool]:
    """(본문, OCR을 거쳤는가). 텍스트 레이어가 멀쩡하면 그대로, 없거나 깨졌으면 OCR 폴백."""
    try:
        from pypdf import PdfReader  # noqa: PLC0415 — 선택 의존성
    except ImportError:
        raise ExtractError("PDF 추출기가 없습니다 — pip install pypdf 후 다시 시도하세요.")

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            # 빈 비밀번호로 열리는 경우가 흔하다(열기 암호 없이 권한 암호만 걸린 PDF).
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                raise ExtractError("암호가 걸린 PDF입니다.")
        pages = [page.extract_text() or "" for page in reader.pages]
    except ExtractError:
        raise
    except Exception as e:  # noqa: BLE001 — 손상된 PDF는 종류가 너무 많다
        raise ExtractError(f"PDF를 읽을 수 없습니다: {str(e)[:200]}")

    text = "\n".join(pages).strip()
    if text and not _looks_garbled(text):
        return text, False

    ocr = _ocr_pdf(path)
    if ocr is not None:
        return ocr, True
    if text:
        # 예전에는 이 깨진 텍스트가 조용히 색인됐다 — 확장자를 믿지 않는 이유와 같다:
        # "읽었다"고 착각하게 만드는 것이 못 읽는 것보다 나쁘다.
        raise ExtractError(
            "PDF 텍스트가 깨져 있습니다(한글이 사설영역 코드로 저장된 HWP 계열 PDF) — "
            "Tesseract(kor)를 설치하고 PAAS_TESSERACT_PATH를 지정하면 OCR로 추출합니다.")
    # "OCR"이 앞쪽에 오게 쓴다 — index_status의 실패 이유 요약이 앞 60자만 남긴다.
    raise ExtractError(
        "PDF에 텍스트가 없습니다(스캔 이미지) — OCR로 추출하려면 Tesseract(kor)와 "
        "pip 패키지 pypdfium2·Pillow를 설치하고 PAAS_TESSERACT_PATH를 지정하세요.")


def _looks_garbled(text: str) -> bool:
    """추출은 됐지만 내용이 깨진 텍스트인가.

    HWP 계열에서 만든 PDF는 한글을 유니코드 사설영역(PUA) 코드로 심는 경우가 있어
    pypdf가 "성공"해도 검색 불가능한 글자만 나온다. **한글이 없다는 것**은 기준으로
    쓰지 않는다 — 영어 문서는 한글 0%가 정상이다. 사설영역·대체문자 비율로만 판정한다.
    """
    sample = re.sub(r"\s", "", text[:20_000])[:5_000]
    if not sample:
        return False
    bad = sum(1 for ch in sample if "\ue000" <= ch <= "\uf8ff" or ch == "\ufffd")
    return bad / len(sample) > 0.3


# 측정(합성 스캔 2쪽, 한글 규정): 150dpi + psm 6에서 쪽당 ~1초, 문자 일치 99%.
# 해상도를 올리면 느려지기만 하고(300dpi 쪽당 2.5초) 정확도는 오히려 내려갔다.
_OCR_DPI = 150
# 페이지 분할은 "단일 텍스트 블록"(psm 6)으로 못 박는다 — 기본값(자동 레이아웃 분석)은
# 같은 이미지에서 문자 일치가 21~38%까지 떨어졌다("물품 반출"이 "둘줌 반줄"이 된다).
# 이 값 하나가 이 폴백의 성패를 가른다.
_OCR_PSM = "6"
# 쪽수 상한 — 스캔 문서 하나가 색인 스레드를 분 단위로 잡지 않게 한다(쪽당 ~1초).
_OCR_MAX_PAGES = 30
_OCR_PAGE_TIMEOUT = 60


def _ocr_pdf(path: Path) -> str | None:
    """페이지를 이미지로 렌더링해 Tesseract로 읽는다. 도구가 없으면 None(호출자가 안내).

    합성 이미지 기준의 측정이므로 실제 스캔(기울어짐·도장·팩스 화질)은 이보다 낮다 —
    그래서 이 경로는 **지금 0인 문서를 살리는 폴백**으로만 쓰고, 텍스트가 멀쩡한 PDF는
    타지 않는다. 표는 평문으로 무너진다(텍스트 PDF와 같은 수준).
    """
    exe = _tesseract()
    if exe is None:
        return None
    try:
        import pypdfium2 as pdfium  # noqa: PLC0415 — 선택 의존성(렌더링)
        import PIL  # noqa: F401, PLC0415 — 렌더링 결과를 PNG로 저장할 때 필요
    except ImportError:
        return None

    texts: list[str] = []
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as e:  # noqa: BLE001 — pypdf는 열었는데 pdfium이 못 여는 경우
        raise ExtractError(f"PDF를 렌더링할 수 없습니다: {str(e)[:200]}")
    try:
        page_count = min(len(doc), _OCR_MAX_PAGES)
        with tempfile.TemporaryDirectory() as workdir:
            for index in range(page_count):
                image = doc[index].render(scale=_OCR_DPI / 72, grayscale=True).to_pil()
                png = Path(workdir) / f"page{index}.png"
                image.save(png)
                try:
                    done = subprocess.run(
                        [exe, str(png), "stdout", "-l", "kor+eng", "--psm", _OCR_PSM],
                        capture_output=True, timeout=_OCR_PAGE_TIMEOUT, check=False,
                    )
                except subprocess.TimeoutExpired:
                    raise ExtractError(
                        f"OCR이 한 쪽에서 {_OCR_PAGE_TIMEOUT}초를 넘겼습니다"
                        f"({index + 1}/{page_count}쪽).")
                if done.returncode != 0:
                    detail = (done.stderr or b"").decode("utf-8", "replace").strip()
                    raise ExtractError(
                        f"OCR이 실패했습니다{f' — {detail[:200]}' if detail else ''} "
                        "(kor 언어 데이터가 설치되어 있는지 확인)")
                texts.append(done.stdout.decode("utf-8", "replace"))
        if len(doc) > page_count:
            # 잘렸다는 사실을 본문에 남긴다 — 뒷부분이 검색에 안 걸리는 이유를
            # 문서를 연 사람이 바로 알 수 있어야 한다.
            texts.append(f"(OCR은 앞 {page_count}쪽까지만 추출했습니다 — 전체 {len(doc)}쪽)")
    finally:
        doc.close()
    text = "\n".join(texts).strip()
    return text or None


def _tesseract() -> str | None:
    configured = get_settings().tesseract_path
    return shutil.which(configured) if configured else shutil.which("tesseract")


# --- 97-2003 바이너리 오피스 ---

def _legacy_office_text(path: Path) -> str:
    """.doc/.xls/.ppt(OLE) — LibreOffice에 맡긴다.

    순수 파이썬으로 이 형식을 제대로 뽑는 방법이 없다. 서버에 LibreOffice가 있으면
    그걸 쓰고, 없으면 무엇을 하면 되는지 알린다(조용히 깨진 텍스트를 주지 않는다).
    """
    exe = _soffice()
    if exe is None:
        raise ExtractError(
            "97-2003 바이너리 형식(doc·xls·ppt)입니다 — LibreOffice를 설치하고 "
            "PAAS_SOFFICE_PATH에 soffice 실행 파일 경로를 지정하면 추출합니다. "
            "docx·xlsx·pptx로 저장된 파일은 그대로 읽힙니다."
        )
    with tempfile.TemporaryDirectory() as outdir:
        try:
            done = subprocess.run(
                [exe, "--headless", "--convert-to", "txt:Text", "--outdir", outdir, str(path)],
                capture_output=True, timeout=_SOFFICE_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired:
            raise ExtractError(f"변환이 {_SOFFICE_TIMEOUT}초를 넘겼습니다.")
        produced = list(Path(outdir).glob("*.txt"))
        if not produced:
            # 실패 원인이 대개 "필터 미설치"(libreoffice-core만 깔린 경우)라서 출력을
            # 함께 싣는다 — 이 문구만 보고 설치 상태를 되짚을 수 있어야 한다.
            detail = (done.stderr or done.stdout or b"").decode("utf-8", "replace").strip()
            raise ExtractError(
                "LibreOffice가 텍스트를 만들지 못했습니다"
                f"{f' — {detail[:200]}' if detail else ' (writer·calc 필터가 설치되어 있는지 확인)'}")
        return _decode(produced[0].read_bytes())


def _soffice() -> str | None:
    configured = get_settings().soffice_path
    return shutil.which(configured) if configured else shutil.which("soffice")


# --- 평문 ---

def _eml_markdown(path: Path) -> str:
    """메일 한 통(.eml) — 제목·보낸 사람·받는 사람·시각과 본문. 첨부는 이름만 남긴다.

    웹 Outlook에서 메일을 내려받으면 이 형식이다. 본문은 MIME(base64·인코딩된 헤더)이라
    평문으로 읽으면 알아볼 수 없으므로 파서로 푼다.
    """
    try:
        msg = BytesParser(policy=email_policy.default).parsebytes(path.read_bytes())
        body = msg.get_body(preferencelist=("plain", "html"))
        content = body.get_content() if body is not None else ""
        if body is not None and body.get_content_type() == "text/html":
            content = re.sub(r"(?is)<(script|style).*?</\1>", "", content)
            content = unescape(re.sub(r"<[^>]+>", " ", content))
            content = re.sub(r"[ \t]+", " ", re.sub(r"\s*\n\s*", "\n", content))
        names = [part.get_filename() for part in msg.iter_attachments() if part.get_filename()]
        lines = [
            f"# {str(msg['subject'] or '(제목 없음)').strip()}", "",
            f"- 보낸 사람: {msg['from'] or ''}",
            f"- 받는 사람: {msg['to'] or ''}",
            f"- 받은 시각: {msg['date'] or ''}",
        ]
        if names:
            lines.append(f"- 첨부: {', '.join(names)}")
        return "\n".join([*lines, "", content.strip(), ""])
    except (OSError, ValueError, LookupError) as e:
        raise ExtractError(f"메일을 읽을 수 없습니다: {e}")


def _plain_text(path: Path) -> str:
    return _decode(path.read_bytes())


def _decode(data: bytes) -> str:
    """utf-8 → cp949 순서. 한국어 윈도우에서 만든 txt·csv는 cp949인 경우가 많다."""
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
