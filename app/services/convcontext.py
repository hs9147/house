"""스마트워크 대화 문맥 — 다음 턴의 모델에게 **무엇을 다시 보여 줄지** 정한다.

예전에는 최근 메시지 20개를 그대로 다시 보냈다. 그러면 세 가지가 깨진다.

  - **산출물을 모른다.** assistant로 남는 것은 짧은 답 글뿐이고 보고서·파일은 화면(view)에만 있어,
    "아까 그 표에 부서 추가"의 '그 표'를 다음 턴의 모델은 본 적이 없다. 그래서 대화에 산출물의
    번호·제목·판·열 이름을 남기고([결과: r12 …]), 본문은 필요할 때 session__read로 다시 연다
    (Anthropic 문맥 엔지니어링의 just-in-time 읽기).
  - **요구가 흩어진다.** 지시가 여러 턴에 나뉘어 오면 모델 성능이 크게 떨어진다(LLMs Get Lost in
    Multi-Turn Conversation, 2505.06120 — 평균 39%). 그래서 사람이 원한 것을 한곳에 모은 **업무
    원장**(목적·요구·정한 것·아직 모르는 것)을 매 턴 앞에 갱신해 이번 요청 바로 앞에 둔다 — 입력의
    끝이 중간보다 잘 읽힌다(Lost in the Middle). 원장은 JSON 슬롯이고(FnCTOD, Magentic-One의 task
    ledger), 통째로 다시 쓰지 않고 고칠 것만 고친다(ACE — 짧게 다시 쓰면 세부를 잃는다).
  - **지시어를 못 푼다.** 같은 호출에서 이번 요청을 대화를 모르는 사람도 알 문장으로 풀어 쓴다
    (CHIQ의 2단계 — 기록의 모호성부터 정리하고 일한다). 사내 문서 검색은 낱말이 다 들어 있어야 걸리므로
    (docsearch) '그거'가 섞인 검색어는 그대로 0건이다.

창은 메시지 수가 아니라 **글자 예산**이다. 예산 밖의 앞 대화는 원장이 들고 간다. 지난 턴의 첨부 원문은
다시 싣지 않는다(앞부분과 다시 읽을 번호만) — 예전에는 첨부가 메시지 상한 밖에서 덧붙어 매 턴 다시 실렸다.
원장은 그 턴의 assistant 메시지(view.brief)에 남는다 — 메시지 이력이 곧 원장의 이력이다.
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Callable

from . import mcp_server

HISTORY_CHARS = 60_000      # 다시 보내는 대화의 글자 예산(이번 요청 포함)
MAX_MESSAGE_CHARS = 20_000  # 메시지 하나(smartwork.MAX_MESSAGE_CHARS와 같다)
OLD_ATTACHMENT_CHARS = 600  # 지난 턴 첨부는 앞부분만 — 원문은 session__read로
DIALOG_CHARS = 12_000       # 원장을 고칠 때 읽는 대화(지난 원장 이후)
READ_CHARS = 40_000         # session__read 한 번에 돌려주는 글자
MAX_ITEMS, ITEM_CHARS = 12, 200

BRIEF_MARK = "[업무 원장"
REQUEST_MARK = "\n\n[이번 요청]\n"


# ---------------------------------------------------------------- 산출물
def _columns_of_html(text: str) -> list[str]:
    """보고서의 첫 표 머리글 — "그 표에 열 추가"를 할 때 무엇이 있었는지."""
    table = re.search(r"<table[\s\S]*?</table>", text or "", re.I)
    if not table:
        return []
    heads = re.findall(r"<th[^>]*>([\s\S]*?)</th>", table.group(0), re.I)
    return [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", h))).strip()[:40] for h in heads][:15]


def _columns_of_file(f: dict) -> list[str]:
    if f.get("sheets"):
        rows = (f["sheets"][0] or {}).get("rows") or []
        return [str(c)[:40] for c in (rows[0] if rows else [])][:15]
    if f.get("format") == "csv":
        first = str(f.get("content") or "").splitlines()[:1]
        return [c.strip().strip('"')[:40] for c in (first[0].split(",") if first else [])][:15]
    return []


def artifacts_of(history: list[dict]) -> list[dict]:
    """이 대화에서 만든 결과(보고서·파일)와 지난 턴의 첨부 — 번호로 가리키고 session__read로 다시 연다.
    같은 제목을 다시 내면 판(version)이 오른다 — "아까 그 보고서"는 마지막 판이다."""
    out, versions = [], {}
    for m in history:
        mid, view = m.get("id"), m.get("view") or {}
        if mid is None:
            continue   # 이번 요청 — 아직 번호가 없고, 그 첨부는 이번 턴에 원문으로 간다
        if m.get("role") == "user":
            for i, a in enumerate(m.get("attachments") or []):
                if a.get("kind") == "document":
                    out.append({"id": f"a{mid}-{i}", "kind": "첨부", "title": a.get("name") or "첨부",
                                "version": 1, "columns": [], "body": a.get("text") or ""})
            continue
        report = view.get("report")
        if report and report.get("content"):
            title = str(report.get("title") or "보고서")
            versions[title] = versions.get(title, 0) + 1
            out.append({"id": f"r{mid}", "kind": "보고서", "title": title, "version": versions[title],
                        "columns": _columns_of_html(report["content"]) if report.get("format") == "html" else [],
                        "body": report["content"]})
        f = view.get("file")
        if f and f.get("source") == "generated":
            title = str(f.get("name") or "파일")
            versions[title] = versions.get(title, 0) + 1
            body = (json.dumps(f["sheets"], ensure_ascii=False) if f.get("sheets") else str(f.get("content") or ""))
            out.append({"id": f"f{mid}", "kind": "파일", "title": title, "version": versions[title],
                        "columns": _columns_of_file(f), "body": body})
    return out


def artifact_line(a: dict) -> str:
    cols = f" · 열: {', '.join(a['columns'])}" if a["columns"] else ""
    ver = f" {a['version']}판" if a["version"] > 1 else ""
    return f"{a['id']} {a['kind']} 「{a['title']}」{ver}{cols}"


# ---------------------------------------------------------------- 대화 창
def _assistant_text(m: dict, by_id: dict[str, dict]) -> str:
    """답 글 + 그 턴에 낸 것(번호로) — 다음 턴의 모델이 무엇을 만들었고 무엇을 물었는지 알게."""
    view, mid, notes = m.get("view") or {}, m.get("id"), []
    for prefix in ("r", "f"):
        a = by_id.get(f"{prefix}{mid}")
        if a:
            notes.append(f"[결과: {artifact_line(a)}]")
    if view.get("agent"):
        notes.append(f"[에이전트: {view['agent'].get('name')}]")
    if view.get("web"):
        notes.append(f"[웹: {view['web'].get('url')}]")
    if view.get("file") and view["file"].get("source") == "personal":
        notes.append(f"[내 문서 열기: {view['file'].get('name')}]")
    if view.get("choices"):
        notes.append("[물은 선택지: " + " | ".join(str(c.get("label")) for c in view["choices"]) + "]")
    if view.get("suggestions"):
        notes.append("[제안: " + " | ".join(str(s.get("title")) for s in view["suggestions"]) + "]")
    steps = [s for s in view.get("steps") or [] if not str(s).startswith("결과 작성")]
    if steps:
        notes.append("[찾아본 것: " + "; ".join(str(s) for s in steps[:8]) + "]")
    text = str(m.get("content") or "")[:MAX_MESSAGE_CHARS]
    return "\n".join([text, *notes]).strip()


def _user_text(m: dict, current: bool, shared: bool) -> str:
    text = str(m.get("content") or "")[:MAX_MESSAGE_CHARS]
    for i, a in enumerate(m.get("attachments") or []):
        if a.get("kind") != "document":
            # 이미지는 그 턴에만 모델에게 갔다 — 뒤 턴에는 이름만 남는다.
            text += f"\n\n[첨부 이미지: {a['name']}]"
        elif current:
            text += f"\n\n[첨부 문서: {a['name']}]\n{a['text']}"
        else:
            body = a.get("text") or ""
            more = (f"… (전체는 session__read a{m.get('id')}-{i})" if len(body) > OLD_ATTACHMENT_CHARS else "")
            text += f"\n\n[첨부 문서: {a['name']}]\n{body[:OLD_ATTACHMENT_CHARS]}{more}"
    if shared and m.get("author"):
        text = f"[{m['author']}] {text}"
    return text


def window(history: list[dict], shared: bool, opening: str, budget: int = 0) -> tuple[list[dict], int]:
    """다시 보낼 대화 — 새것부터 글자 예산까지(이번 요청은 늘 넣는다). (메시지들, 뺀 앞 메시지 수).

    처음부터 다 들어가면 예전처럼: 대화가 제안(assistant)으로 시작하면 여는 지시를 앞에 되살린다(그
    제안이 무엇에 대한 것이었는지). 앞을 잘랐으면 잘린 자리의 assistant는 버린다 — 대화 중간에 여는
    지시가 다시 나타나면 모델이 업무 제안부터 다시 한다. 잘린 앞 대화는 원장이 들고 간다."""
    budget = budget or HISTORY_CHARS
    by_id = {a["id"]: a for a in artifacts_of(history)}
    rendered = []
    last = len(history) - 1
    for i, m in enumerate(history):
        role = m.get("role")
        if role == "user":
            rendered.append({"role": "user", "content": _user_text(m, i == last and m.get("id") is None, shared)})
        elif role == "assistant":
            rendered.append({"role": "assistant", "content": _assistant_text(m, by_id)})
    rendered = [r for r in rendered if r["content"]]
    used, start = 0, len(rendered)
    while start > 0:
        size = len(rendered[start - 1]["content"])
        if start < len(rendered) and used + size > budget:
            break
        used += size
        start -= 1
    out = rendered[start:]
    if start > 0:
        while out and out[0]["role"] != "user":
            out.pop(0)
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": opening})
    return out, start


# ---------------------------------------------------------------- 업무 원장
RESTATE_PROMPT = """사내 업무 대화의 **업무 원장**을 고친다. 원장은 이 업무에서 지금까지 사람이 원한 것을 한곳에 모은
것이다 — 다음 답을 쓰는 도우미는 흩어진 대화보다 이것을 먼저 믿고 일한다. 출력은 JSON 객체 하나다(설명·코드펜스 없이).

