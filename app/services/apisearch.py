"""외부 API 카탈로그 — 소스에서 받아 DB에 쌓고, 검색은 그 표만 읽는다.

수집 소스는 둘이고 한 표(api_catalog)에 source로 갈라 담는다:

  apis.guru      PAAS_API_DIRECTORY_URL (기본값 있음) — 글로벌 OpenAPI 목록
  공공데이터      PAAS_PUBLIC_DATA_URL (기본 비어 있음 = 안 부른다) — 국내 공공 카탈로그

**받는 일과 찾는 일을 갈랐다.** 예전에는 검색이 목록 전체를 메모리에 캐시하고 그 위에서
걸렀다. 재시작하면 사라지고, 워커가 여럿이면 각자 따로 받고, 무엇보다 검색 경로에
아웃바운드 호출이 섞여 있어서 관리자 전용으로 묶을 수밖에 없었다. 이제 수집
(sync_catalog)만 밖으로 나가고 검색(search_apis)은 DB만 읽는다 — 그래서 검색을 MCP
도구로 열 수 있다(api/mcp_servers.py의 /mcp/apis).

**갱신은 바뀐 것만 쓴다.** 소스는 매번 목록 전체를 주지만 그중 달라지는 것은 몇 개뿐이다.
행마다 필드를 비교해 달라진 것만 대입하므로 안 바뀐 행에는 UPDATE 자체가 나가지 않고,
updated_at도 그대로다 — "언제 바뀌었나"가 "언제 받았나"에 덮이지 않는다.

**사라진 항목은 지우지 않고 removed_at을 찍는다.** 한 소스가 실패하면 그 소스의 행은
아예 손대지 않는다 — 조회 실패를 "없어졌다"로 기록하면 다음 검색에서 멀쩡한 카탈로그가
통째로 사라진다.

주의: 수집은 아웃바운드 호출이다. 그래서 수집을 부르는 창구(POST /modules/search/refresh)는
관리자 전용이고, get_with_retry로 서킷브레이커를 적용한다.
"""
import re
import threading
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import ApiCatalogEntry, utcnow
from .httpx_retry import get_with_retry

SOURCE_APISGURU = "apisguru"
SOURCE_PUBLIC_DATA = "publicdata"

# 소스가 주는 값 그대로인 필드. 갱신할 때 이 목록만 비교한다 — search_text는 여기서
# 파생되고, created_at·updated_at·removed_at은 표가 스스로 관리한다.
_FIELDS = ("title", "description", "provider", "categories", "homepage", "spec_url")

_SYNC_INTERVAL = 86400.0  # 1일 1회
_scheduler_lock = threading.Lock()
_scheduler_thread: threading.Thread | None = None

EMPTY_CATALOG = (
    "API 카탈로그가 비어 있습니다 — 아직 수집하지 않았습니다"
    "(관리자: POST /modules/search/refresh)."
)


class ApiSearchError(RuntimeError):
    """카탈로그 수집 실패 — 502로 매핑."""


def _as_categories(value) -> list[str]:
    """카테고리를 **언제나** 문자열 리스트로 만든다.

    소스가 이 자리에 리스트가 아니라 문자열 하나를 넣어 주는 일이 있다
    (x-apisguru-categories: "security"). 그대로 두면 리스트처럼 순회되는 곳마다 글자
    단위로 쪼개진다 — 카테고리 목록에 s·e·c·u·r·i·t·y가 따로 서고, 검색용 건초더미도
    "s e c u r i t y"가 되어 낱말로는 아무 것도 안 걸린다.

    읽는 쪽에서도 이 함수를 거친다: 고치기 전에 문자열로 저장된 행이 이미 표에 있고,
    그 행도 다시 수집하기 전까지 화면에서 맞게 보여야 한다.
    """
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


# --- 소스 1: apis.guru ---

def _apisguru_items() -> list[dict]:
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
    out = []
    for api_id, entry in data.items():
        item = _entry_to_result(api_id, entry)
        if item is not None:
            out.append(item)
    return out


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
        "categories": _as_categories(info.get("x-apisguru-categories")),
        "homepage": homepage,
        "spec_url": ver.get("swaggerUrl") or ver.get("swaggerYamlUrl") or "",
    }


# --- 소스 2: 공공데이터 카탈로그 어댑터 ---
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
    settings = get_settings()
    url = settings.public_data_url.strip()
    if not url:
        return []

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
            "categories": _as_categories(category),
            "homepage": _pick(row, _URL_KEYS),
            "spec_url": "",
        })
    return items


