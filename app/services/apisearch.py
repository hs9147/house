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

_CACHE_TTL = 86400.0  # 1일 1회(24시간) 주기적 동기화
_lock = threading.Lock()
_cache: dict | None = None
_cached_at = 0.0
_scheduler_thread: threading.Thread | None = None


class ApiSearchError(RuntimeError):
    """디렉터리 조회 실패 — 502로 매핑."""


def _load_directory(force_refresh: bool = False) -> dict:
    global _cache, _cached_at
    with _lock:
        if not force_refresh and _cache is not None and time.monotonic() - _cached_at < _CACHE_TTL:
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


def refresh_api_directory() -> dict:
    """외부 API 수집 루트를 즉시 강제 탐색 및 업데이트한다."""
    return _load_directory(force_refresh=True)


def clear_cache() -> None:
    """테스트·수동 갱신용."""
    global _cache
    with _lock:
        _cache = None


def start_daily_api_directory_scheduler() -> None:
    """1일 1회(24시간 주기) 백그라운드에서 외부 API 수집 루트를 탐색하고 갱신한다."""
    global _scheduler_thread
    with _lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return

        def _loop():
            # 최초 실행 10초 후 1차 워밍업 수행
            time.sleep(10)
            while True:
                try:
                    refresh_api_directory()
                except Exception:
                    pass
                # 24시간 (86,400초) 주기 대기
                time.sleep(_CACHE_TTL)

        _scheduler_thread = threading.Thread(target=_loop, daemon=True, name="daily-api-scheduler")
        _scheduler_thread.start()


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


# 카테고리가 비어 있는 항목을 고르는 값. 디렉터리에 실제로 그런 항목이 많아서
# (x-apisguru-categories가 없는 스펙) "전체" 아니면 못 고르는 상태였다.
UNCATEGORIZED = "기타"


def list_categories() -> list[dict]:
    """디렉터리에 실제로 있는 카테고리와 그 개수. 목록 끝에 "기타"(카테고리 없음)를 붙인다.

    고정 표를 두지 않는 이유: 목록은 외부 디렉터리가 정하고 갱신될 때마다 바뀐다 —
    화면에만 적어 두면 실제로는 고를 수 없는 값이 남는다.
    """
    counts: dict[str, int] = {}
    uncategorized = 0
    for api_id, entry in _load_directory().items():
        result = _entry_to_result(api_id, entry)
        if result is None:
            continue
        if not result["categories"]:
            uncategorized += 1
            continue
        for name in result["categories"]:
            counts[name] = counts.get(name, 0) + 1
    items = [{"name": name, "count": counts[name]} for name in sorted(counts)]
    if uncategorized:
        items.append({"name": UNCATEGORIZED, "count": uncategorized})
    return items


def search_apis(keyword: str, category: str = "", limit: int = 30) -> list[dict]:
    """키워드·카테고리로 API를 찾는다. 두 조건은 AND, 각각 비우면 그 조건은 안 건다.

    category가 UNCATEGORIZED("기타")면 카테고리가 없는 항목만 고른다 — 그 항목들은
    카테고리 이름으로는 영영 걸리지 않아서 따로 고를 값이 필요하다.
    둘 다 비면 빈 목록이다(디렉터리 전체를 쏟아내지 않는다).
    """
    kw = keyword.strip().lower()
    cat = category.strip()
    if not kw and not cat:
        return []
    directory = _load_directory()
    results: list[dict] = []
    for api_id, entry in directory.items():
        r = _entry_to_result(api_id, entry)
        if r is None or not _matches_category(r, cat):
            continue
        haystack = " ".join([
            r["id"], r["title"], r["description"], " ".join(r["categories"]),
        ]).lower()
        if kw and kw not in haystack:
            continue
        results.append(r)
        if len(results) >= limit:
            break
    return results


def _matches_category(result: dict, category: str) -> bool:
    if not category:
        return True
    if category == UNCATEGORIZED:
        return not result["categories"]
    return any(c.lower() == category.lower() for c in result["categories"])


def normalize_module_name(raw: str) -> str:
    """모듈명 규약(^[a-z0-9][a-z0-9-]{1,40}$)에 맞게 정규화한다.

    apis.guru id는 'googleapis.com:calendar'처럼 규약을 위반하는 문자가 많다 —
    소문자화 후 영숫자 외 문자를 '-'로 바꾸고 중복·양끝을 정리, 40자로 자른다.
    """
    s = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if not s or not s[0].isalnum():
        s = "api-" + s.lstrip("-")
    return s[:40].rstrip("-") or "api"
