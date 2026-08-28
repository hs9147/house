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
from urllib.parse import parse_qsl, unquote, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import ApiCatalogEntry, utcnow
from .httpx_retry import get_with_retry

SOURCE_APISGURU = "apisguru"
SOURCE_PUBLIC_DATA = "publicdata"
# 화면에 그대로 쓰는 이름. 서버가 들고 있는 이유: 소스를 하나 더 붙일 때 콘솔을 같이
# 고쳐야 한다면 그 소스는 화면에서 "publicdata" 같은 내부 이름으로 보이게 된다.
SOURCE_LABELS = {SOURCE_APISGURU: "apis.guru", SOURCE_PUBLIC_DATA: "공공데이터"}

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

# 공공데이터포털(data.go.kr) 계열이 요구하는 것. **주소에 이미 있으면 건드리지 않는다.**
#   _type=json   이 계열은 지정하지 않으면 XML을 준다 — 그러면 "JSON이 아닙니다"로 끝난다.
#   numOfRows    기본값이 10이라, 지정하지 않으면 카탈로그에서 열 건만 받아 온다.
# 다른 카탈로그(odcloud 등)는 모르는 파라미터를 무시하므로 붙어 있어도 해가 없다.
_PUBLIC_DATA_DEFAULTS = {"_type": "json", "numOfRows": "1000"}


def _public_data_params(url: str, key: str) -> dict:
    """주소에 적힌 질의를 살린 채 필요한 것만 채운 요청 파라미터.

    **httpx는 params를 주면 URL의 질의문자열을 덮어쓴다**(합치지 않는다 — 확인했다).
    그래서 주소에 pageNo·numOfRows를 적어 두고 인증키까지 설정하면 적어 둔 값이 통째로
    사라졌다. 여기서 먼저 합쳐 두면 주소에 적은 쪽이 이긴다.

    serviceKey는 한 번 풀어서 넘긴다. 포털은 인증키를 인코딩된 것과 아닌 것 두 벌로
    주는데, 인코딩된 쪽을 붙여 넣으면 httpx가 %를 다시 인코딩해서(%2B → %252B) 등록되지
    않은 키라는 응답이 온다 — 키를 잘못 넣은 것과 구분되지 않는 실패다.
    """
    params = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    for name, value in _PUBLIC_DATA_DEFAULTS.items():
        params.setdefault(name, value)
    if key.strip():
        params["serviceKey"] = unquote(key.strip())
    return params


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

    params = _public_data_params(url, settings.public_data_key)
    try:
        res = get_with_retry(url, timeout=15, params=params)
    except Exception as e:  # noqa: BLE001 — 네트워크/서킷 오류를 도메인 오류로 변환
        raise ApiSearchError(f"공공데이터 카탈로그 조회 실패: {e}") from e
    if res.status_code >= 400:
        raise ApiSearchError(f"공공데이터 카탈로그 조회 실패 (HTTP {res.status_code})")
    try:
        payload = res.json()
    except Exception as e:  # noqa: BLE001 — JSON이 아닌 응답(XML·HTML 오류 페이지 등)
        head = (res.text or "")[:200].strip().replace("\n", " ")
        raise ApiSearchError(
            f"공공데이터 카탈로그가 JSON이 아닙니다: {e}"
            f" — 받은 앞부분: {head!r}"
            " (XML이면 주소에 _type=json을 넣으세요)") from e

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
        # 건초더미는 파생값이라 필드가 그대로여도 낡을 수 있다 — **만드는 방법을 바꾸면**
        # 이미 쌓인 행이 옛 방식대로 남는다(URL을 넣기 전에 수집한 행이 그랬다).
        # 결과와 비교해 두면 다음 수집이 알아서 따라잡는다.
        fresh_text = _haystack(item)
        stale = row.search_text != fresh_text
        back = row.removed_at is not None
        if not changed and not stale and not back:
            stats["unchanged"] += 1
            continue
        for field in changed:
            setattr(row, field, item[field])
        if stale:
            row.search_text = fresh_text
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


