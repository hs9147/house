"""웹 검색 — 지역마다 강한 검색 출처를 두고, **어느 출처로 찾을지는 LLM이 고른다**(MCP /mcp/web).

  글로벌  Brave(Search API) — Google Custom Search JSON API는 새 고객을 받지 않고 2027-01-01에 끝난다(공식
          안내), Bing Search API는 2025-08-11에 끝났다. Brave는 자체 색인이고 매달 무료 크레딧이 있다
  한국    Naver(검색 API — 웹 문서·뉴스, 무료 하루 25,000번)
  일본    Brave 일본 지역 — 야후 재팬은 공식 웹 검색 API가 없다(검색 화면을 긁는 것은 약관 위반이라 하지 않는다).
          Brave에 나라(JP)·언어(ja) 조건을 건다
  중국    Baidu(千帆 AI 검색 API — 검색 화면은 봇 확인(캡차)으로 막힌다)

출처마다 **강점 설명 한 덩어리**(strengths)를 둔다 — 지역·범위·분야를 필드로 나누지 않는다. 목적은 LLM이 읽고
고르게 하는 것이라 문장이 낫다. 출처는 키를 넣은 것만 켜지고(enabled), 새 출처는 SOURCES에 한 줄과 어댑터로
더한다(검색 네트워크를 넓혀 가며 운영한다).

검색어는 회사 밖으로 나간다 — 그래서 스마트워크 대화·통합 서버 묶음에는 넣지 않고 별도 MCP 서버로만 둔다.
감사에는 출처와 검색어 길이만 남긴다(검색어 자체는 남기지 않는다 — 호출하는 쪽이 남긴다).
"""
import re
import threading
import time
from html import unescape

import httpx

from ..config import get_settings

TIMEOUT = 15.0
MAX_RESULTS = 10
_TAG = re.compile(r"<[^>]+>")

_used: dict[str, list[float]] = {}   # 부르는 사람 → 최근 한 시간의 검색 시각(프로세스 안에서만)
_lock = threading.Lock()


class SearchError(ValueError):
    pass


def _clean(text) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub("", str(text or "")))).strip()


# --- 어댑터 ---

def _brave(query: str, count: int, params: dict) -> list[dict]:
    res = httpx.get("https://api.search.brave.com/res/v1/web/search", timeout=TIMEOUT,
                    params={"q": query, "count": count, **params},
                    headers={"X-Subscription-Token": get_settings().search_brave_key, "Accept": "application/json"})
    body = _json(res, "Brave")
    return [{"title": _clean(i.get("title")), "url": i.get("url", ""), "snippet": _clean(i.get("description")),
             **({"date": i["age"]} if i.get("age") else {})}
            for i in (body.get("web") or {}).get("results") or [] if isinstance(i, dict)]


def _naver(kind: str):
    def search(query: str, count: int, _params: dict) -> list[dict]:
        s = get_settings()
        res = httpx.get(f"https://openapi.naver.com/v1/search/{kind}.json", timeout=TIMEOUT,
                        params={"query": query, "display": count},
                        headers={"X-Naver-Client-Id": s.search_naver_id, "X-Naver-Client-Secret": s.search_naver_secret})
        body = _json(res, "Naver")
        return [{"title": _clean(i.get("title")), "url": i.get("originallink") or i.get("link", ""),
                 "snippet": _clean(i.get("description")), **({"date": i["pubDate"]} if i.get("pubDate") else {})}
                for i in body.get("items") or []]
    return search


def _baidu(query: str, count: int, _params: dict) -> list[dict]:
    s = get_settings()
    res = httpx.post("https://qianfan.baidubce.com/v2/ai_search/web_search", timeout=TIMEOUT,
                     headers={"Authorization": f"Bearer {s.search_baidu_key}"},
                     json={"messages": [{"role": "user", "content": query}], "search_source": "baidu_search_v2",
                           "resource_type_filter": [{"type": "web", "top_k": count}]})
    body = _json(res, "Baidu")
    return [{"title": _clean(r.get("title")), "url": r.get("url", ""), "snippet": _clean(r.get("content"))[:500],
             **({"date": r["date"]} if r.get("date") else {})}
            for r in body.get("references") or [] if isinstance(r, dict)]


def _json(res: httpx.Response, who: str) -> dict:
    try:
        body = res.json()
    except ValueError:
        raise SearchError(f"{who} 검색이 JSON이 아닌 응답을 했습니다 (HTTP {res.status_code})")
    if res.status_code >= 400:
        err = body.get("error") if isinstance(body, dict) else None
        said = (err.get("message") or err.get("detail") if isinstance(err, dict) else None) or (
            body.get("errorMessage") or body.get("message") if isinstance(body, dict) else "")
        raise SearchError(f"{who} 검색 실패 (HTTP {res.status_code}): {str(said)[:200]}")
    return body if isinstance(body, dict) else {}


