"""LLM 프로바이더 추상화 — 외부/내부 모두 OpenAI 호환 chat completions로 호출한다.

내부 프로바이더는 base_url에 "project://<llm 프로젝트명>"을 허용하고,
호출 시점에 해당 프로젝트의 배포 도메인으로 해석한다 (소스가 사내망을 벗어나지 않음).
"""
import json
import re
from typing import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import BuildProfile, LlmProvider, Project
from ..security import decrypt_value

EDIT_SYSTEM_PROMPT = """You are a coding assistant working inside an internal PaaS.
When the user asks for a code change, reply with a short explanation followed by
ONE unified diff enclosed in a ```diff fenced block. The diff must apply cleanly
with `git apply` from the repository root (use a/ and b/ path prefixes).
If no code change is needed, reply without a diff block."""

REVIEW_SYSTEM_PROMPT = """You are a strict code reviewer. Review the given unified diff.
Reply in Korean as a JSON array of findings:
[{"severity": "high|medium|low", "file": "...", "comment": "..."}]
Return [] if the diff looks fine. Reply with JSON only."""


def resolve_base_url(base_url: str, db: Session | None = None) -> str:
    """project://name → 플랫폼에 release 프로필로 배포된 프로젝트의 실제 URL.

    1차(small)는 서브패스 기반 배포이므로(services/proxy/__init__.py의
    path_prefix_for), 대상 프로젝트의 조직에 맞는 경로를 써야 실제 배포와 일치한다.
    db가 없으면(세션을 못 넘기는 호출부) 조직을 알 수 없어 "_" 자리로 안전하게
    떨어진다 — 조직 소속 llm 프로젝트라면 가능한 경우 db를 넘길 것."""
    if not base_url.startswith("project://"):
        return base_url.rstrip("/")
    name = base_url.removeprefix("project://").strip("/")
    settings = get_settings()
    if settings.tier == "enterprise":
        return f"http://{name}.{settings.base_domain}"
    from .proxy import path_prefix_for  # noqa: PLC0415 — 순환 import 회피

    org_name = None
    if db is not None:
        target = db.execute(select(Project).where(Project.name == name)).scalar_one_or_none()
        if target is not None and target.organization is not None:
            org_name = target.organization.name
    path = path_prefix_for(org_name, name, None, BuildProfile.release)
    return f"http://{settings.base_domain}{path}"


MAX_TOOL_ROUNDS = 6


def chat_completion(
    provider: LlmProvider,
    messages: list[dict],
    db: Session | None = None,
    tools: list[dict] | None = None,
    tool_executor: Callable[[str, dict], str] | None = None,
    _round: int = 0,
) -> str:
    """tools/tool_executor를 주면(예: 프로젝트에 바인딩된 MCP 서버) OpenAI 호환
    tool-call 프로토콜로 모델↔도구를 오간다 — 모델이 더 이상 tool_calls를 요청하지
    않을 때까지(최대 MAX_TOOL_ROUNDS회) 반복하고 최종 텍스트만 반환한다. tools가
    없으면 기존과 동일하게 단발 completion."""
    url = resolve_base_url(provider.base_url, db)
    headers = {"content-type": "application/json"}
    if provider.api_key_encrypted:
        headers["authorization"] = f"Bearer {decrypt_value(provider.api_key_encrypted)}"
    payload = {"model": provider.model, "messages": messages}
    if tools:
        payload["tools"] = tools
    data = _post_chat(url, headers, payload)
    message = data["choices"][0]["message"]

    tool_calls = message.get("tool_calls")
    if tool_calls and tool_executor and _round < MAX_TOOL_ROUNDS:
        next_messages = [*messages, message]
        for tc in tool_calls:
            fn = tc.get("function", {})
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            result = tool_executor(fn.get("name", ""), arguments)
            next_messages.append({
                "role": "tool", "tool_call_id": tc.get("id", ""), "content": result,
            })
        return chat_completion(provider, next_messages, db, tools, tool_executor, _round + 1)
    return message["content"]


def _post_chat(url: str, headers: dict, payload: dict) -> dict:
    """테스트에서 monkeypatch하는 실제 HTTP 경계."""
    res = httpx.post(url, headers=headers, json=payload, timeout=120)
    res.raise_for_status()
    return res.json()


_DIFF_FENCE = re.compile(r"```(?:diff|patch)\n(.*?)```", re.DOTALL)


def extract_diff(text: str) -> str | None:
    """응답에서 unified diff를 추출한다. 펜스 우선, 없으면 원문에서 diff 헤더 탐색."""
    m = _DIFF_FENCE.search(text)
    if m:
        diff = m.group(1)
        return diff if diff.strip() else None
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith(("diff --git ", "--- a/", "--- /dev/null")):
            return "".join(lines[i:])
    return None


def review_diff(provider: LlmProvider, diff: str, db: Session | None = None) -> list[dict]:
    reply = chat_completion(
        provider,
        [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": f"```diff\n{diff}\n```"},
        ],
        db,
    )
    try:
        # 모델이 펜스로 감싸는 경우까지 허용
        cleaned = re.sub(r"^```(?:json)?|```$", "", reply.strip(), flags=re.MULTILINE).strip()
        findings = json.loads(cleaned)
        if isinstance(findings, list):
            return findings
    except (json.JSONDecodeError, ValueError):
        pass
    return [{"severity": "info", "file": "", "comment": reply.strip()[:2000]}]


def max_severity(findings: list[dict]) -> str:
    order = {"high": 3, "medium": 2, "low": 1}
    top = 0
    for f in findings:
        top = max(top, order.get(str(f.get("severity", "")).lower(), 0))
    return {3: "high", 2: "medium", 1: "low", 0: "none"}[top]
