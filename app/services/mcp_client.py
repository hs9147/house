"""MCP(Model Context Protocol) 서버 최소 클라이언트 — JSON-RPC 2.0 / HTTP.

대상은 사내 서버(api/mcp_servers.py)든 외부 서버든 같다 — 주소와 키만 다르다.

MCP 서버가 단일 JSON 응답(스트리밍 없는 streamable-http)으로 답하는 경우만
지원한다 — tools/list · tools/call 두 메서드만 다루고, resources/prompts, 세션
재개(Mcp-Session-Id 유지), SSE 스트리밍 응답은 이번 범위 밖이다.

플랫폼 채팅(services/llm.py)이 이 모듈로 도구 목록을 모아 OpenAI 호환 tools=
스키마를 만들고, 모델이 도구를 호출하면 다시 이 모듈로 실제 MCP 서버를 호출한다.
"""
import itertools
import threading
import time

import httpx

_id_counter = itertools.count(1)

# tools/list 캐시 TTL(초). 도구 스키마는 서버를 다시 띄울 때나 바뀌므로 짧게 잡아도
# 충분하고, 이 정도면 한 대화 안에서 오가는 여러 턴은 캐시로 덮인다.
_TOOLS_TTL = 60.0
_tools_lock = threading.Lock()
# (url, api_key) -> (조회시각, 도구목록 | None). None은 "조회 실패"를 캐시한 것.
_tools_cache: dict[tuple[str, str], tuple[float, list[dict] | None]] = {}


class McpError(RuntimeError):
    pass


def _post_rpc(url: str, headers: dict, payload: dict) -> dict:
    """테스트에서 monkeypatch하는 실제 HTTP 경계."""
    res = httpx.post(url, headers=headers, json=payload, timeout=30)
    res.raise_for_status()
    return res.json()


def _rpc(url: str, api_key: str | None, method: str, params: dict) -> dict:
    headers = {"content-type": "application/json", "accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    payload = {"jsonrpc": "2.0", "id": next(_id_counter), "method": method, "params": params}
    data = _post_rpc(url, headers, payload)
    if "error" in data:
        raise McpError(str(data["error"].get("message", "MCP 서버 오류")))
    return data.get("result") or {}


def list_tools(url: str, api_key: str | None = None) -> list[dict]:
    """서버가 제공하는 도구 목록 — 각 항목은 {name, description, inputSchema}."""
    return _rpc(url, api_key, "tools/list", {}).get("tools", [])


def cached_list_tools(url: str, api_key: str | None = None) -> list[dict] | None:
    """tools/list를 TTL 캐시로 감싼 것 — None은 조회 실패.

    솔루션 구성 단계는 LLM을 부르기 전에 바인딩된 서버 전부의 도구 목록을 모은다
    (services/planning.solution_tools). 캐시가 없으면 매 턴 서버 수만큼 동기 HTTP
    왕복이 돌고, 죽은 서버가 하나 있으면 그 타임아웃(30초)을 턴마다 다시 기다린다 —
    그래서 실패도 같이 캐시한다.

    실제 응답 여부를 봐야 하는 연결 확인(check_server)은 캐시를 쓰지 않는다.
    """
    key = (url, api_key or "")
    now = time.monotonic()
    with _tools_lock:
        hit = _tools_cache.get(key)
        if hit is not None and (now - hit[0]) < _TOOLS_TTL:
            return hit[1]
    try:
        tools: list[dict] | None = list_tools(url, api_key)
    except Exception:  # noqa: BLE001 — 서버 하나의 장애가 채팅 전체를 막으면 안 됨
        tools = None
    with _tools_lock:
        _tools_cache[key] = (now, tools)
    return tools


def clear_tools_cache() -> None:
    """캐시 비우기 — 모듈 주소·키를 고친 직후와 테스트에서 쓴다."""
    with _tools_lock:
        _tools_cache.clear()


def call_tool(url: str, api_key: str | None, name: str, arguments: dict) -> str:
    """도구를 실행하고 텍스트 결과를 반환한다 — content[].type=="text"만 이어붙인다."""
    result = _rpc(url, api_key, "tools/call", {"name": name, "arguments": arguments})
    parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    return "\n".join(parts) if parts else str(result)


def build_openai_tools(servers: list[dict]) -> tuple[list[dict], dict[str, tuple[dict, str]]]:
    """바인딩된 MCP 서버들의 tools/list를 모아 OpenAI 호환 tools= 스키마로 변환한다.

    함수명이 서버 간에 겹칠 수 있어 "{서버명}__{도구명}"으로 접두사를 붙인다.
    반환하는 registry는 실제 호출(make_tool_executor)에서 함수명 → (서버, 원래
    도구명)을 되찾는 역참조 테이블이다. 서버 하나가 응답하지 않아도(tools/list
    실패) 나머지 서버의 도구는 계속 쓸 수 있게 서버별로 따로 조회한다
    (cached_list_tools — 성공·실패 모두 짧게 캐시된다)."""
    tools: list[dict] = []
    registry: dict[str, tuple[dict, str]] = {}
    for server in servers:
        server_tools = cached_list_tools(server["url"], server.get("api_key"))
        if server_tools is None:
            continue
        for t in server_tools:
            fn_name = f"{server['name']}__{t['name']}"
            tools.append({
                "type": "function",
                "function": {
                    "name": fn_name,
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                },
            })
            registry[fn_name] = (server, t["name"])
    return tools, registry


def make_tool_executor(registry: dict[str, tuple[dict, str]]):
    """services.llm.chat_completion에 넘길 tool_executor(name, arguments) -> str 콜백."""
    def _execute(fn_name: str, arguments: dict) -> str:
        entry = registry.get(fn_name)
        if entry is None:
            return f"unknown tool: {fn_name}"
        server, tool_name = entry
        try:
            return call_tool(server["url"], server.get("api_key"), tool_name, arguments)
        except Exception as e:  # noqa: BLE001 — 도구 실패도 대화가 이어지도록 텍스트로 반환
            return f"tool call failed: {e}"
    return _execute


def check_server(url: str, api_key: str | None = None) -> dict:
    """MCP 서버가 실제로 응답하는지 확인한다 — tools/list를 한 번 찔러 본다.

    등록만으로는 동작을 알 수 없어서 있는 함수다. 주소가 틀렸거나(이름 조회 실패),
    전송 방식이 안 맞거나(이 클라이언트는 단일 JSON 응답만 다룬다 — /sse 엔드포인트는
    여기서 통신이 안 된다), 서버가 stdio 전용이면 등록은 성공한 채 조용히 죽어 있다.

    예외를 던지지 않는다 — 화면에서 여러 모듈을 한 번에 확인하므로, 하나가 실패해도
    나머지 결과를 그대로 보여줘야 한다.
    """
    if not url:
        return {"ok": False, "error": "url이 비어 있습니다.", "tool_count": 0, "tools": []}
    hint = ""
    if url.rstrip("/").endswith("/sse"):
        # 이 클라이언트는 SSE 스트리밍을 다루지 않는다(모듈 docstring 참고).
        hint = (
            " 주소가 /sse로 끝납니다 — 이 클라이언트는 단일 JSON 응답(streamable-http)만"
            " 다루므로 SSE 엔드포인트와는 통신할 수 없습니다."
        )
    try:
        tools = list_tools(url, api_key)
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)[:200]}{hint}",
            "tool_count": 0,
            "tools": [],
        }
    return {
        "ok": True,
        "error": None,
        "tool_count": len(tools),
        "tools": [t.get("name", "") for t in tools][:20],
    }