def _sources() -> list[tuple[str, object]]:
    """지금 켜져 있는 소스. 공공데이터는 주소를 넣은 설치본에서만 목록에 오른다 —
    끈 소스를 "받았는데 비어 있었다"로 다루면 그 소스의 행이 전부 removed로 찍힌다."""
    out: list[tuple[str, object]] = [(SOURCE_APISGURU, _apisguru_items)]
    if get_settings().public_data_url.strip():
        out.append((SOURCE_PUBLIC_DATA, _public_data_items))
    return out


# --- 수집 ---

def sync_catalog(db: Session) -> dict:
    """소스를 받아 카탈로그를 최신으로 만든다. 바뀐 행만 쓴다.

    소스별로 따로 처리한다 — 한쪽이 죽어도 다른 쪽은 갱신되고, 죽은 쪽의 행은 그대로
    남는다(다음 검색에서 사라지지 않는다). 전부 죽었을 때만 오류다: 그때는 "바뀐 것이
    없다"가 아니라 아무 것도 못 받은 것이다.
    """
    stats = {"added": 0, "updated": 0, "restored": 0, "removed": 0, "unchanged": 0}
    warnings: list[str] = []
    synced: list[str] = []
    for source, load in _sources():
        try:
            items = load()
        except ApiSearchError as e:
            warnings.append(str(e))
            continue
        _merge(db, source, items, stats)
        synced.append(source)
    if not synced:
        raise ApiSearchError(" / ".join(warnings) or "수집할 소스가 없습니다")
    db.commit()
    return {**stats, "sources": synced, "warnings": warnings}


def _merge(db: Session, source: str, items: list[dict], stats: dict) -> None:
    existing = {
        row.ext_id: row
        for row in db.execute(
            select(ApiCatalogEntry).where(ApiCatalogEntry.source == source)
        ).scalars()
    }
    seen: set[str] = set()
    for item in items:
        ext_id = str(item["id"])[:255]
        if ext_id in seen:
            continue  # 같은 응답 안의 중복 — 앞엣것만 남긴다
        seen.add(ext_id)
        row = existing.get(ext_id)
        if row is None:
            db.add(ApiCatalogEntry(
                source=source, ext_id=ext_id, search_text=_haystack(item),
                **{f: item[f] for f in _FIELDS},
            ))
            stats["added"] += 1
            continue

        # **바뀐 필드만 대입한다.** 전부 대입하면 categories(JSON)가 같은 값이어도
        # dirty로 잡혀 UPDATE가 나가고, onupdate가 updated_at을 매번 밀어 올린다.
        changed = [f for f in _FIELDS if getattr(row, f) != item[f]]
        back = row.removed_at is not None
        if not changed and not back:
            stats["unchanged"] += 1
            continue
        for field in changed:
            setattr(row, field, item[field])
        if changed:
            row.search_text = _haystack(item)
        if back:
            row.removed_at = None
        stats["restored" if back else "updated"] += 1

    if not seen:
        # 성공했는데 목록이 비었다 = 카탈로그가 통째로 사라진 것보다 응답이 깨진 쪽이
        # 훨씬 그럴듯하다. 지우는 판단은 하지 않는다(다음 수집이 정상이면 그때 정리된다).
        return
    for ext_id, row in existing.items():
        if ext_id in seen or row.removed_at is not None:
            continue
        row.removed_at = utcnow()
        stats["removed"] += 1


def _haystack(item: dict) -> str:
    """검색이 훑을 소문자 건초더미 — 예전에 검색마다 메모리에서 만들던 그 문자열이다."""
    return " ".join([
        str(item["id"]), item["title"], item["description"], " ".join(item["categories"]),
    ]).lower()


def start_daily_api_directory_scheduler() -> None:
    """1일 1회 백그라운드로 카탈로그를 수집한다.

    기동 직후가 아니라 10초 뒤에 시작한다 — 첫 요청이 몰리는 구간에 수천 건 upsert를
    끼워 넣지 않기 위해서다. 실패는 삼킨다: 카탈로그가 낡는 것은 서비스가 죽는 것보다
    훨씬 가벼운 문제이고, 실패한 소스의 행은 손대지 않으므로 기존 카탈로그는 남는다.
    """
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return

        def _loop():
            time.sleep(10)
            while True:
                try:
                    _sync_in_background()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(_SYNC_INTERVAL)

        _scheduler_thread = threading.Thread(target=_loop, daemon=True, name="daily-api-scheduler")
        _scheduler_thread.start()


def _sync_in_background() -> dict:
    """스케줄러 전용 — 요청 세션이 없으므로 자기 세션을 연다."""
    from ..db import SessionLocal  # noqa: PLC0415 — 기동 순서 의존을 만들지 않는다

    db = SessionLocal()
    try:
        return sync_catalog(db)
    finally:
        db.close()


# --- 검색(DB만 읽는다) ---

