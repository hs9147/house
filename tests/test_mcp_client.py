"""외부 MCP 서버 최소 클라이언트 — JSON-RPC 2.0/HTTP, tools/list·tools/call, 도구 스키마 변환."""
from app.services import mcp_client


def test_list_tools_returns_tool_list(monkeypatch):
    monkeypatch.setattr(
        mcp_client, "_post_rpc",
        lambda url, headers, payload: {"result": {"tools": [
            {"name": "search", "description": "웹 검색", "inputSchema": {"type": "object"}},
        ]}},
    )
    tools = mcp_client.list_tools("https://mcp.example.com", "key-1")
    assert tools == [{"name": "search", "description": "웹 검색", "inputSchema": {"type": "object"}}]


def test_call_tool_joins_text_content(monkeypatch):
    monkeypatch.setattr(
        mcp_client, "_post_rpc",
        lambda url, headers, payload: {"result": {"content": [
            {"type": "text", "text": "line1"}, {"type": "text", "text": "line2"},
        ]}},
    )
    result = mcp_client.call_tool("https://mcp.example.com", None, "search", {"q": "x"})
    assert result == "line1\nline2"


def test_rpc_includes_bearer_header_only_when_api_key_given(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        mcp_client, "_post_rpc",
        lambda url, headers, payload: (captured.update(headers=headers, payload=payload), {"result": {}})[1],
    )
    mcp_client.list_tools("https://mcp.example.com", "secret-key")
    assert captured["headers"]["authorization"] == "Bearer secret-key"
    assert captured["payload"]["method"] == "tools/list"
    assert captured["payload"]["jsonrpc"] == "2.0"

    mcp_client.list_tools("https://mcp.example.com", None)
    assert "authorization" not in captured["headers"]


def test_rpc_error_raises_mcp_error(monkeypatch):
    monkeypatch.setattr(
        mcp_client, "_post_rpc",
        lambda url, headers, payload: {"error": {"code": -32601, "message": "method not found"}},
    )
    try:
        mcp_client.list_tools("https://mcp.example.com")
        raised = False
    except mcp_client.McpError as e:
        raised = True
        assert "method not found" in str(e)
    assert raised


def test_build_openai_tools_aggregates_and_namespaces_across_servers(monkeypatch):
    def fake_post_rpc(url, headers, payload):
        if url == "https://a.example.com":
            return {"result": {"tools": [{"name": "search", "description": "a search"}]}}
        return {"result": {"tools": [{"name": "search", "description": "b search"}]}}

    monkeypatch.setattr(mcp_client, "_post_rpc", fake_post_rpc)
    servers = [
        {"name": "srv-a", "url": "https://a.example.com", "api_key": None},
        {"name": "srv-b", "url": "https://b.example.com", "api_key": None},
    ]
    tools, registry = mcp_client.build_openai_tools(servers)

    names = {t["function"]["name"] for t in tools}
    assert names == {"srv-a__search", "srv-b__search"}
    assert all(t["type"] == "function" for t in tools)
    assert registry["srv-a__search"] == (servers[0], "search")
    assert registry["srv-b__search"] == (servers[1], "search")


def test_build_openai_tools_skips_unreachable_server(monkeypatch):
    def fake_post_rpc(url, headers, payload):
        if url == "https://down.example.com":
            raise RuntimeError("connection refused")
        return {"result": {"tools": [{"name": "ok"}]}}

    monkeypatch.setattr(mcp_client, "_post_rpc", fake_post_rpc)
    servers = [
        {"name": "down", "url": "https://down.example.com", "api_key": None},
        {"name": "up", "url": "https://up.example.com", "api_key": None},
    ]
    tools, registry = mcp_client.build_openai_tools(servers)
    assert [t["function"]["name"] for t in tools] == ["up__ok"]


def test_make_tool_executor_dispatches_to_correct_server(monkeypatch):
    calls = []

    def fake_post_rpc(url, headers, payload):
        calls.append((url, payload["params"]))
        return {"result": {"content": [{"type": "text", "text": f"ran on {url}"}]}}

    monkeypatch.setattr(mcp_client, "_post_rpc", fake_post_rpc)
    servers = [{"name": "srv-a", "url": "https://a.example.com", "api_key": "k"}]
    registry = {"srv-a__search": (servers[0], "search")}
    executor = mcp_client.make_tool_executor(registry)

    result = executor("srv-a__search", {"q": "hello"})
    assert result == "ran on https://a.example.com"
    assert calls[0] == ("https://a.example.com", {"name": "search", "arguments": {"q": "hello"}})


def test_make_tool_executor_unknown_tool_returns_message():
    executor = mcp_client.make_tool_executor({})
    assert "unknown tool" in executor("nope__x", {})


def test_make_tool_executor_tool_failure_returns_message_not_raise(monkeypatch):
    def boom(url, headers, payload):
        raise RuntimeError("timeout")

    monkeypatch.setattr(mcp_client, "_post_rpc", boom)
    servers = [{"name": "srv-a", "url": "https://a.example.com", "api_key": None}]
    executor = mcp_client.make_tool_executor({"srv-a__search": (servers[0], "search")})
    result = executor("srv-a__search", {})
    assert "tool call failed" in result
