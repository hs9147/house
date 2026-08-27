"""외부 API 디렉터리 검색 — 키워드로 공개 API를 찾아 external_api 모듈로 추가한다.

소스는 둘이고, 결과는 합쳐서 내보낸다:

  apis.guru      PAAS_API_DIRECTORY_URL (기본값 있음) — 글로벌 OpenAPI 목록
  공공데이터      PAAS_PUBLIC_DATA_URL (기본 비어 있음 = 안 부른다) — 국내 공공 카탈로그

두 번째를 붙인 이유: apis.guru는 글로벌 OpenAPI 카탈로그라 국내 공공데이터가 잡히지
않는다. 기본값을 비워 두는 이유: 설정하지 않은 설치본에 아웃바운드 호출을 새로 만들지
않기 위해서다.

**한 소스가 죽어도 다른 소스는 나온다.** 둘을 한 번에 실패시키면 "검색이 안 된다"만
남고 어느 쪽이 문제인지 알 수 없다 — 살아 있는 결과를 주고 죽은 쪽은 warnings로 말한다.

목록 전체를 한 번 받아 메모리에 캐시(TTL)하고, 이후 키워드 필터는 로컬에서 수행한다 —
검색마다 외부 호출을 하지 않는다. 폐쇄망에서는 두 주소 모두 사내 미러로 바꾼다.

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
# 공공데이터 카탈로그는 형식이 apis.guru와 달라 정규화한 뒤 캐시한다.
_public_cache: list[dict] | None = None
_public_cached_at = 0.0
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
    global _cache, _public_cache
    with _lock:
        _cache = None
        _public_cache = None


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


# --- 공공데이터 카탈로그 어댑터 ---
#
# **응답 형식을 확정할 수 없는 자리다.** 카탈로그마다 감싸는 모양이 다르고(목록을 그대로
# 주기도, data/items/response.body.items 아래 넣기도 한다) 필드 이름도 제각각이다. 그래서
# 후보를 훑어 찾고, 하나도 못 찾으면 **조용히 빈 목록을 주는 대신 무엇을 받았는지 말하며
# 실패한다** — 형식이 어긋난 것과 "그런 데이터가 없다"가 구분되지 않으면 설정을 고칠
# 방법이 없다.
_LIST_KEYS = ("data", "items", "results", "list", "records")
_TITLE_KEYS = ("title", "name", "apiNm", "listTitle", "서비스명", "api_nm")
_DESC_KEYS = ("description", "desc", "apiDesc", "listDesc", "설명", "api_desc")
_URL_KEYS = ("url", "link", "endpoint", "apiUrl", "linkUrl", "detailUrl")
_ID_KEYS = ("id", "apiId", "listId", "publicDataPk", "serviceId")
_CATEGORY_KEYS = ("category", "categoryNm", "classification", "분류")


def _pick(entry: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _rows_of(payload) -> list[dict]:
    """응답에서 목록을 꺼낸다. 감싸는 층이 몇 겹이든 첫 dict 리스트를 찾는다."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in _LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    # response.body.items 처럼 한 겹 더 들어가 있는 모양
    for value in payload.values():
        if isinstance(value, (dict, list)):
            found = _rows_of(value)
            if found:
                return found
    return []


