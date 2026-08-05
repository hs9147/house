"""외주 빌드 결과 검증 — LLM·모듈 사용이 가용 모듈 제약을 지켰는지 정적으로 확인한다.

기획 단계가 만든 '가용 모듈 제약'(services/planning.build_constraints)은 문서·프롬프트로만
전달되므로, 외부에서 실제로 커밋된 코드가 규칙을 지켰는지는 아무도 보지 않는다. 여기서
리포 워킹카피를 훑어 위반을 찾고, 그대로 외주 빌더에게 넘길 수정 지시 프롬프트를 만든다.

LLM을 쓰지 않는다 — 판정 근거가 파일·줄로 남아야 재현되고 다툼이 없다. 대신 오탐이 적은
세 가지만 본다: 외부 LLM 직접 호출 · 하드코딩된 자격증명 · 가용 목록 밖 모듈 호출.
"""
import re
from pathlib import Path

from .workspace import CONTEXT_EXTENSIONS, MAX_CONTEXT_FILE_BYTES, file_tree

MAX_SCAN_FILES = 400

# 게이트웨이를 우회하는 외부 LLM 직접 호출. 호스트와 공식 SDK import를 함께 본다.
_LLM_HOST_RE = re.compile(
    r"(api\.openai\.com|api\.anthropic\.com|generativelanguage\.googleapis\.com"
    r"|bedrock[\w.-]*\.amazonaws\.com|openai\.azure\.com|api\.cohere\.ai|api\.mistral\.ai)",
    re.I,
)
_LLM_SDK_RE = re.compile(
    r"^\s*(?:from|import)\s+(openai|anthropic|google\.generativeai|cohere|mistralai)\b",
    re.M,
)
# 코드에 박힌 자격증명. 접두사가 뚜렷한 것만 본다(임의 문자열을 키로 오인하지 않도록).
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16})")
# 게이트웨이 경유 호출에서 대상 모듈/에이전트 이름을 뽑는다.
_GATEWAY_TARGET_RE = re.compile(r"/proxy/modules/([A-Za-z0-9_-]+)|/a2a/agents/([A-Za-z0-9_-]+)")

_RULE_TITLES = {
    "llm_direct": "외부 LLM 직접 호출 — 중앙 게이트웨이(/paas/api/v1/proxy/llm) 경유로 바꿔야 한다",
    "hardcoded_secret": "코드에 박힌 자격증명 — 플랫폼이 주입하는 환경변수로 바꿔야 한다",
    "unknown_module": "가용 모듈 제약 목록에 없는 모듈/에이전트 호출",
}


def scan(workdir: Path, constraints: dict) -> list[dict]:
    """워킹카피를 훑어 위반 목록을 반환한다. [{rule, file, line, snippet, detail}]"""
    if not workdir.exists():
        return []
    allowed = _allowed_names(constraints)
    findings: list[dict] = []
    root = workdir.resolve()
    for rel in file_tree(workdir, limit=MAX_SCAN_FILES):
        path = (root / rel).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        if path.suffix.lower() not in CONTEXT_EXTENSIONS:
            continue
        if path.stat().st_size > MAX_CONTEXT_FILE_BYTES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        findings.extend(_scan_text(rel, text, allowed))
    return findings


def _allowed_names(constraints: dict) -> set[str]:
    names = {str(r.get("name", "")) for r in constraints.get("available_resources") or []}
    names |= {str(a.get("name", "")) for a in constraints.get("bound_agents") or []}
    return {n for n in names if n}


def _redact(text: str) -> str:
    """자격증명 값은 결과에 싣지 않는다 — 위반을 보고하다 비밀을 유출하면 안 된다."""
    return _SECRET_RE.sub(lambda m: m.group(0)[:6] + "…", text)


def _scan_text(rel: str, text: str, allowed: set[str]) -> list[dict]:
    findings: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule, match in _line_violations(line, allowed):
            findings.append({
                "rule": rule,
                "file": rel,
                "line": lineno,
                "snippet": _redact(line.strip()[:200]),
                "detail": match,
            })
    return findings


def _line_violations(line: str, allowed: set[str]):
    host = _LLM_HOST_RE.search(line)
    if host:
        yield "llm_direct", host.group(0)
    sdk = _LLM_SDK_RE.match(line)
    if sdk:
        yield "llm_direct", sdk.group(1)
    secret = _SECRET_RE.search(line)
    if secret:
        yield "hardcoded_secret", _redact(secret.group(0))
    for m in _GATEWAY_TARGET_RE.finditer(line):
        name = m.group(1) or m.group(2)
        if name not in allowed:
            yield "unknown_module", name


def summarize(findings: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["rule"]] = counts.get(f["rule"], 0) + 1
    return counts


def builder_prompt(findings: list[dict], constraints_doc: str) -> str:
    """외주 빌더(외부 개발도구)에게 그대로 전달할 수정 지시 프롬프트.

    위반이 없으면 빈 문자열 — 보낼 것이 없다는 뜻이다.
    """
    if not findings:
        return ""
    lines = [
        "다음은 이 리포에 대한 플랫폼 컴플라이언스 검사 결과다. 아래 위반을 모두 수정하라.",
        "",
        "## 위반 목록",
    ]
    for rule in _RULE_TITLES:
        items = [f for f in findings if f["rule"] == rule]
        if not items:
            continue
        lines.append("")
        lines.append(f"### {_RULE_TITLES[rule]}")
        for f in items:
            lines.append(f"- `{f['file']}:{f['line']}` — {f['detail']}")
            lines.append(f"  ```\n  {f['snippet']}\n  ```")
    lines += [
        "",
        "## 수정 규칙",
        "- LLM 호출은 반드시 중앙 게이트웨이 `/paas/api/v1/proxy/llm`(또는 주입된 "
        "`PAAS_LLM_PROXY_URL`)를 경유한다. 외부 LLM SDK·엔드포인트를 직접 쓰지 않는다.",
        "- 모듈 호출은 `/paas/api/v1/proxy/modules/{module_name}` 또는 "
        "`/paas/api/v1/a2a/agents/{agent_name}/task`를 경유하고, 아래 가용 목록에 있는 이름만 쓴다.",
        "- 자격증명·엔드포인트는 코드에 쓰지 말고 플랫폼이 주입하는 환경변수를 읽는다.",
        "- 수정 후 같은 검사를 다시 통과시켜라(MCP `check_compliance`).",
        "",
        "## 가용 모듈 제약 (원문)",
        "",
        constraints_doc,
    ]
    return "\n".join(lines)
