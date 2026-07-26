"""외부 API 디렉터리 검색 — 키워드로 공개 API를 찾아 external_api 모듈로 추가한다.

기본 소스는 apis.guru의 머신리더블 OpenAPI 목록(list.json). 목록 전체를 한 번
받아 메모리에 캐시(TTL)하고, 이후 키워드 필터는 로컬에서 수행한다 — 검색마다
외부 호출을 하지 않는다. 폐쇄망에서는 PAAS_API_DIRECTORY_URL을 사내 미러로 바꾼다.

주의: 이 조회는 아웃바운드 호출이다(소스코드가 아니라 API 메타데이터). 그래서
관리자 전용으로 게이트하고, get_with_retry로 서킷브레이커를 적용한다.
"""
import re
import threading
import time

from ..config import get_settings
from .httpx_retry import get_with_retry

_CACHE_TTL = 3600.0  # 목록은 자주 바뀌지 않으므로 1시간 캐시
_lock = threading.Lock()
_cache: dict | None = None
_cached_at = 0.0


class ApiSearchError(RuntimeError):
    """디렉터리 조회 실패 — 502로 매핑."""


def _load_directory() -> dict:
    global _cache, _cached_at
    with _lock:
        if _cache is not None and time.monotonic() - _cached_at < _CACHE_TTL:
            return _cache
    url = get_settings().api_directory_url
    try:
        res = get_with_retry(url, timeout=15)
    except Exception as e:  # noqa: BLE001 — 네트워크/서킷 오류를 도메인 오류로 변환
        raise ApiSearchError(f"API 디렉터리 조회 실패: {e}") from e
    if res.status_code >= 400:
        raise ApiSearchError(f"API 디렉터리 조회 실패 (HTTP {res.status_code})")
    data = res.json()
    if not isinstance(data, dict):
        raise ApiSearchError("API 디렉터리 형식이 올바르지 않습니다")
    with _lock:
        _cache = data
        _cached_at = time.monotonic()
    return data


def clear_cache() -> None:
    """테스트·수동 갱신용."""
    global _cache
    with _lock:
        _cache = None


def _entry_to_result(api_id: str, entry: dict) -> dict | None:
    versions = entry.get("versions") or {}
    preferred = entry.get("preferred")
    ver = versions.get(preferred) or (next(iter(versions.values())) if versions else None)
    if not ver:
        return None
    info = ver.get("info") or {}
    homepage = ""
    contact = info.get("contact") or {}
    if isinstance(contact, dict) and contact.get("url"):
        homepage = contact["url"]
    elif (ver.get("externalDocs") or {}).get("url"):
        homepage = ver["externalDocs"]["url"]
    return {
        "id": api_id,
        "title": info.get("title") or api_id,
        "description": (info.get("description") or "").strip()[:300],
        "provider": info.get("x-providerName") or api_id.split(":")[0],
        "categories": info.get("x-apisguru-categories") or [],
        "homepage": homepage,
        "spec_url": ver.get("swaggerUrl") or ver.get("swaggerYamlUrl") or "",
    }


def search_apis(keyword: str, limit: int = 30) -> list[dict]:
    """키워드가 id·제목·설명·카테고리에 포함된 API를 반환한다(대소문자 무시)."""
    kw = keyword.strip().lower()
    if not kw:
        return []
    directory = _load_directory()
    results: list[dict] = []
    for api_id, entry in directory.items():
        r = _entry_to_result(api_id, entry)
        if r is None:
            continue
        haystack = " ".join([
            r["id"], r["title"], r["description"], " ".join(r["categories"]),
        ]).lower()
        if kw in haystack:
            results.append(r)
        if len(results) >= limit:
            break
    return results


def normalize_module_name(raw: str) -> str:
    """모듈명 규약(^[a-z0-9][a-z0-9-]{1,40}$)에 맞게 정규화한다.

    apis.guru id는 'googleapis.com:calendar'처럼 규약을 위반하는 문자가 많다 —
    소문자화 후 영숫자 외 문자를 '-'로 바꾸고 중복·양끝을 정리, 40자로 자른다.
    """
    s = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if not s or not s[0].isalnum():
        s = "api-" + s.lstrip("-")
    return s[:40].rstrip("-") or "api"