def _url_text(value: str) -> str:
    """URL을 검색용으로 다듬는다 — 스킴과 끝 슬래시를 뗀다.

    주소를 붙여 넣어 찾는 일이 잦은데 브라우저에서 복사하면 https://가 붙고 끝에
    슬래시가 붙는다. 저장된 값과 한 글자만 어긋나도 부분일치가 깨진다. 그래서 넣을 때와
    찾을 때 같은 방법으로 다듬어 둔다(URL이 아닌 낱말에는 아무 일도 하지 않는다).
    """
    return re.sub(r"^[a-z][a-z0-9+.-]*://", "", value.strip().lower()).rstrip("/")


def _haystack(item: dict) -> str:
    """검색이 훑을 소문자 건초더미.

    **주소도 넣는다.** 붙여 넣어 찾는 대상 중에 URL이 가장 잦은데(스펙 주소를 받아 놓고
    "이거 뭐였지"를 되짚는 경우), 이름·설명만 넣어 두면 그 주소로는 영영 안 걸린다.
    """
    return " ".join([
        str(item["id"]), item["title"], item["description"], " ".join(item["categories"]),
        _url_text(item["homepage"]), _url_text(item["spec_url"]),
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


def search_apis(db: Session, keyword: str, category: str = "", source: str = "",
                limit: int = 30) -> dict:
    """키워드·카테고리·소스로 카탈로그를 찾는다. 셋은 AND, 각각 비우면 그 조건은 안 건다.

    keyword는 이름·설명·카테고리뿐 아니라 **주소(홈페이지·스펙 URL)에도 걸린다** —
    주소를 그대로 붙여 넣어 찾을 수 있다(스킴과 끝 슬래시는 양쪽에서 떼고 맞춘다).

    category가 UNCATEGORIZED("기타")면 카테고리가 없는 항목만 고른다 — 그 항목들은
    카테고리 이름으로는 영영 걸리지 않아서 따로 고를 값이 필요하다.
    source는 SOURCE_LABELS의 키다(공공데이터만 보기 = SOURCE_PUBLIC_DATA).
    셋 다 비면 빈 목록이다(카탈로그 전체를 쏟아내지 않는다) — 소스만 골라도 조건이므로
    그때는 그 소스를 훑는다.

    모르는 source는 빈 목록이 아니라 오류다: 라벨("공공데이터")을 키 자리에 넣는 실수가
    조용히 "그런 API가 없다"로 보이면 원인을 찾을 방법이 없다.
    """
    # 넣을 때와 같은 방법으로 다듬는다 — 주소를 그대로 붙여 넣어도 걸리게.
    kw = _url_text(keyword)
    cat = category.strip()
    src = source.strip()
    if src and src not in SOURCE_LABELS:
        raise ApiSearchError(
            f"모르는 소스입니다: {src} (쓸 수 있는 값: {', '.join(SOURCE_LABELS)})")
    if not kw and not cat and not src:
        return {"results": [], "warnings": []}

    query = _live(select(ApiCatalogEntry))
    if src:
        query = query.where(ApiCatalogEntry.source == src)
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

    **행이 하나도 없는 소스도 0건으로 내보낸다.** 표에 있는 소스만 세면 공공데이터를
    한 번도 못 받은 설치본에서는 그 소스가 아예 없는 것처럼 보이고, 화면에서 고를 수도
    없어서 "왜 공공데이터가 안 나오지"에 답할 자리가 사라진다. enabled가 그 답이다 —
    주소를 넣지 않아 아예 부르지 않는 소스인지, 불렀는데 못 받은 것인지 구분해 준다.
    """
    enabled = {name for name, _load in _sources()}
    per_source: dict[str, dict] = {
        name: {"label": label, "enabled": name in enabled,
               "total": 0, "removed": 0, "updated_at": None}
        for name, label in SOURCE_LABELS.items()
    }
    rows = db.execute(select(
        ApiCatalogEntry.source, ApiCatalogEntry.removed_at, ApiCatalogEntry.updated_at,
    )).all()
    for source, removed_at, updated_at in rows:
        stat = per_source.setdefault(source, {
            "label": SOURCE_LABELS.get(source, source), "enabled": source in enabled,
            "total": 0, "removed": 0, "updated_at": None,
        })
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