def _public_data_items() -> list[dict]:
    """공공데이터 카탈로그 → 공통 항목 모양. 주소가 없으면 빈 목록(소스를 끈 것)."""
    global _public_cache, _public_cached_at
    settings = get_settings()
    url = settings.public_data_url.strip()
    if not url:
        return []
    with _lock:
        if _public_cache is not None and time.monotonic() - _public_cached_at < _CACHE_TTL:
            return _public_cache

    params = {"serviceKey": settings.public_data_key} if settings.public_data_key else None
    try:
        res = get_with_retry(url, timeout=15, params=params)
    except Exception as e:  # noqa: BLE001 — 네트워크/서킷 오류를 도메인 오류로 변환
        raise ApiSearchError(f"공공데이터 카탈로그 조회 실패: {e}") from e
    if res.status_code >= 400:
        raise ApiSearchError(f"공공데이터 카탈로그 조회 실패 (HTTP {res.status_code})")
    try:
        payload = res.json()
    except Exception as e:  # noqa: BLE001 — JSON이 아닌 응답(HTML 오류 페이지 등)
        raise ApiSearchError(f"공공데이터 카탈로그가 JSON이 아닙니다: {e}") from e

    rows = _rows_of(payload)
    if not rows:
        shape = ", ".join(sorted(payload)[:8]) if isinstance(payload, dict) else type(payload).__name__
        raise ApiSearchError(
            f"공공데이터 카탈로그에서 목록을 찾지 못했습니다(받은 최상위: {shape})"
            " — PAAS_PUBLIC_DATA_URL이 카탈로그 목록을 주는 주소인지 확인하세요.")

    items = []
    for row in rows:
        title = _pick(row, _TITLE_KEYS)
        if not title:
            continue  # 이름조차 없으면 고를 수도, 등록할 수도 없다
        category = _pick(row, _CATEGORY_KEYS)
        items.append({
            "id": _pick(row, _ID_KEYS) or normalize_module_name(title),
            "title": title,
            "description": _pick(row, _DESC_KEYS)[:300],
            "provider": "공공데이터",
            "categories": [category] if category else [],
            "homepage": _pick(row, _URL_KEYS),
            "spec_url": "",
        })
    with _lock:
        _public_cache = items
        _public_cached_at = time.monotonic()
    return items


def _all_items() -> tuple[list[dict], list[str]]:
    """두 소스를 합친 항목과, 실패한 소스의 사유. 한쪽이 죽어도 다른 쪽은 나온다."""
    items: list[dict] = []
    warnings: list[str] = []
    for load in (_apisguru_items, _public_data_items):
        try:
            items += load()
        except ApiSearchError as e:
            warnings.append(str(e))
    if not items and warnings:
        # 둘 다 죽었으면 결과가 없는 것이 아니라 조회가 안 된 것이다.
        raise ApiSearchError(" / ".join(warnings))
    return items, warnings


def _apisguru_items() -> list[dict]:
    out = []
    for api_id, entry in _load_directory().items():
        result = _entry_to_result(api_id, entry)
        if result is not None:
            out.append(result)
    return out


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
    items, _warnings = _all_items()
    for result in items:
        if not result["categories"]:
            uncategorized += 1
            continue
        for name in result["categories"]:
            counts[name] = counts.get(name, 0) + 1
    items = [{"name": name, "count": counts[name]} for name in sorted(counts)]
    if uncategorized:
        items.append({"name": UNCATEGORIZED, "count": uncategorized})
    return items


def search_apis(keyword: str, category: str = "", limit: int = 30) -> dict:
    """키워드·카테고리로 API를 찾는다. 두 조건은 AND, 각각 비우면 그 조건은 안 건다.

    category가 UNCATEGORIZED("기타")면 카테고리가 없는 항목만 고른다 — 그 항목들은
    카테고리 이름으로는 영영 걸리지 않아서 따로 고를 값이 필요하다.
    둘 다 비면 빈 목록이다(디렉터리 전체를 쏟아내지 않는다).
    """
    kw = keyword.strip().lower()
    cat = category.strip()
    if not kw and not cat:
        return {"results": [], "warnings": []}
    items, warnings = _all_items()
    results: list[dict] = []
    for r in items:
        if not _matches_category(r, cat):
            continue
        haystack = " ".join([
            r["id"], r["title"], r["description"], " ".join(r["categories"]),
        ]).lower()
        if kw and kw not in haystack:
            continue
        results.append(r)
        if len(results) >= limit:
            break
    # 살아 있는 결과와 함께 죽은 소스를 말한다 — 결과가 적은 것이 "그런 API가 없다"인지
    # "한쪽 목록을 못 받았다"인지 화면에서 구분되어야 한다.
    return {"results": results, "warnings": warnings}


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
