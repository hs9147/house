"""워크플로 구성 대화 — LLM이 스펙을 쓰고, **검증기가 그것을 받는다.**

산업과 연구가 같은 모양을 쓴다: 구조화 스펙을 LLM이 생성 → 스키마·그래프 검증 → 사람이
대화로 고친다(ProMoAI 2024는 생성·오류 처리·수정 루프를 그대로 보고하고, n8n의 AI workflow
builder도 LLM이 노드와 연결을 만들고 사람이 캔버스에서 고친다). 그래서 여기서 하는 일은
"프롬프트를 잘 쓰는 것"이 아니라 **되돌려 고치게 하는 것**이다 — 기동 스크립트 작성과 같은
한 번의 수선(startscript.propose)을 쓴다.

**제약사항이 이 생성을 하네싱한다.** 그 제약은 **조직의 업무 규칙**이다
(workflow_constraints) — 선급금 한도, 평가 순서, 결재선 같은 것. 기획의 공통 제약사항은
싣지 않는다: 그쪽은 에이전트를 **개발**할 때의 제한이고(프록시 구조·외부 솔루션 금지),
구매 업무 워크플로가 지킬 규칙이 아니다. 실측에서 섞어 본 결과가 그랬다 — 평가에 개발 제약이
실려 "이 규칙을 지키는 단계가 없습니다"가 떴고, 맞는 말이지만 쓸모가 없었다.
지키지 못할 요청이면 스펙을 만들지 말고 무엇이 걸리는지 말하게 한다.

제안은 **저장하지 않는다.** 화면이 캔버스에 올려 사람이 보고 저장을 누른다 — LLM이 쓴 것이
조용히 운영 자원을 건드리는 흐름이 되면 안 된다(기동 스크립트와 같은 자리다).
"""
import json
import re

from sqlalchemy.orm import Session

from ..models import Workflow
from . import llm, workflow as workflow_service

# 대화에 싣는 직전 이력. 길면 자원 목록과 제약사항이 밀린다 — 그쪽이 사실이고 이력은 맥락이다.
HISTORY_TURNS = 8

PROMPT = """당신은 사내 PaaS의 워크플로를 설계한다. 출력은 **JSON 객체 하나**다.

{{"summary": "무엇을 바꿨는지 두세 문장(한국어)",
  "spec": {{"nodes": [...], "edges": [...]}},
  "extracted": {{
    "entities":    [{{"name": "다루는 대상", "note": "한 줄 설명"}}],
    "states":      [{{"entity": "대상 이름", "name": "그 대상이 거치는 상태", "note": ""}}],
    "transitions": [{{"from": "상태", "to": "상태", "trigger": "무엇이 일어나면",
                     "node": "그 일을 하는 단계 id(없으면 \\"\\")"}}],
    "constraints": [{{"text": "지켜야 하는 규칙", "origin": "대화 또는 조직",
                     "node": "그 규칙을 지키는 단계 id(반영 못 했으면 \\"\\")"}}]
  }}}}

extracted는 **대화에서 읽어 낸 업무**다 — 스펙과 별개로 내놓는다. 스펙만 보면 "그럴듯한데
내 업무가 아닌" 워크플로를 사람이 알아볼 수 없기 때문이다. 대화에 없는 것을 채우지 말고,
못 읽은 자리는 빈 배열로 둔다. 제약은 대화에서 나온 것과 아래 업무 제약사항을 모두 싣고,
스펙의 어느 단계가 그것을 지키는지 node에 적는다(지키는 단계를 못 만들었으면 비운다).

규칙(어기면 저장이 거부된다):
- 코드펜스(```)나 설명 문장을 JSON 밖에 쓰지 않는다.
- 노드는 {{"id","type",...}}. id는 짧은 한국어·영문 낱말이고 공백·슬래시를 쓰지 않는다.
- 연결은 {{"from","to"}}. 분기에서 나가는 연결에만 {{"case": "참"}} 또는 {{"case": "거짓"}}를 둔다.
- 쓸 수 있는 노드 종류와 항목은 아래 목록에 있는 것뿐이다. 다른 항목을 적으면 거부된다.
- 저장소·모듈·LLM 프로바이더는 **아래 목록에 있는 이름만** 쓴다. 없으면 그 단계를 만들지
  말고 summary에 무엇이 없어서 못 했는지 적는다.
- 고리(cycle)를 만들지 않는다. 모든 노드는 적어도 하나의 연결에 닿는다.
- 비밀값(API 키·비밀번호·토큰)을 스펙에 적지 않는다. 자격증명은 플랫폼이 주입한다.
- 사람의 판단이 필요한 자리에는 human 노드를 둔다(승인·검토·입력). 사람 단계에서 실행은
  멈추고 사람이 제출하면 이어진다.

쓸 수 있는 노드 종류:
{node_types}

이 조직이 쓸 수 있는 자원:
{resources}

반드시 지켜야 하는 이 조직의 업무 제약사항:
{constraints}

현재 스펙:
{current}

요청: {request}
"""

