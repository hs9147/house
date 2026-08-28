"""문서 온톨로지 — `.ready` 마크다운에서 그래프를 뽑는다(정적 파싱, LLM 호출 없음).

**왜 LLM이 아닌가.** 색인 루프는 한 번에 20초 예산으로 수천 건을 돈다(docsearch.reindex).
문서마다 LLM을 부르면 처리량이 무너지고 비용이 문서 수에 비례해 붙는다. 게다가 그러면
LLM 프로바이더가 설정되지 않은 설치본에서는 색인이 반쪽이 된다. 코드에 대해 같은 판단을
한 곳이 이미 있다(codemap.py — 정적 파싱).

**왜 지금 뽑을 수 있나.** `.ready` 마크다운은 원본의 구조를 살려 둔다 — docx 제목 단계는
`#`으로, 표는 머리글이 있는 마크다운 표로 나온다(doctext.py). 그래서 "문서 → 절 → 표"
계층과 표의 컬럼 이름은 **추정이 아니라 사실**로 읽을 수 있다. 사내 규정 문서에 흔한
`제N조(제목)`·`"X"란 …`·`(이하 "Y")`·`「문서명」`도 형태가 일정해서 규칙으로 잡힌다.

무엇을 뽑는가:

  document  문서 하나                     detail=제목
  section   제목 한 줄(깊이 포함)          detail=제N조 번호
  term      정의된 낱말                    detail=정의문
  table     표 하나                        detail=컬럼 이름 목록 ← "스키마"가 여기서 나온다

  contains    문서→절, 절→하위 절, 절→표
  defines     절→용어
  references  절→다른 문서(「」로 인용한 것)

**노드는 문서 단위다.** 같은 용어가 여러 문서에 정의돼 있으면 노드도 그 수만큼 생긴다.
문서를 지울 때 path 하나로 정확히 지울 수 있고(색인 행의 수명과 같다), 문서를 가로지르는
연결은 조회 때 **이름으로** 맺는다(api/mcp_servers.py의 /mcp/graph). 전역 용어 노드를
하나 두면 그 노드를 언제 지워야 하는지가 문서 삭제마다 문제가 되는데, 얻는 것은 조회
한 번에 할 수 있는 이름 맞추기뿐이다.
"""
import re

# 제목 한 줄. doctext가 docx의 제목 단계를 그대로 옮겨 놓는다.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.M)
# 사내 규정의 조문. 제목 안에 있으면 그 절의 번호가 되고, 본문에 있으면 인용이다.
_ARTICLE_RE = re.compile(r"제\s*(\d{1,3})\s*조")
# `"X"란 …` / `"X"라 함은` — 정의문의 가장 흔한 두 모양.
_QUOTE = "\"'“”‘’"
_DEFINED_RE = re.compile(
    rf"[{_QUOTE}]([^{_QUOTE}\n]{{1,40}})[{_QUOTE}]\s*(?:이란|란|라\s*함은)\s*([^\n]{{0,200}})"
)
# `(이하 "Y"라 한다)` — 본이름과 줄임말을 잇는다. 사내 문서에서 이후 본문은 줄임말만 쓴다.
_ALIAS_RE = re.compile(
    rf"\(\s*이하\s*[{_QUOTE}]?([^{_QUOTE}()\n]{{1,40}}?)[{_QUOTE}]?\s*(?:이?라\s*(?:한다|칭한다))?\s*\)"
)
# 「문서명」·〈문서명〉 — 다른 문서를 가리키는 인용 부호.
_DOCREF_RE = re.compile(r"[「〈]([^」〉\n]{2,60})[」〉]")
# 마크다운 표의 구분선. 바로 윗줄이 머리글이다.
_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{0,}:?\s*\|?\s*$")

# 정의문에서 잘라 낼 꼬리. 문장을 통째로 담으면 detail이 길어지기만 한다.
_DEF_TAIL = 160
# 한 문서에서 뽑을 노드 상한. 표가 수백 개인 엑셀 한 건이 그래프를 통째로 먹지 않게 한다.
MAX_NODES_PER_DOC = 400


def _cells(line: str) -> list[str]:
    """마크다운 표 한 줄 → 셀 목록. 이스케이프된 파이프(`\\|`)는 값의 일부다."""
    body = line.strip().strip("|")
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", body)]


def _node(kind: str, name: str, detail: str = "", depth: int = 0) -> dict:
    return {"kind": kind, "name": name[:200], "detail": detail[:400], "depth": depth}


def _key(node: dict) -> str:
    """문서 안에서의 노드 식별자. 같은 이름이 두 번 나오면 같은 노드로 본다 —
    절 제목이 반복되는 문서에서 노드가 불어나는 것을 막는다."""
    return f"{node['kind']}:{node['name']}"


def extract(rel_path: str, title: str, markdown: str) -> tuple[list[dict], list[dict]]:
    """(노드, 엣지). 엣지는 노드 키(_key)로 잇는다 — 저장은 docsearch가 한다."""
    doc = _node("document", title or rel_path)
    nodes: dict[str, dict] = {_key(doc): doc}
    edges: list[dict] = []

    def add(node: dict) -> str:
        key = _key(node)
        if key not in nodes and len(nodes) < MAX_NODES_PER_DOC:
            nodes[key] = node
        return key

    def link(src: str, rel: str, dst: str) -> None:
        if src in nodes and dst in nodes:
            edges.append({"src": src, "rel": rel, "dst": dst})

    doc_key = _key(doc)
    # 절 계층 — 깊이가 줄어들면 그만큼 조상 스택을 접는다.
    stack: list[tuple[int, str]] = []
    current = doc_key

    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        heading = _HEADING_RE.match(line)
        if heading:
            depth = len(heading.group(1))
            name = heading.group(2).strip()
            article = _ARTICLE_RE.search(name)
            key = add(_node("section", name, f"제{article.group(1)}조" if article else "", depth))
            while stack and stack[-1][0] >= depth:
                stack.pop()
            link(stack[-1][1] if stack else doc_key, "contains", key)
            stack.append((depth, key))
            current = key
            continue

        # 표: 구분선을 만나면 바로 윗줄이 머리글이다. 컬럼 이름이 곧 스키마다.
        if _TABLE_SEP_RE.match(line) and i > 0:
            columns = [c for c in _cells(lines[i - 1]) if c]
            if len(columns) >= 2:
                key = add(_node("table", " | ".join(columns), f"columns={len(columns)}"))
                link(current, "contains", key)
            continue

        for match in _DEFINED_RE.finditer(line):
            key = add(_node("term", match.group(1).strip(), match.group(2).strip()[:_DEF_TAIL]))
            link(current, "defines", key)
        for match in _ALIAS_RE.finditer(line):
            key = add(_node("term", match.group(1).strip(), "줄임말(이하 …라 한다)"))
            link(current, "defines", key)
        for match in _DOCREF_RE.finditer(line):
            key = add(_node("document", match.group(1).strip(), "인용된 문서"))
            link(current, "references", key)

    return list(nodes.values()), edges