# 카테고리가 비어 있는 항목을 고르는 값. 카탈로그에 실제로 그런 항목이 많아서
# (x-apisguru-categories가 없는 스펙) "전체" 아니면 못 고르는 상태였다.
UNCATEGORIZED = "기타"


def _to_result(row: ApiCatalogEntry) -> dict:
    return {
        "id": row.ext_id,
        "title": row.title,
        "description": row.description,
        "provider": row.provider,
        "categories": _as_categories(row.categories),
        "homepage": row.homepage,
        "spec_url": row.spec_url,
        "source": row.source,
    }


def _live(query):
    return query.where(ApiCatalogEntry.removed_at.is_(None))


def search_apis(db: Session, keyword: str, category: str = "", limit: int = 30) -> dict:
    """키워드·카테고리로 카탈로그를 찾는다. 두 조건은 AND, 각각 비우면 그 조건은 안 건다.

    category가 UNCATEGORIZED("기타")면 카테고리가 없는 항목만 고른다 — 그 항목들은
    카테고리 이름으로는 영영 걸리지 않아서 따로 고를 값이 필요하다.
    둘 다 비면 빈 목록이다(카탈로그 전체를 쏟아내지 않는다).
    """
    kw = keyword.strip().lower()
    cat = category.strip()
    if not kw and not cat:
        return {"results": [], "warnings": []}

    query = _live(select(ApiCatalogEntry))
    if kw:
        # 카테고리는 리스트라 SQL에서 걸 수 없다 — 키워드로 먼저 좁히고 아래에서 본다.
        query = query.where(ApiCatalogEntry.search_text.contains(kw))
    results = []
    for row in db.execute(query.order_by(ApiCatalogEntry.title, ApiCatalogEntry.id)).scalars():
        if not _matches_category(_as_categories(row.categories), cat):
            continue
        results.append(_to_result(row))
        if len(results) >= limit:
            break

    # 결과가 없는 것과 아직 아무 것도 안 받은 것은 다른 문제다 — 구분해서 알려 준다.
    warnings = [] if results or catalog_size(db) else [EMPTY_CATALOG]
    return {"results": results, "warnings": warnings}


def _matches_category(categories: list, category: str) -> bool:
    if not category:
        return True
    if category == UNCATEGORIZED:
        return not categories
    return any(str(c).lower() == category.lower() for c in categories)


def list_categories(db: Session) -> list[dict]:
    """카탈로그에 실제로 있는 카테고리와 그 개수. 끝에 "기타"(카테고리 없음)를 붙인다.

    고정 표를 두지 않는 이유: 목록은 소스가 정하고 수집할 때마다 바뀐다 — 화면에만 적어
    두면 실제로는 고를 수 없는 값이 남는다.
    """
    counts: dict[str, int] = {}
    uncategorized = 0
    for raw in db.execute(_live(select(ApiCatalogEntry.categories))).scalars():
        names = _as_categories(raw)
        if not names:
            uncategorized += 1
            continue
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    items = [{"name": name, "count": counts[name]} for name in sorted(counts)]
    if uncategorized:
        items.append({"name": UNCATEGORIZED, "count": uncategorized})
    return items


def catalog_size(db: Session) -> int:
    return db.execute(_live(select(func.count(ApiCatalogEntry.id)))).scalar_one()


def catalog_status(db: Session) -> dict:
    """수집 현황 — 소스별 건수와 마지막으로 바뀐 시각.

    "검색 결과가 없다"가 질의 탓인지 수집이 안 된 탓인지 여기서 갈린다.
    """
    per_source: dict[str, dict] = {}
    rows = db.execute(select(
        ApiCatalogEntry.source, ApiCatalogEntry.removed_at, ApiCatalogEntry.updated_at,
    )).all()
    for source, removed_at, updated_at in rows:
        stat = per_source.setdefault(source, {"total": 0, "removed": 0, "updated_at": None})
        if removed_at is None:
            stat["total"] += 1
        else:
            stat["removed"] += 1
        if updated_at and (stat["updated_at"] is None or updated_at > stat["updated_at"]):
            stat["updated_at"] = updated_at
    return {
        "total": sum(s["total"] for s in per_source.values()),
        "sources": per_source,
    }


def normalize_module_name(raw: str) -> str:
    """모듈명 규약(^[a-z0-9][a-z0-9-]{1,40}$)에 맞게 정규화한다.

    apis.guru id는 'googleapis.com:calendar'처럼 규약을 위반하는 문자가 많다 —
    소문자화 후 영숫자 외 문자를 '-'로 바꾸고 중복·양끝을 정리, 40자로 자른다.
    """
    s = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if not s or not s[0].isalnum():
        s = "api-" + s.lstrip("-")
    return s[:40].rstrip("-") or "api"