REPAIR = """위 스펙은 검증에서 거부되었다. 아래 문제를 **전부** 고친 JSON을 다시 낸다
(같은 형식, 코드펜스 없이):

{problems}
"""


def propose(db: Session, workflow: Workflow, request: str, history: list[dict]) -> dict:
    """요청 하나를 받아 새 스펙을 제안한다.

    돌려주는 것: spec·summary·problems·attempts·provider·facts. problems가 비어 있지 않으면
    화면은 **저장 버튼을 막는다** — 통과하지 못한 것을 경고만 하고 넘기지 않는다.
    facts를 함께 주는 이유는 기동 스크립트와 같다: 무엇을 보고 쓴 것인지 사람이 확인할 수
    있어야 한다.
    """
    provider = llm.default_provider(db)
    if provider is None:
        raise workflow_service.WorkflowError(
            "기본 LLM 프로바이더가 없습니다 — LLM 관리에서 「설정」을 누르세요.")

    resources = workflow_service.resources(db, workflow.organization_id)
    constraints = workflow_service.constraints(db, workflow.organization_id)
    facts = PROMPT.format(
        node_types="\n".join(
            f"- {t['type']}({t['label']}): {t['help']}"
            f" 필수={list(t['required'])} 선택={list(t['optional'])}"
            for t in resources["node_types"]),
        resources=json.dumps({
            "stores": resources["stores"], "modules": resources["modules"],
            "providers": resources["providers"],
            "default_provider": resources["default_provider"],
        }, ensure_ascii=False, indent=2),
        constraints="\n".join(f"- {c}" for c in constraints) or "- (등록된 제약사항 없음)",
        current=workflow_service.spec_as_text(workflow.spec or {}),
        request=request.strip(),
    )

    messages = [{"role": "system", "content": facts}]
    for turn in history[-HISTORY_TURNS:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        messages.append({"role": role, "content": str(turn.get("content") or "")[:4000]})
    messages.append({"role": "user", "content": request.strip()})

    attempts = 1
    summary, spec, extracted = _ask(provider, messages, db)
    problems = workflow_service.validate(db, workflow.organization_id, spec)
    if problems:
        # 한 번만 되돌린다 — 두 번 이상은 같은 실수를 되풀이하는 쪽이 많았다(기동 스크립트
        # 작성에서 측정한 것과 같다).
        attempts = 2
        messages.append({"role": "assistant",
                         "content": json.dumps({"summary": summary, "spec": spec},
                                               ensure_ascii=False)})
        messages.append({"role": "user",
                         "content": REPAIR.format(
                             problems="\n".join(f"- {p}" for p in problems))})
        summary, spec, extracted = _ask(provider, messages, db)
        problems = workflow_service.validate(db, workflow.organization_id, spec)

    return {
        "summary": summary, "spec": spec, "extracted": extracted,
        "problems": problems, "review": review(spec, extracted, constraints),
        "attempts": attempts, "provider": provider.name, "facts": facts,
    }


def review(spec: dict, extracted: dict, constraints: list[str]) -> list[str]:
    """읽어 낸 것과 스펙이 **맞물리는지** 기계가 볼 수 있는 만큼 본다.

    저장을 막지 않는다(그건 validate의 일이다) — 여기서 나오는 것은 사람이 검토할 자리다.
    "제약을 읽기는 했는데 어느 단계도 그걸 지키지 않는다"가 가장 중요한 경우고, 그건 스펙만
    봐서는 보이지 않는다.
    """
    notes: list[str] = []
    if not isinstance(extracted, dict) or not extracted:
        return ["대화에서 읽어 낸 업무(개체·상태·전이·제약)가 없습니다 — 검토할 수 없습니다."]
    node_ids = {str(n.get("id")) for n in (spec or {}).get("nodes", [])
                if isinstance(n, dict)}
    entities = {str(e.get("name") or "") for e in _rows(extracted, "entities")}

    for state in _rows(extracted, "states"):
        owner = str(state.get("entity") or "")
        if owner and owner not in entities:
            notes.append(f"상태 '{state.get('name')}'의 대상 '{owner}'이 개체 목록에 없습니다.")
    for move in _rows(extracted, "transitions"):
        node = str(move.get("node") or "")
        if node and node not in node_ids:
            notes.append(
                f"전이 '{move.get('from')} → {move.get('to')}'가 없는 단계 '{node}'를 가리킵니다.")
        if not node:
            notes.append(
                f"전이 '{move.get('from')} → {move.get('to')}'를 수행하는 단계가 없습니다.")
    seen = []
    for rule in _rows(extracted, "constraints"):
        text = str(rule.get("text") or "")
        seen.append(text)
        node = str(rule.get("node") or "")
        if node and node not in node_ids:
            notes.append(f"제약 '{text[:40]}'이 없는 단계 '{node}'를 가리킵니다.")
        elif not node:
            notes.append(f"제약 '{text[:40]}'을 지키는 단계가 없습니다.")
    for common in constraints:
        # 등록된 업무 제약은 **빠뜨렸는지**가 중요하다 — 등록해 둔 규칙이 워크플로에 들어오지
        # 않으면 그 규칙은 없는 것과 같다. 다만 앞부분 문자열로 맞춰 보면 안 된다: 모델은
        # 문장을 풀어 쓴다(실측: 같은 규칙을 그대로 실었는데도 "읽히지 않았다"가 떴다).
        # 낱말이 얼마나 겹치는지로 본다.
        if not any(_overlaps(common, text) for text in seen):
            notes.append(f"업무 제약사항이 읽히지 않았습니다: {common[:60]}")
    return notes


# 제약 문장에서 뜻을 나르는 낱말만 — 조사·기호는 떼고 두 글자 이상만 센다.
_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")


def _overlaps(common: str, candidate: str, ratio: float = 0.4) -> bool:
    """같은 규칙을 말하고 있는가 — 낱말 겹침으로 본다(문장이 바뀌어도 붙는다)."""
    words = set(_WORD_RE.findall(common))
    if not words:
        return False
    hit = words & set(_WORD_RE.findall(candidate))
    return len(hit) / len(words) >= ratio


def _rows(extracted: dict, key: str) -> list[dict]:
    rows = extracted.get(key)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _ask(provider, messages: list[dict], db: Session) -> tuple[str, dict, dict]:
    text = llm.chat_completion(provider, messages, db)
    data = _parse(text)
    extracted = data.get("extracted")
    extracted = extracted if isinstance(extracted, dict) else {}
    spec = data.get("spec")
    if not isinstance(spec, dict):
        # 스펙이 없으면 거부할 것도 없다 — 모델이 "못 했다"고 말한 경우가 이쪽이다.
        return str(data.get("summary") or text)[:4000], {"nodes": [], "edges": []}, extracted
    return str(data.get("summary") or "")[:4000], spec, extracted


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _parse(text: str) -> dict:
    """모델 응답에서 JSON 객체를 꺼낸다 — 코드펜스와 앞뒤 설명을 견딘다.

    프롬프트로 금지해도 섞여 나온다. 거기서 바로 실패시키면 사람은 "LLM이 틀렸다"만 보고
    무엇을 할 수 없다 — 꺼낼 수 있으면 꺼낸다.
    """
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"summary": text[:4000]}
