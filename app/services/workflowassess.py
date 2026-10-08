"""워크플로 평가 — 사람이 하는 일을 **에이전트로 옮길 수 있는가**, 옮기려면 무엇을 바꿔야
하는가.

구성 대화(workflowchat)와 다른 질문이다. 거기서는 "이 업무를 흐름으로 적어 달라"였고,
여기서는 **이미 적힌 흐름을 보고** 어느 단계가 사람 손을 떠날 수 있는지 본다. 실측에서 이
구분이 바로 필요해졌다: GP구매 업무 메모로 만든 워크플로는 29단계 중 24개가 사람 단계였다 —
업무를 충실히 옮긴 결과지만, 그 상태로는 자동화된 것이 하나도 없다.

판정은 세 가지로만 둔다.
  agent   — 지금 있는 자원으로 바로 옮길 수 있다.
  partial — 자료 수집·초안은 옮기고 **결정은 사람이** 남는다(가장 흔한 답이다).
  human   — 사람이 해야 한다(결재 권한·책임·대외 교섭).

**셈은 LLM에게 맡기지 않는다.** 판정은 모델이 하고, 비율과 개수는 우리가 센다 — 모델이
"24개 중 8개"라고 적어 놓고 목록에는 6개만 있는 경우를 걸러야 한다. 스펙에 없는 단계를
가리키는 항목도 버리고 그 사실을 적는다(읽어 낸 것을 검토하는 것과 같은 규칙이다).
"""
import json
import re

from sqlalchemy.orm import Session

from ..models import Workflow
from . import llm, planning, workflow as workflow_service

VERDICTS = ("agent", "partial", "human")

PROMPT = """당신은 사내 PaaS의 워크플로를 보고 **자동화 가능성**을 평가한다. 출력은 JSON 객체
하나다.

{{"summary": "두세 문장(한국어). 이 흐름이 지금 어디까지 자동화 가능한지.",
  "steps": [{{
    "id": "평가 대상 단계 id(스펙에 있는 것만)",
    "verdict": "agent | partial | human",
    "why": "그렇게 본 이유 한두 문장",
    "becomes": ["바꿔 넣을 노드 종류 목록(쓸 수 있는 종류 중에서만)"],
    "change": "워크플로를 어떻게 바꾸면 되는지 — 단계를 쪼개는지, 무엇을 앞에 두는지",
    "needs": ["그러려면 있어야 하는 것(저장소·모듈·데이터·권한). 지금 다 있으면 빈 배열"]
  }}],
  "missing": ["이 흐름을 더 자동화하려면 플랫폼에 추가로 있어야 하는 것"],
  "risks": ["자동화하면 생기는 위험(오판의 대가, 책임 소재). 없으면 빈 배열"]}}

규칙:
- 코드펜스(```)나 JSON 밖의 문장을 쓰지 않는다.
- **사람 단계(human)를 모두** 평가한다. 하나도 빼지 않는다.
- becomes에는 아래 '쓸 수 있는 노드 종류'에 있는 것만 쓴다. 없는 도구를 지어내지 않는다.
- 결재·승인처럼 **권한과 책임**이 걸린 단계는 human으로 둔다. 자료 수집과 초안 작성을
  앞 단계로 떼어 내 사람이 **확인만** 하게 만드는 쪽이 현실적인 답이다 — 그 경우 partial이고,
  change에 "무엇을 떼어 내는지"를 적는다.
- 아래 제약사항을 어기는 자동화는 제안하지 않는다.

쓸 수 있는 노드 종류:
{node_types}

이 조직이 쓸 수 있는 자원:
{resources}

반드시 지켜야 하는 공통 제약사항:
{constraints}

평가할 워크플로({name}):
{spec}
"""


