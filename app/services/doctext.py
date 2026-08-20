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
  %PDF           pdf                  pypdf(선택 의존성). 스캔 이미지 PDF는 텍스트가
                                      없으므로 OCR이 필요하다고 알린다.
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


class ExtractError(RuntimeError):
    """추출할 수 없는 파일 — 형식 미지원, 드라이버 없음, 손상."""


def extract(path: Path) -> tuple[str, str]:
    """문서 하나를 (마크다운, 검색용 평문)으로. 못 뽑으면 ExtractError(이유를 담아서).

    한 번만 열고 한 번만 파싱한다 — 두 모양이 어긋나면 "검색에는 걸리는데 읽으면 없는"
    상태가 된다. 구조를 담을 수 없는 형식(pdf·평문·97-2003)은 둘이 같은 값이다.
    """
    try:
        head = path.open("rb").read(8)
    except OSError as e:
        raise ExtractError(f"파일을 열 수 없습니다: {e}")

    if head[:4] == b"PK\x03\x04":
        markdown = _ooxml_markdown(path)[:MAX_TEXT_CHARS]
        # 벗기기는 **우리가 만든 마크다운에만** 적용한다. 공유 폴더에 있는 .md나
        # 파이프가 든 csv를 평문 취급하다 벗기면 원문을 망가뜨린다.
        return markdown, to_plain(markdown)

    if head[:4] == b"%PDF":
        text = _pdf_text(path)
    elif head[:4] == _OLE_MAGIC:
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
            for pattern in (_SLIDE_RE, _HWPX_RE):
                entries = sorted((n for n in names if pattern.match(n)), key=_entry_order)
                if not entries:
                    continue
                # 단락 사이는 빈 줄로 — 마크다운에서 줄바꿈 하나는 같은 문단의 이어짐이다.
                parts = [_xml_text(zf.read(n), "t", "p") for n in entries]
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
    """(셀 텍스트, 가로 병합 칸 수). 세로 병합은 칸 수를 바꾸지 않으므로 빈 셀로 남는다."""
    rows = []
    for tr in (c for c in tbl if _local(c) == "tr"):
        cells = []
        for tc in (c for c in tr if _local(c) == "tc"):
            text = "".join(t.text or "" for t in tc.iter() if _local(t) == "t")
            span = next((_attr(g, "val") for g in tc.iter() if _local(g) == "gridSpan"), "")
            cells.append((text, int(span) if span.isdigit() else 1))
        if cells:
            rows.append(cells)
    return rows


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
            cells = [_cell_text(cell, shared) for cell in row if _local(cell) == "c"]
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

def _pdf_text(path: Path) -> str:
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
    if not text:
        raise ExtractError(
            "PDF에서 텍스트를 찾지 못했습니다 — 스캔 이미지 PDF일 수 있습니다(OCR 필요).")
    return text


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
