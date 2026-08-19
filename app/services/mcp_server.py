"""MCP 서버 공통 처리 — JSON-RPC 2.0 initialize · tools/list · tools/call 디스패치.

플랫폼은 MCP 서버를 여러 개 노출한다(외주 빌더용은 api/planning.py, 사내 도구용은
api/mcp_servers.py). 프로토콜 껍데기는 전부 같아서 여기 한 번만 둔다 — 서버마다
따로 쓰면 어느 서버는 unknown method에 200 error를, 어느 서버는 500을 내는 식으로
갈라진다.

전송은 streamable-http 단일 JSON 응답이다 — 이 플랫폼의 클라이언트
(services/mcp_client.py)가 그것만 다루므로 서버도 같은 모양으로 맞춘다. SSE 스트리밍,
resources/prompts, 세션 재개(Mcp-Session-Id)는 양쪽 모두 범위 밖이다.

도구 목록은 그냥 dict 리스트다({name, description, inputSchema}) — MCP가 요구하는
모양 그대로라 별도 타입을 두지 않는다.
"""
from typing import Callable

PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = "0.1.0"

# JSON-RPC 2.0 표준 코드 중 여기서 쓰는 둘
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class McpToolError(RuntimeError):
    """도구가 인자·상태 문제로 실행할 수 없을 때 — JSON-RPC error로 나간다.

    HTTP 500이 아니라 200 + error다. MCP 클라이언트는 도구 실패를 대화에 되돌려
    모델이 다시 시도하게 만드는 쪽이 맞다.
    """


def text_result(body: str) -> dict:
    """tools/call 결과 — 이 플랫폼의 도구는 전부 텍스트 하나만 돌려준다."""
    return {"content": [{"type": "text", "text": body}]}


def ok(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def error(req_id, message: str, code: int = INVALID_PARAMS) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def read_payload(request) -> dict:
    """요청 본문 파싱 — 깨진 본문에도 JSON-RPC 오류로 답해야 하므로 예외를 삼킨다."""
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}


def dispatch(
    payload: dict,
    *,
    server_name: str,
    tools: list[dict],
    call: Callable[[str, dict], str],
) -> dict:
    """JSON-RPC 요청 하나를 처리한다. call(도구명, 인자) -> 텍스트."""
    method = payload.get("method")
    req_id = payload.get("id")
    params = payload.get("params") or {}

    if method == "initialize":
        return ok(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": server_name, "version": SERVER_VERSION},
        })
    if method == "tools/list":
        return ok(req_id, {"tools": tools})
    if method == "tools/call":
        name = params.get("name")
        if name not in {t["name"] for t in tools}:
            return error(req_id, f"unknown tool: {name}", METHOD_NOT_FOUND)
        try:
            body = call(name, params.get("arguments") or {})
        except McpToolError as e:
            return error(req_id, str(e))
        return ok(req_id, text_result(body))
    return error(req_id, f"unknown method: {method}", METHOD_NOT_FOUND)
