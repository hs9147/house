"""외부 MCP(Model Context Protocol) 서버 디렉터리 검색 및 1일 1회 주기적 탐색/업데이트 서비스."""
import threading
import time
from datetime import datetime, timezone

_CACHE_TTL = 86400.0  # 1일 1회 (24시간 주기)
_lock = threading.Lock()
_cache: list[dict] | None = None
_cached_at: float = 0.0

# 비어 있는 것이 맞다.
#
# 여기에는 mcp-postgres.internal:8000 같은 주소가 7건 하드코딩돼 있었는데, 그 이름은
# 어디에도 실재하지 않았다 — 가져와 등록한 모듈은 등록 직후부터 죽어 있었다. 게다가
# 주소가 /sse로 끝나 있어, 실재했더라도 이 플랫폼의 클라이언트(services/mcp_client.py는
# 단일 JSON 응답만 다룬다)로는 통신할 수 없었다. 널리 쓰이는 postgres·brave-search·
# filesystem MCP 서버는 애초에 stdio 전용이라 URL이라는 개념이 없다.
#
# 실재하지 않는 주소를 목록으로 내주면 사용자는 "등록했는데 왜 안 되나"를 추적하게
# 된다. 사내에서 실제로 띄운 MCP 서버가 있으면 그 주소를 여기 추가하거나, 모듈 등록
# 화면에서 직접 입력한다. 등록 후에는 연결 확인(mcp_client.check_server)으로 실제
# 응답 여부를 볼 수 있다.
BUILTIN_MCP_DIRECTORY: list[dict] = []


def _load_mcp_directory(force_refresh: bool = False) -> list[dict]:
    """MCP 디렉터리 목록. 지금은 BUILTIN_MCP_DIRECTORY가 원천이다.

    캐시 구조만 남겨 둔다 — 사내 레지스트리를 붙일 자리다. 연결 여부는 여기서 보지
    않는다(그건 mcp_client.check_server가 한다).
    """
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
    """디렉터리 캐시를 비우고 다시 읽는다.

    외부를 조회하지 않는다 — BUILTIN_MCP_DIRECTORY가 원천이므로 캐시 무효화가 전부다.
    ("외부 수집 루트를 탐색한다"고 적혀 있었지만 실제로는 내장 목록을 복사할 뿐이었다.)
    """
    items = _load_mcp_directory(force_refresh=True)
    return {
        "status": "updated",
        "total_mcp_servers": len(items),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def search_mcp_servers(query: str = "") -> list[dict]:
    """MCP 디렉터리 키워드 검색.

    여기 있는 항목이 실제로 응답한다는 보장은 없다 — 확인은 등록 후 연결 확인으로 한다.
    ("유효성 탐색이 보장된"이라고 적혀 있었지만 아무것도 확인하지 않았다.)
    """
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