def assess(db: Session, workflow: Workflow) -> dict:
    """평가 한 번. 저장하지 않는다 — 스펙이 바뀌면 평가도 옛것이 된다.

    돌려주는 것: summary·steps·metrics·missing·risks·notes·provider. notes는 **우리가**
    붙이는 검토 메모다(모델이 빠뜨린 사람 단계, 스펙에 없는 단계를 가리킨 항목).
    """
    spec = workflow.spec or {}
    nodes = [n for n in spec.get("nodes", []) if isinstance(n, dict)]
    if not nodes:
        raise workflow_service.WorkflowError("단계가 없습니다 — 먼저 워크플로를 구성하세요.")
    provider = llm.default_provider(db)
    if provider is None:
        raise workflow_service.WorkflowError(
            "기본 LLM 프로바이더가 없습니다 — LLM 관리에서 「설정」을 누르세요.")

    resources = workflow_service.resources(db, workflow.organization_id)
    facts = PROMPT.format(
        node_types="\n".join(
            f"- {t['type']}({t['label']}): {t['help']}" for t in resources["node_types"]),
        resources=json.dumps({
            "stores": resources["stores"], "modules": resources["modules"],
            "providers": resources["providers"],
        }, ensure_ascii=False, indent=2),
        constraints="\n".join(f"- {c}" for c in planning.common_constraints(db))
        or "- (등록된 제약사항 없음)",
        name=workflow.name,
        spec=workflow_service.spec_as_text(spec),
    )
    data = _parse(llm.chat_completion(provider, [{"role": "user", "content": facts}], db))

    node_types = {str(n["id"]): str(n.get("type")) for n in nodes if n.get("id")}
    human_ids = {nid for nid, kind in node_types.items() if kind == "human"}
    allowed = set(workflow_service.NODE_TYPES)

    steps: list[dict] = []
    notes: list[str] = []
    seen: set[str] = set()
    for row in data.get("steps", []) if isinstance(data.get("steps"), list) else []:
        if not isinstance(row, dict):
            continue
        node_id = str(row.get("id") or "")
        if node_id not in node_types:
            notes.append(f"스펙에 없는 단계를 평가했습니다: {node_id or '(이름 없음)'}")
            continue
        if node_id in seen:
            continue
        seen.add(node_id)
        verdict = str(row.get("verdict") or "")
        if verdict not in VERDICTS:
            verdict = "partial"
            notes.append(f"{node_id}: 판정이 분명하지 않아 '부분'으로 둡니다.")
        becomes = [str(b) for b in (row.get("becomes") or []) if str(b) in allowed]
        dropped = [str(b) for b in (row.get("becomes") or []) if str(b) not in allowed]
        if dropped:
            notes.append(f"{node_id}: 쓸 수 없는 노드 종류를 제안했습니다 — {', '.join(dropped)}")
        needs = [str(n) for n in (row.get("needs") or []) if str(n).strip()]
        steps.append({
            "id": node_id,
            "type": node_types[node_id],
            "verdict": verdict,
            "why": str(row.get("why") or "")[:600],
            "becomes": becomes,
            "change": str(row.get("change") or "")[:800],
            "needs": needs,
            # 지금 바로 할 수 있는가 — 더 있어야 할 것이 없으면 그렇다.
            "ready_now": verdict != "human" and not needs,
        })

    missed = sorted(human_ids - seen)
    if missed:
        # 사람 단계를 빼먹으면 "평가했다"가 거짓이 된다 — 숨기지 않는다.
        notes.append(f"평가되지 않은 사람 단계: {', '.join(missed)}")

    # 셈은 우리가 한다(모델의 숫자를 믿지 않는다). 판정 'human'의 개수는 human_only로
    # 내보낸다 — 'human'은 **사람 단계의 수**이고, 둘을 같은 키에 담으면 하나가 덮인다.
    counts = {v: sum(1 for s in steps if s["verdict"] == v) for v in VERDICTS}
    metrics = {
        "total": len(nodes),
        "human": len(human_ids),
        "assessed": len(steps),
        "agent": counts["agent"],
        "partial": counts["partial"],
        "human_only": counts["human"],
        "ready_now": sum(1 for s in steps if s["ready_now"]),
        # 사람 단계 중 손을 떠날 수 있다고 본 비율 — 이 화면의 한 줄 요약이다.
        "shift_rate": round(
            (counts["agent"] + counts["partial"]) / len(human_ids) * 100, 1)
        if human_ids else 0.0,
    }
    return {
        "provider": provider.name,
        "summary": str(data.get("summary") or "")[:2000],
        "steps": steps,
        "metrics": metrics,
        "missing": [str(m) for m in (data.get("missing") or []) if str(m).strip()][:20],
        "risks": [str(r) for r in (data.get("risks") or []) if str(r).strip()][:20],
        "notes": notes,
        "facts": facts,
    }


def change_request(assessment: dict) -> str:
    """평가를 **다음 구성 요청**으로 바꾼다 — 읽고 끝나는 평가는 아무것도 바꾸지 않는다.

    지금 바로 할 수 있는 것만 싣는다(needs가 있는 항목은 자원이 생긴 뒤의 일이다).
    """
    lines = [
        "아래 평가를 반영해 워크플로를 고쳐 주세요. 사람이 확인·결재하는 자리는 그대로 두고,"
        " 자료 수집과 초안 작성만 앞 단계로 떼어 냅니다.",
    ]
    for step in assessment.get("steps", []):
        if not step.get("ready_now"):
            continue
        becomes = ", ".join(step.get("becomes") or []) or "자동 단계"
        lines.append(f"- {step['id']}: {step.get('change') or becomes}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _parse(text: str) -> dict:
    cleaned = _FENCE_RE.sub("", text.strip())
    for candidate in (cleaned, cleaned[cleaned.find("{"):cleaned.rfind("}") + 1]):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {"summary": text[:2000]}
