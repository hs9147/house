"""외부 MCP(Model Context Protocol) 서버 최소 클라이언트 — JSON-RPC 2.0 / HTTP.

MCP 서버가 단일 JSON 응답(스트리밍 없는 streamable-http)으로 답하는 경우만
지원한다 — tools/list · tools/call 두 메서드만 다루고, resources/prompts, 세션
재개(Mcp-Session-Id 유지), SSE 스트리밍 응답은 이번 범위 밖이다.

플랫폼 채팅(services/llm.py)이 이 모듈로 도구 목록을 모아 OpenAI 호환 tools=
스키마를 만들고, 모델이 도구를 호출하면 다시 이 모듈로 실제 MCP 서버를 호출한다.
"""
import itertools

import httpx

_id_counter = itertools.count(1)


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
    실패) 나머지 서버의 도구는 계속 쓸 수 있도록 개별적으로 감싼다."""
    tools: list[dict] = []
    registry: dict[str, tuple[dict, str]] = {}
    for server in servers:
        try:
            server_tools = list_tools(server["url"], server.get("api_key"))
        except Exception:  # noqa: BLE001 — 서버 하나의 장애가 채팅 전체를 막으면 안 됨
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