{{"goal": "이 업무의 목적 한 문장",
  "requirements": ["사람이 정한 조건·범위·형식 — 하나에 하나, 대상과 값을 이름으로"],
  "decisions": ["대화에서 정해진 것 — 사람이 고른 선택지, 확인한 기준"],
  "open": ["아직 정하지 않았거나 확인하지 못한 것"],
  "request": "이번 요청을 대화를 모르는 사람도 알도록 쓴 완결된 문장",
  "refers": ["이번 요청이 가리키는 산출물 번호 — 아래 목록에 있는 것만"]}}

규칙:
- 지난 원장을 **고친다**: 그대로인 항목은 그대로 두고, 바뀐 것은 고치고, 새것은 더하고, 사람이 취소한 것만
  뺀다. 짧게 다시 쓰다 세부(이름·숫자·기간)를 잃지 마라.
- 사람이 말하지 않은 것을 지어내지 마라. 도우미가 제안만 하고 사람이 받지 않은 것은 decisions가 아니다.
- request는 이번 요청의 '그거·아까·다시·거기에·그 표' 같은 말을 가리키는 것(산출물 번호와 제목, 대상 이름,
  조건)으로 바꿔 쓴다. 새 요구를 더하지 않는다. 무엇을 가리키는지 분명하지 않으면 그 말을 그대로 두고 open에 적는다.
