"""외부 MCP(Model Context Protocol) 서버 디렉터리 검색 및 카탈로그 서비스."""

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


def search_mcp_servers(query: str = "") -> list[dict]:
    """공개 MCP 레지스트리/디렉터리 키워드 검색."""
    query_lower = query.lower().strip()
    if not query_lower:
        return BUILTIN_MCP_DIRECTORY
    return [
        item for item in BUILTIN_MCP_DIRECTORY
        if query_lower in item["name"].lower()
        or query_lower in item["description"].lower()
        or query_lower in item["category"].lower()
        or query_lower in item["vendor"].lower()
    ]