# --- 출처 ---
# strengths는 LLM이 읽고 고르는 글이다 — 어느 지역·범위·분야에서 강한지, 어디서 약한지를 함께 적는다.

def _brave_on() -> bool:
    return bool(get_settings().search_brave_key)


def _naver_on() -> bool:
    s = get_settings()
    return bool(s.search_naver_id and s.search_naver_secret)


SOURCES = [
    {"id": "global", "name": "Brave (전 세계)",
     "strengths": "전 세계 웹 — 영어권 자료, 글로벌 기업·제품·표준, 기술 문서·개발(공식 문서·GitHub·Stack Overflow),"
                  " 학술·국제 뉴스에 강하다. 지역을 가리지 않는 기본 출처(자체 색인). 한국의 블로그·카페, 중국 본토 사이트는"
                  " 약하다.",
     "enabled": _brave_on, "search": _brave, "params": {}},
    {"id": "naver", "name": "Naver 웹 문서",
     "strengths": "한국 — 한국어 질의, 국내 기업·기관·제도·생활 정보, 블로그·카페·지식iN 등 국내 사용자 글에 강하다."
                  " 국내 업체·제품 평판이나 국내에서만 쓰는 용어를 찾을 때. 영어권·해외 기술 자료는 Google이 낫다.",
     "enabled": _naver_on, "search": _naver("webkr"), "params": {}},
    {"id": "naver_news", "name": "Naver 뉴스",
     "strengths": "한국 뉴스 — 국내 언론 기사(최신 순). 국내 기업·산업·정책의 최근 소식, 발표·사건의 날짜를 확인할 때.",
     "enabled": _naver_on, "search": _naver("news"), "params": {}},
    {"id": "japan", "name": "Brave 일본 지역",
     "strengths": "일본 — 일본어 웹, 일본 기업·제품·제도·규격, 일본 시장 정보(나라 JP·언어 ja 조건). 일본어로 묻는 것이"
                  " 좋다. 야후 재팬은 공식 검색 API가 없어 이 출처가 일본을 맡는다.",
     "enabled": _brave_on, "search": _brave, "params": {"country": "JP", "search_lang": "jp"}},
    {"id": "baidu", "name": "Baidu",
     "strengths": "중국 — 중국어 웹, 중국 기업·정책·법규·시장 정보, 중국 본토에서만 보이는 사이트(Google에 잘 잡히지 않는"
                  " 곳)에 강하다. 중국어(간체)로 묻는 것이 좋다.",
     "enabled": lambda: bool(get_settings().search_baidu_key), "search": _baidu, "params": {}},
]
_BY_ID = {s["id"]: s for s in SOURCES}


def sources() -> list[dict]:
    """LLM이 고를 수 있게 — id·이름·강점·켜짐 여부."""
    return [{"id": s["id"], "name": s["name"], "strengths": s["strengths"], "enabled": s["enabled"]()}
            for s in SOURCES]


def configured() -> bool:
    return any(s["enabled"]() for s in SOURCES)


def search(actor: str, query: str, source: str, count: int = MAX_RESULTS) -> dict:
    query = re.sub(r"\s+", " ", str(query or "")).strip()[:300]
    if not query:
        raise SearchError("검색어가 비어 있습니다.")
    src = _BY_ID.get(source)
    if src is None:
        raise SearchError(f"모르는 출처입니다: {source} — list_search_sources의 id를 쓰세요.")
    if not src["enabled"]():
        raise SearchError(f"출처 {source}는 꺼져 있습니다(인증키가 없습니다) — 켜진 출처를 고르세요.")
    limit = get_settings().search_max_per_hour
    with _lock:
        now = time.time()
        recent = [t for t in _used.get(actor, []) if now - t < 3600]
        if len(recent) >= limit:
            raise SearchError(f"한 시간에 {limit}번까지 검색할 수 있습니다 — 잠시 뒤에 다시 하세요.")
        recent.append(now)
        _used[actor] = recent
    try:
        results = src["search"](query, max(1, min(int(count or MAX_RESULTS), MAX_RESULTS)), src["params"])
    except httpx.HTTPError as e:
        raise SearchError(f"{src['name']}에 연결하지 못했습니다: {type(e).__name__}")
    return {"source": source, "name": src["name"], "query": query,
            "results": [{**r, "source": source} for r in results if r.get("url")]}