- 목록마다 {items}개까지, 항목 하나는 {chars}자까지.

지난 원장:
{brief}

산출물(이 대화에서 만든 결과와 지난 턴의 첨부):
{artifacts}

지난 원장 이후의 대화:
{dialog}

이번 요청:
{request}
"""


def _json_object(text: str) -> dict:
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    data = json.loads(cleaned[start:end + 1] if start >= 0 and end > start else cleaned)
    if not isinstance(data, dict):
        raise ValueError("원장이 객체가 아니다")
    return data


def _items(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip()[:ITEM_CHARS] for x in raw if str(x).strip()][:MAX_ITEMS]


def last_brief(history: list[dict]) -> tuple[dict | None, int]:
    """마지막 원장과 그것을 남긴 메시지의 자리 — 없으면 (None, -1)."""
    for i in range(len(history) - 1, -1, -1):
        brief = (history[i].get("view") or {}).get("brief")
        if history[i].get("role") == "assistant" and isinstance(brief, dict):
            return brief, i
    return None, -1


def _dialog(messages: list[dict], by_id: dict[str, dict]) -> str:
    lines = []
    for m in messages:
        if m.get("role") == "assistant":
            lines.append("도우미: " + _assistant_text(m, by_id)[:1500])
        elif m.get("role") == "user":
            names = [a["name"] for a in m.get("attachments") or []]
            lines.append("사람: " + str(m.get("content") or "")[:1500] + (f" (첨부: {', '.join(names)})" if names else ""))
    text = "\n".join(lines)
    return text[-DIALOG_CHARS:] if len(text) > DIALOG_CHARS else text


def needs_restate(history: list[dict]) -> bool:
    """이전 요청이 있는 턴만 — 첫 요청은 풀어 쓸 기록이 없다(CHIQ·history-aware retriever도 건너뛴다)."""
    return bool(history) and history[-1].get("role") == "user" and any(
        m.get("role") == "user" for m in history[:-1])


def restate(history: list[dict], ask: Callable[[str], str]) -> dict | None:
    """원장을 고치고 이번 요청을 풀어 쓴다 — ask(프롬프트) → 모델의 글. 실패하면 None(지난 원장으로 간다)."""
    prev, at = last_brief(history)
    arts = artifacts_of(history)
    by_id = {a["id"]: a for a in arts}
    since = history[at:-1] if at >= 0 else history[:-1]
    request = str(history[-1].get("content") or "")
    prompt = RESTATE_PROMPT.format(
        items=MAX_ITEMS, chars=ITEM_CHARS,
        brief=json.dumps(prev, ensure_ascii=False, indent=1) if prev else "(없음 — 처음 만든다)",
        artifacts="\n".join(f"- {artifact_line(a)}" for a in arts) or "(없음)",
        dialog=_dialog(since, by_id) or "(없음)", request=request[:MAX_MESSAGE_CHARS])
    try:
        data = _json_object(ask(prompt))
    except Exception:  # noqa: BLE001 — 원장이 없을 뿐 대화는 이어진다(지난 원장이 그대로 간다)
        return None
    brief = {"goal": str(data.get("goal") or (prev or {}).get("goal") or "").strip()[:ITEM_CHARS],
             **{k: _items(data.get(k)) for k in ("requirements", "decisions", "open")}}
    return {"brief": brief, "request": str(data.get("request") or "").strip()[:2000] or request,
            "refers": [r for r in _items(data.get("refers")) if r in by_id]}


def brief_block(brief: dict | None, artifacts: list[dict], restated: dict | None) -> str:
    """이번 요청 바로 앞에 붙는 원장 — 없으면 빈 글."""
    if not brief and not artifacts:
        return ""
    lines = [f"{BRIEF_MARK} — 지금까지 사람이 원한 것. 앞 대화가 잘렸어도 이것은 다 들어 있다. 대화와 어긋나면"
             " 사람의 가장 최근 말을 따른다]"]
    if brief:
        if brief.get("goal"):
            lines.append(f"목적: {brief['goal']}")
        for key, label in (("requirements", "요구"), ("decisions", "정한 것"), ("open", "아직 모름")):
            if brief.get(key):
                lines.append(f"{label}:")
                lines += [f"- {x}" for x in brief[key]]
    if artifacts:
        lines.append("이 대화의 산출물(본문은 session__read로 다시 읽는다 — 고쳐 달라는 요청이면 먼저 읽고 고친"
                     " 판을 다시 낸다):")
        lines += [f"- {artifact_line(a)}" for a in artifacts[-15:]]
    if restated and restated.get("request"):
        refers = f" (가리키는 것: {', '.join(restated['refers'])})" if restated.get("refers") else ""
        lines.append(f"이번 요청을 풀어 쓴 것: {restated['request']}{refers}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 다시 읽기 도구
_READ_TOOL = [{
    "name": "read",
    "description": (
        "이 대화에서 만든 보고서·파일과 지난 턴의 첨부 문서를 다시 읽는다 — 업무 원장·대화의 번호(r12·f13·"
        "a10-0)로. 앞에서 낸 결과를 고치거나 이어서 쓰라는 요청이면 먼저 읽고, 고친 판을 다시 낸다."
    ),
    "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "description": "산출물 번호"}},
                    "required": ["id"]},
}]


def toolset(artifacts: list[dict]):
    """('session', 도구, 호출) — 산출물이 없으면 None."""
    if not artifacts:
        return None
    by_id = {a["id"]: a for a in artifacts}

    def call(name: str, args: dict) -> str:
        a = by_id.get(str(args.get("id") or "").strip())
        if a is None:
            raise mcp_server.McpToolError(f"그런 번호가 없습니다 — 있는 것: {', '.join(by_id)}")
        body = a["body"]
        cut = f"\n\n… (앞 {READ_CHARS}자만 — 전체 {len(body)}자)" if len(body) > READ_CHARS else ""
        return f"{artifact_line(a)}\n\n{body[:READ_CHARS]}{cut}"

    return ("session", _READ_TOOL, call)
