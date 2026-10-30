"""LLM 단가 — **서버가 직접 얻을 수 있는 경우만** 쓴다. 사람이 적어 넣는 단가는 두지 않는다(낡아도 아무도 모른다).

지금 직접 얻을 수 있는 곳은 Azure다: Azure Retail Prices API(prices.azure.com, 인증 없음)가 모델·배포 유형·
문맥 길이별 미터를 준다. 예: GPT-6.1 sol은 "6.1-sol ShortCo Inp Std Gl"(입력)·"… Cd Inp …"(캐시 입력)·
"… Opt …"(출력)이 100만 토큰당 가격이다. 그 밖의 프로바이더(OpenAI·Anthropic 직결, Bedrock, 사내 LLM)는
단가를 None으로 둔다 — 화면은 그 호출의 비용을 보이지 않고 토큰만 보인다.

고르는 미터: **Global Standard, 짧은 문맥**(ShortCo). 배포 유형(Data Zone·Flex·Provisioned)은 프로바이더 설정만으로는
알 수 없어 Global Standard로 본다 — 다른 유형이면 실제 청구와 다를 수 있다(화면에 어느 미터인지 함께 보인다).
Azure 프로바이더의 model은 **배포 이름**이다. 배포 이름이 모델 이름(gpt-6.1-sol 등)이 아니면 미터를 찾지 못해
단가가 없다.
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request

PRICES_API = "https://prices.azure.com/api/retail/prices"
SOURCE = "Azure Retail Prices API"
TIMEOUT = 15
OK_TTL = 24 * 3600          # 단가는 자주 바뀌지 않는다 — 하루 한 번
FAIL_TTL = 3600             # 못 얻었으면(망이 막힘·미터 없음) 한 시간 뒤에 다시

_cache: dict[str, tuple[dict | None, float]] = {}
_lock = threading.Lock()

_SKIP = {"batch", "flex", "fl", "pp", "ft", "dz", "dzone", "data", "zone", "regnl", "regional", "rgnl", "hstng",
         "hosting", "trng", "training", "aud", "audio", "rt", "realtime", "img", "image"}


def _meter_key(model: str) -> str:
    """배포 이름에서 미터 이름의 앞부분 — gpt-6.1-sol → 6.1-sol, gpt-4o → 4o."""
    key = model.strip().lower()
    key = re.sub(r"^(azure[-_ ]?)?gpt[-_ ]?", "", key)
    return key


def _fetch(flt: str) -> list[dict]:
    url = PRICES_API + "?" + urllib.parse.urlencode({"$filter": flt})
    items: list[dict] = []
    while url and len(items) < 5000:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as res:   # noqa: S310 — 고정된 Azure 공개 주소
            data = json.load(res)
        items += data.get("Items") or []
        url = data.get("NextPageLink")
    return items


def _per_million(item: dict) -> float | None:
    unit = str(item.get("unitOfMeasure") or "").upper().replace(" ", "")
    price = item.get("retailPrice")
    if not isinstance(price, (int, float)):
        return None
    return float(price) * (1000 if unit == "1K" else 1 if unit == "1M" else 0) or None


def _spellings(key: str) -> list[str]:
    """미터 이름의 표기는 모델마다 다르다 — 6.1-sol · 5.6 sol · 56sol이 같은 모양이다(실측)."""
    return list(dict.fromkeys([key, key.replace("-", " "), re.sub(r"[-.\s]", "", key)]))


def pick(items: list[dict], key: str) -> dict | None:
    """미터들에서 Global Standard(짧은 문맥)의 입력·캐시 입력·출력 단가를 고른다. 셋 중 입력·출력이 없으면 None."""
    found: dict[str, dict] = {}
    for it in items:
        name = str(it.get("meterName") or "").lower()
        head = next((k for k in _spellings(key) if name.startswith(k + " ") or name.startswith(k + "-")), None)
        if head is None:
            continue   # 6.1-sol이 6.1-sol-mini를 잡지 않게
        words = set(re.split(r"[\s\-]+", name[len(head):]))
        if words & _SKIP or "longco" in words or "loco" in words:
            continue
        if not ({"gl", "glbl", "global"} & words):
            continue
        if {"wr", "write"} & words:
            continue                     # 캐시 쓰기 — 이 플랫폼의 기록으로는 나눌 수 없어 쓰지 않는다
        cached = bool({"cd", "cached", "cchd"} & words)
        if {"inp", "input"} & words:
            slot = "cached_input" if cached else "input"
        elif {"opt", "outp", "output"} & words:
            slot = "output"
        else:
            continue
        price = _per_million(it)
        if price is not None:
            found.setdefault(slot, {"price": price, "meter": it.get("meterName"),
                                    "effective": str(it.get("effectiveStartDate") or "")[:10]})
    if "input" not in found or "output" not in found:
        return None
    return {
        "input": found["input"]["price"], "output": found["output"]["price"],
        "cached_input": found.get("cached_input", found["input"])["price"],
        "unit": "USD / 1M tokens", "source": SOURCE, "tier": "Global Standard · 짧은 문맥",
        "meters": {slot: v["meter"] for slot, v in found.items()},
        "effective": max(v["effective"] for v in found.values()),
    }


def azure_price(model: str) -> dict | None:
    key = _meter_key(model)
    if not key:
        return None
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[1] < (OK_TTL if hit[0] else FAIL_TTL):
            return hit[0]
    try:
        names = " or ".join(f"contains(tolower(meterName), '{k.replace(chr(39), '')}')" for k in _spellings(key))
        items = _fetch(f"startswith(productName, 'Azure OpenAI') and priceType eq 'Consumption' and ({names})")
        price = pick(items, key)
    except Exception:  # noqa: BLE001 — 망이 막혔거나 응답이 바뀌었다: 단가를 모르는 것으로 둔다
        price = None
    with _lock:
        _cache[key] = (price, now)
    return price


def price_for(provider) -> dict | None:
    """이 프로바이더의 단가 — 서버가 직접 얻을 수 있을 때만(지금은 Azure). 없으면 None."""
    kind = provider.kind.value if hasattr(provider.kind, "value") else str(provider.kind)
    if kind == "azure" and provider.model:
        return azure_price(provider.model)
    return None


def cost(price: dict | None, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float | None:
    """한 호출의 비용(USD). 캐시된 입력은 prompt_tokens 안에 들어 있다(OpenAI 계열의 셈) — 그만큼은 캐시 단가로."""
    if not price:
        return None
    cached = max(0, min(cached_tokens or 0, prompt_tokens or 0))
    return ((max(0, (prompt_tokens or 0) - cached) * price["input"] + cached * price["cached_input"]
             + (completion_tokens or 0) * price["output"]) / 1_000_000)


def reset_cache() -> None:
    with _lock:
        _cache.clear()
