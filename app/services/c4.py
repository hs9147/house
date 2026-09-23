"""C4 모델 시각화 — 기획 산출물 안의 mermaid C4 블록을 그래프로 뽑는다(정적 파싱, LLM 없음).

**왜 문서가 원천인가.** 확정 산출물은 프로젝트 Gitea 리포에 커밋되어 외부 개발도구에서
그대로 열린다(services/planning). 다이어그램을 별도 표나 LLM 추출로 따로 두면 문서와
그림이 갈라지고, 콘솔 밖에서는 그림을 볼 수 없다. 단계 프롬프트가 문서 안에 mermaid C4
블록을 쓰게 하고(planning.C4_BLOCK_GUIDE) 콘솔은 그 블록을 읽는다 — 같은 그림이
Gitea·VSCode 프리뷰에서도 렌더된다.

**왜 LLM이 아닌가.** codemap.py·ontology.py와 같은 판단이다. 블록의 형태가 정해져 있어
규칙으로 읽을 수 있고, 조회마다 LLM을 부르면 비용이 붙고 프로바이더가 설정되지 않은
설치본에서는 화면이 반쪽이 된다.

레벨과 기획 단계의 대응:

  context    ① 기획서 확정 후 — 사용자(Person)와 외부 환경(System_Ext).
             ③ 솔루션 구성에서 내외부 솔루션으로 구체화된다.
  container  ② 아키텍처 설계 확정 후. ③에서 실제 내부 모듈·기술로 구체화된다.
  component  ② 아키텍처 설계 확정 후. ③에서 구체화된다.
  code       구현된 component별 — 리포 정적 파싱(codemap.py)이 원천이고, 어느 파일이
             그 component인지는 문서의 `$link`가 말한다(component_paths).
"""
import re

# 문서에서 읽는 C4 레벨. code는 문서가 아니라 리포에서 나오므로 여기 없다.
LEVELS = ("context", "container", "component")

