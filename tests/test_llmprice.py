"""LLM 단가(services/llmprice) — 서버가 직접 얻을 수 있는 경우만(Azure Retail Prices API). 망은 막고 미터만 준다."""
import pytest

from app.models import LlmProvider, LlmProviderKind
from app.services import llmprice


def _meter(name: str, price: float, unit: str = "1M", date: str = "2026-10-01T00:00:00Z") -> dict:
    return {"meterName": name, "retailPrice": price, "unitOfMeasure": unit, "effectiveStartDate": date}


# 실제 6.1-sol 미터 이름(2026-10-01, prices.azure.com) — 짧은·긴 문맥, Standard·Flex·Data Zone, 캐시 쓰기가 섞여 있다
METERS = [
    _meter("6.1-sol ShortCo Inp Std Gl 1M Tokens", 2.0), _meter("6.1-sol ShortCo Cd Inp Std Gl 1M Tokens", 0.1),
    _meter("6.1-sol ShortCo Opt Std Gl 1M Tokens", 10.0), _meter("6.1-sol ShortCo Cd Wr Std Gl 1M Tokens", 2.5),
    _meter("6.1-sol LongCo Inp Std Gl 1M Tokens", 4.0), _meter("6.1-sol LongCo Opt Std Gl 1M Tokens", 15.0),
    _meter("6.1-sol ShortCo Inp Flex Gl 1M Tokens", 1.0), _meter("6.1-sol ShortCo Opt Flex Gl 1M Tokens", 5.0),
    _meter("6.1-sol ShortCo Inp Std DZ 1M Tokens", 2.2), _meter("6.1-sol ShortCo Opt Std DZ 1M Tokens", 11.0),
    _meter("6.1-sol-mini ShortCo Inp Std Gl 1M Tokens", 0.3),
]


@pytest.fixture(autouse=True)
def _fresh():
    llmprice.reset_cache()
    yield
    llmprice.reset_cache()


def test_picks_global_standard_short_context_input_cached_output():
    price = llmprice.pick(METERS, "6.1-sol")
    assert (price["input"], price["cached_input"], price["output"]) == (2.0, 0.1, 10.0)
    assert price["source"] == llmprice.SOURCE and price["effective"] == "2026-10-01"
    assert "Cd Wr" not in " ".join(price["meters"].values())                  # 캐시 쓰기는 쓰지 않는다


def test_spellings_differ_by_model_and_per_1k_units_are_converted():
    items = [_meter("5.6 sol ShortCo Inp Std Gl 1M Tokens", 4.0), _meter("5.6 sol ShortCo Opt Std Gl 1M Tokens", 20.0)]
    assert llmprice.pick(items, "5.6-sol")["input"] == 4.0                    # 5.6 sol(공백) 표기
    old = [_meter("x1 Inp glbl Tokens", 0.0025, "1K"), _meter("x1 Outp glbl Tokens", 0.01, "1K")]
    assert (llmprice.pick(old, "x1")["input"], llmprice.pick(old, "x1")["output"]) == (2.5, 10.0)
    # 입력·출력 중 하나라도 없으면 단가가 없다(지어내지 않는다)
    assert llmprice.pick([_meter("6.1-sol ShortCo Inp Std Gl 1M Tokens", 2.0)], "6.1-sol") is None


def test_only_azure_has_a_price_and_a_custom_deployment_name_has_none(monkeypatch):
    monkeypatch.setattr(llmprice, "_fetch", lambda flt: METERS)
    az = LlmProvider(name="az", kind=LlmProviderKind.azure, base_url="https://x", model="gpt-6.1-sol")
    assert llmprice.price_for(az)["output"] == 10.0
    assert llmprice.price_for(LlmProvider(name="az2", kind=LlmProviderKind.azure, base_url="https://x",
                                          model="my-deploy")) is None        # 배포 이름이 모델 이름이 아니다
    for kind in (LlmProviderKind.aws, LlmProviderKind.openai, LlmProviderKind.internal):
        assert llmprice.price_for(LlmProvider(name="o", kind=kind, base_url="https://x", model="gpt-6.1-sol")) is None


def test_unreachable_price_api_means_no_price_and_is_not_retried_at_once(monkeypatch):
    calls = []

    def down(flt):
        calls.append(flt)
        raise OSError("blocked")

    monkeypatch.setattr(llmprice, "_fetch", down)
    assert llmprice.azure_price("gpt-6.1-sol") is None
    assert llmprice.azure_price("gpt-6.1-sol") is None and len(calls) == 1   # 실패도 한동안 기억한다


def test_cost_counts_cached_input_at_the_cached_price():
    price = llmprice.pick(METERS, "6.1-sol")
    assert llmprice.cost(price, 1_000_000, 0, 1_000_000) == pytest.approx(0.1)
    assert llmprice.cost(price, 1_000_000, 1_000_000) == pytest.approx(12.0)
    assert llmprice.cost(None, 10, 10) is None
