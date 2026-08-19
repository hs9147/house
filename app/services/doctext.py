"""문서 파일 → 텍스트 추출 — 사내 문서 폴더를 LLM이 읽을 수 있게 만드는 유일한 경로.

바이트를 utf-8로 그냥 디코드하면 .pdf·.docx·.xlsx는 깨진 글자만 나온다. 여기서 형식을
판별해 본문 텍스트만 뽑는다.

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


def extract_text(path: Path) -> str:
    """문서 하나의 본문 텍스트. 못 뽑으면 ExtractError(이유를 담아서)."""
    try:
        head = path.open("rb").read(8)
    except OSError as e:
        raise ExtractError(f"파일을 열 수 없습니다: {e}")

    if head[:4] == b"PK\x03\x04":
        text = _ooxml_text(path)
    elif head[:4] == b"%PDF":
        text = _pdf_text(path)
    elif head[:4] == _OLE_MAGIC:
        text = _legacy_office_text(path)
    else:
        text = _plain_text(path)
    return text[:MAX_TEXT_CHARS]


# --- zip + XML 계열 (docx·xlsx·pptx·hwpx) ---

# (본문 엔트리 판별, 텍스트 태그 로컬명, 블록 태그 로컬명)
# 블록 태그는 줄바꿈 자리다 — 없으면 문서 전체가 한 줄로 붙어 검색 발췌가 쓸모없어진다.
_OOXML_KINDS = [
    (re.compile(r"^word/document\.xml$"), "t", "p"),
    (re.compile(r"^ppt/slides/slide\d+\.xml$"), "t", "p"),
    (re.compile(r"^Contents/section\d+\.xml$"), "t", "p"),  # hwpx
]
_SHEET_RE = re.compile(r"^xl/worksheets/sheet\d+\.xml$")


def _ooxml_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if "xl/workbook.xml" in names:
                return _xlsx_text(zf, names)
            for pattern, text_tag, block_tag in _OOXML_KINDS:
                entries = sorted((n for n in names if pattern.match(n)), key=_entry_order)
                if not entries:
                    continue
                parts = [_xml_text(zf.read(n), text_tag, block_tag) for n in entries]
                return "\n".join(p for p in parts if p)
    except zipfile.BadZipFile as e:
        raise ExtractError(f"압축이 깨졌습니다: {e}")
    raise ExtractError(
        "zip 파일이지만 문서 형식이 아닙니다(docx·xlsx·pptx·hwpx가 아님).")


def _xlsx_text(zf: zipfile.ZipFile, names: list[str]) -> str:
    """스프레드시트는 행 단위로 뽑는다 — 셀 값을 탭으로 이은 한 줄이 한 행.

    문자열이 어디 있는지가 두 갈래다: 엑셀이 저장한 파일은 xl/sharedStrings.xml에 모아
    두고 셀에서 번호로 참조하지만(t="s"), 라이브러리로 만든 파일은 셀 안에 그대로
    넣는다(t="inlineStr"). 둘 다 받는다 — 실제 파일로 확인한 결과 이 차이 때문에 한쪽만
    보면 통째로 빈 텍스트가 나온다.
    """
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        try:
            root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
        except ElementTree.ParseError as e:
            raise ExtractError(f"sharedStrings.xml을 읽을 수 없습니다: {e}")
        for si in root:
            shared.append("".join(t.text or "" for t in si.iter() if _local(t) == "t"))

    lines: list[str] = []
    for sheet in sorted((n for n in names if _SHEET_RE.match(n)), key=_entry_order):
        try:
            root = ElementTree.fromstring(zf.read(sheet))
        except ElementTree.ParseError as e:
            raise ExtractError(f"{sheet}을 읽을 수 없습니다: {e}")
        for row in root.iter():
            if _local(row) != "row":
                continue
            cells = [_cell_text(cell, shared) for cell in row if _local(cell) == "c"]
            if any(cells):
                lines.append("\t".join(cells))
    return "\n".join(lines)


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