# 펜스 블록 하나. 여는 펜스와 같은 문자·같은 길이로 닫힌 것만 블록으로 본다.
_BLOCK_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n(.*?)^[ \t]*\1[ \t]*$", re.S | re.M)
# 블록 첫 줄의 다이어그램 종류 — 이것이 레벨을 정한다.
_DIAGRAM_RE = re.compile(r"^C4(Context|Container|Component)\b", re.I)
# `Kind(인자들)` 한 줄. 뒤에 `{`가 붙으면 경계가 열린다(자식 선언이 이어진다).
_CALL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*(\{)?\s*$")

# 한 블록에서 읽을 요소·관계 상한 — 문서 하나가 화면을 통째로 먹지 않게 한다
# (ontology.MAX_NODES_PER_DOC과 같은 이유).
MAX_ELEMENTS = 200
MAX_RELATIONS = 400

_BOUNDARY_KINDS = {
    "boundary": "",
    "enterprise_boundary": "enterprise",
    "system_boundary": "system",
    "container_boundary": "container",
}
_BASES = ("person", "system", "container", "component")
_REL_KINDS = {
    "rel", "birel",
    "rel_back", "rel_neighbor",
    "rel_u", "rel_up", "rel_d", "rel_down",
    "rel_l", "rel_left", "rel_r", "rel_right",
}
# 선언별 위치 인자 이름. mermaid C4는 종류마다 인자 수가 달라서 이름을 여기서 붙인다.
_POSITIONAL = {
    "person": ("alias", "label", "description"),
    "system": ("alias", "label", "description"),
    "container": ("alias", "label", "technology", "description"),
    "component": ("alias", "label", "technology", "description"),
    "boundary": ("alias", "label", "boundary_type"),
}
# `$name=값` 이름 인자 → 요소 필드. mermaid가 지원하는 것 중 그림에 필요한 것만 읽는다.
_NAMED = {
    "label": "label", "descr": "description", "techn": "technology",
    "link": "link", "tags": "tags", "type": "boundary_type",
}


def extract_blocks(markdown: str) -> dict[str, str]:
    """마크다운 펜스 블록에서 C4 다이어그램 본문을 레벨별로 뽑는다.

    같은 레벨 블록이 한 문서에 여러 개면 마지막 것을 쓴다 — 문서를 고칠 때 갱신본을
    아래에 덧붙이는 편집이 흔하고, 그때 확정 의도는 나중 것이다.
    """
    blocks: dict[str, str] = {}
    for match in _BLOCK_RE.finditer(markdown or ""):
        body = match.group(2)
        head = next((line.strip() for line in body.splitlines() if line.strip()), "")
        kind = _DIAGRAM_RE.match(head)
        if kind:
            blocks[kind.group(1).lower()] = body
    return blocks


def parse_block(body: str) -> dict:
    """C4 다이어그램 본문 → {"title", "elements", "relations"}.

    읽지 못한 줄은 조용히 건너뛴다 — 스타일 지시(UpdateElementStyle 등)나 오타 한 줄
    때문에 그림 전체를 잃는 쪽이 더 나쁘다.
    """
    title = ""
    elements: list[dict] = []
    relations: list[dict] = []
    stack: list[str] = []  # 열려 있는 경계의 alias(중첩 가능)

    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("%%"):
            continue
        if line.startswith("}"):
            if stack:
                stack.pop()
            continue
        if line.lower().startswith("title "):
            title = line[6:].strip()
            continue
        match = _CALL_RE.match(line)
        if match is None:
            continue
        kind, args_raw, opens = match.group(1), match.group(2), bool(match.group(3))
        args = _split_args(args_raw)

        if kind.lower() in _REL_KINDS:
            rel = _relation(kind, args)
            if rel and len(relations) < MAX_RELATIONS:
                relations.append(rel)
            continue

        element = _element(kind, args, stack[-1] if stack else None)
        if element is None:
            continue
        if len(elements) < MAX_ELEMENTS:
            elements.append(element)
        if opens and element["base"] == "boundary":
            stack.append(element["alias"])

    return {"title": title, "elements": elements, "relations": relations}


def same_blocks(left: str, right: str) -> bool:
    """두 문서가 **같은 그림**을 담고 있는가(C4 블록만 비교, 여백 무시).

    확정된 단계를 열어 보기만 해도 "초안(미확정)"으로 표시되던 문제 때문에 필요하다.
    편집기에는 확정본 본문이 들어 있으므로 문서는 확정본과 같은데, 화면은 "지금 편집 중인
    단계"라는 사실만 보고 초안이라고 말했다.

    문서 전체가 아니라 블록만 비교한다 — 라벨이 말하는 것은 그림이다. 산문을 고쳤다는
    이유로 그림이 미확정이 되면 안 된다.
    """
    def blocks(text: str) -> dict[str, str]:
        return {level: body.strip() for level, body in extract_blocks(text).items()}

    return blocks(left) == blocks(right)


def model_from_stages(
    stage_docs: list[tuple[str, str]], draft_stage: str | None = None,
) -> dict[str, dict]:
    """단계별 (stage, 산출물 본문) → 레벨별 다이어그램.

    같은 레벨을 여러 단계가 그리면 **뒤 단계**가 이긴다. 아키텍처가 그린 컨테이너를
    솔루션 구성이 실제 모듈·기술로 고쳐 다시 싣는 것이 정상 흐름이므로, 최신 그림은
    뒤 단계의 것이다. 어느 단계에서 온 그림인지는 stage로 함께 돌려준다 —
    화면에서 "이 그림은 ③ 솔루션 구성이 확정한 것"임을 밝혀야 한다.

    draft_stage로 넘긴 단계의 본문은 아직 확정되지 않은 편집 중 초안이다. 그림은 확정을
    **검토하기 위한** 도구이고 확정은 그 검토의 결과이므로, 초안 단계에서 이미 보여야
    한다 — 레벨마다 confirmed로 어느 쪽인지 밝혀 화면이 "미확정 초안"을 표시할 수 있게 한다.

    stage_docs는 단계 순서대로 들어와야 한다(planning.STAGE_ORDER).
    """
    levels: dict[str, dict] = {}
    for stage, content in stage_docs:
        for level, body in extract_blocks(content).items():
            parsed = parse_block(body)
            if parsed["elements"]:
                levels[level] = {"stage": stage, "confirmed": stage != draft_stage, **parsed}
    return levels


def component_paths(element: dict, tree: list[str]) -> list[str]:
    """component가 구현된 리포 파일 경로 — code 레벨의 대상이다.

    문서가 `$link`로 경로를 적어 주면 그것이 원천이다(접두사 일치라 파일 하나든
    디렉터리든 같은 규칙으로 걸린다). 적혀 있지 않으면 alias·label에서 뽑은 토큰이
    경로에 들어 있는 파일로 떨어진다 — `$link` 관례가 생기기 전에 확정된 문서에서도
    코드 레벨이 통째로 비지 않게 하는 최소 폴백이다.
    """
    link = (element.get("link") or "").strip().strip("/")
    if link:
        return [p for p in tree if p == link or p.startswith(link + "/")]
    tokens = _tokens(element.get("alias", "")) | _tokens(element.get("label", ""))
    if not tokens:
        return []
    return [p for p in tree if any(t in _normalize(p) for t in tokens)]


def _tokens(text: str) -> set[str]:
    """경로 대조에 쓸 낱말 — 세 글자 미만은 아무 경로에나 걸려서 뺀다."""
    return {t for t in re.split(r"[^A-Za-z0-9]+", _normalize(text)) if len(t) >= 3}


def _normalize(text: str) -> str:
    return text.lower().replace("-", "_")


def _split_args(raw: str) -> list[str]:
    """선언의 인자 목록을 나눈다 — 따옴표·괄호 안의 콤마는 구분자가 아니다.

    따옴표는 값에서 벗겨진다(라벨에 "가 남으면 그대로 화면에 찍힌다).
    """
    args: list[str] = []
    buf: list[str] = []
    quote = ""
    depth = 0
    for ch in raw:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    args.append("".join(buf).strip())
    return args


def _classify(kind: str) -> tuple[str, bool, str, str] | None:
    """선언 이름 → (base, external, shape, boundary_type). 모르는 선언은 None."""
    key = kind.lower()
    if key in _BOUNDARY_KINDS:
        return "boundary", False, "boundary", _BOUNDARY_KINDS[key]
    external = "_ext" in key
    key = key.replace("_ext", "").rstrip("_")
    shape = "box"
    for suffix in ("db", "queue"):
        if key.endswith(suffix) and key != suffix:
            key, shape = key[: -len(suffix)], suffix
            break
    if key not in _BASES:
        return None
    return key, external, ("person" if key == "person" else shape), ""


def _element(kind: str, args: list[str], parent: str | None) -> dict | None:
    fields = _classify(kind)
    if fields is None:
        return None
    base, external, shape, boundary_type = fields
    element = {
        "kind": kind, "base": base, "alias": "", "label": "", "technology": "",
        "description": "", "external": external, "shape": shape,
        "boundary_type": boundary_type, "parent": parent, "link": "", "tags": "",
        "paths": [],
    }
    names = _POSITIONAL[base]
    position = 0
    for arg in args:
        named = re.match(r"^\$([A-Za-z]+)\s*=\s*(.*)$", arg)
        if named:
            field = _NAMED.get(named.group(1).lower())
            if field:
                element[field] = named.group(2).strip()
            continue
        if position < len(names):
            element[names[position]] = arg
        position += 1
    if not element["alias"]:
        return None
    if not element["label"]:
        element["label"] = element["alias"]
    return element


def _relation(kind: str, args: list[str]) -> dict | None:
    positional = [a for a in args if not a.startswith("$")]
    if len(positional) < 2 or not positional[0] or not positional[1]:
        return None
    return {
        "source": positional[0],
        "target": positional[1],
        "label": positional[2] if len(positional) > 2 else "",
        "technology": positional[3] if len(positional) > 3 else "",
        "bidirectional": kind.lower() == "birel",
    }
