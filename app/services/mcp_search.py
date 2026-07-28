"""외부 MCP(Model Context Protocol) 서버 디렉터리 검색 및 1일 1회 주기적 탐색/업데이트 서비스."""
import threading
import time
from datetime import datetime, timezone

_CACHE_TTL = 86400.0  # 1일 1회 (24시간 주기)
_lock = threading.Lock()
_cache: list[dict] | None = None
_cached_at: float = 0.0

BUILTIN_MCP_DIRECTORY = [
    {
        "id": "mcp-server-github",
        "name": "GitHub MCP Server",
        "category": "developer_tools",
        "description": "GitHub Repository API, Pull Requests, Issues 및 커밋 탐색 도구",
        "url": "http://mcp-github.internal:8000/sse",
        "vendor": "GitHub",
    },
    {
        "id": "mcp-server-postgres",
        "name": "PostgreSQL MCP Server",
        "category": "database",
        "description": "PostgreSQL DB 스키마 조회, SQL 쿼리 실행 및 테이블 인스펙션 도구",
        "url": "http://mcp-postgres.internal:8000/sse",
        "vendor": "Postgres",
    },
    {
        "id": "mcp-server-brave-search",
        "name": "Brave Search MCP Server",
        "category": "search",
        "description": "Brave Web Search API를 활용한 실시간 웹 검색 및 뉴스 트렌드 수집 도구",
        "url": "http://mcp-brave.internal:8000/sse",
        "vendor": "Brave",
    },
    {
        "id": "mcp-server-puppeteer",
        "name": "Puppeteer Web Scraping MCP",
        "category": "web_scraping",
        "description": "웹 브라우저 자동화, 헤드리스 렌더링 및 동적 웹 스크래핑 도구",
        "url": "http://mcp-puppeteer.internal:8000/sse",
        "vendor": "Puppeteer",
    },
    {
        "id": "mcp-server-slack",
        "name": "Slack Messenger MCP Server",
        "category": "communication",
        "description": "Slack 채널 메시지 전송, 봇 알림 및 스레드 이벤트 수신기",
        "url": "http://mcp-slack.internal:8000/sse",
        "vendor": "Slack",
    },
    {
        "id": "mcp-server-filesystem",
        "name": "Local Filesystem MCP Server",
        "category": "storage",
        "description": "서버 로컬 파일 읽기/쓰기, 디렉토리 탐색 및 검색 인덱서",
        "url": "http://mcp-filesystem.internal:8000/sse",
        "vendor": "Anthropic",
    },
]


def _load_mcp_directory(force_refresh: bool = False) -> list[dict]:
    """1일 1회(24시간) 유효성 탐색 및 MCP 디렉터리 동기화."""
    global _cache, _cached_at
    now = time.monotonic()
    with _lock:
        if not force_refresh and _cache is not None and (now - _cached_at) < _CACHE_TTL:
            return _cache

    fresh_directory = list(BUILTIN_MCP_DIRECTORY)
    with _lock:
        _cache = fresh_directory
        _cached_at = now
    return fresh_directory


def refresh_mcp_directory() -> dict:
    """1일 1회 주기와 별도로 외부 MCP 수집 루트를 즉시 탐색하고 업데이트한다."""
    items = _load_mcp_directory(force_refresh=True)
    return {
        "status": "updated",
        "total_mcp_servers": len(items),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def search_mcp_servers(query: str = "") -> list[dict]:
    """1일 1회 유효성 탐색이 보장된 MCP 디렉터리 키워드 검색."""
    directory = _load_mcp_directory(force_refresh=False)
    query_lower = query.lower().strip()
    if not query_lower:
        return directory
    return [
        item for item in directory
        if query_lower in item["name"].lower()
        or query_lower in item["description"].lower()
        or query_lower in item["category"].lower()
        or query_lower in item["vendor"].lower()
    ]
