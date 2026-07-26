"""갭7·후속4 — 외부 호출 재시도 + 서킷브레이커."""
import httpx
import pytest

from app.services import httpx_retry


@pytest.fixture(autouse=True)
def _clean_breakers():
    httpx_retry.reset_breakers()
    yield
    httpx_retry.reset_breakers()


class _Res:
    status_code = 200


def test_retries_connect_error_then_succeeds(monkeypatch):
    calls = []

    def flaky(url, **kw):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("refused")
        return _Res()

    monkeypatch.setattr(httpx_retry.httpx, "post", flaky)
    monkeypatch.setattr(httpx_retry.time, "sleep", lambda s: None)
    res = httpx_retry.post_with_retry("https://x/api")
    assert res.status_code == 200
    assert len(calls) == 3


def test_gives_up_after_max_attempts(monkeypatch):
    calls = []

    def down(url, **kw):
        calls.append(1)
        raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr(httpx_retry.httpx, "post", down)
    monkeypatch.setattr(httpx_retry.time, "sleep", lambda s: None)
    with pytest.raises(httpx.ConnectTimeout):
        httpx_retry.post_with_retry("https://x/api")
    assert len(calls) == 3


def test_http_error_response_not_retried(monkeypatch):
    """4xx/5xx는 '응답을 받은' 상태 — 비멱등 호출 중복 방지를 위해 재시도하지 않는다."""
    calls = []

    class _Bad:
        status_code = 500

    monkeypatch.setattr(httpx_retry.httpx, "post", lambda url, **kw: (calls.append(1), _Bad())[1])
    res = httpx_retry.post_with_retry("https://x/api")
    assert res.status_code == 500
    assert len(calls) == 1


def test_non_network_exception_not_retried(monkeypatch):
    calls = []

    def boom(url, **kw):
        calls.append(1)
        raise ValueError("programming error")

    monkeypatch.setattr(httpx_retry.httpx, "post", boom)
    with pytest.raises(ValueError):
        httpx_retry.post_with_retry("https://x/api")
    assert len(calls) == 1


# --- 서킷브레이커 (후속4) ---

def _always_down(calls):
    def down(url, **kw):
        calls.append(1)
        raise httpx.ConnectError("refused")
    return down


def test_circuit_opens_after_threshold(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx_retry.httpx, "post", _always_down(calls))
    monkeypatch.setattr(httpx_retry.time, "sleep", lambda s: None)

    # 임계(5회 연속 실패)까지는 ConnectError, 이후엔 즉시 차단
    for _ in range(httpx_retry.FAILURE_THRESHOLD):
        with pytest.raises(httpx.ConnectError):
            httpx_retry.post_with_retry("https://dead.example.com/api")
    before = len(calls)
    with pytest.raises(httpx_retry.CircuitOpenError):
        httpx_retry.post_with_retry("https://dead.example.com/api")
    assert len(calls) == before  # 차단 중에는 실제 호출 없음


def test_circuit_is_per_host(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx_retry.httpx, "post", _always_down(calls))
    monkeypatch.setattr(httpx_retry.time, "sleep", lambda s: None)
    for _ in range(httpx_retry.FAILURE_THRESHOLD):
        with pytest.raises(httpx.ConnectError):
            httpx_retry.post_with_retry("https://dead.example.com/api")

    # 다른 호스트는 영향 없음
    class _Ok:
        status_code = 200

    monkeypatch.setattr(httpx_retry.httpx, "post", lambda url, **kw: _Ok())
    assert httpx_retry.post_with_retry("https://alive.example.com/api").status_code == 200


def test_half_open_after_cooldown_then_close_on_success(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx_retry.httpx, "post", _always_down(calls))
    monkeypatch.setattr(httpx_retry.time, "sleep", lambda s: None)
    for _ in range(httpx_retry.FAILURE_THRESHOLD):
        with pytest.raises(httpx.ConnectError):
            httpx_retry.post_with_retry("https://flaky.example.com/api")
    with pytest.raises(httpx_retry.CircuitOpenError):
        httpx_retry.post_with_retry("https://flaky.example.com/api")

    # 쿨다운 경과 시뮬레이션 → half-open 1회 허용, 성공 시 회로 닫힘
    now = httpx_retry.time.monotonic()
    monkeypatch.setattr(httpx_retry.time, "monotonic",
                        lambda: now + httpx_retry.COOLDOWN_SECONDS + 1)

    class _Ok:
        status_code = 200

    monkeypatch.setattr(httpx_retry.httpx, "post", lambda url, **kw: _Ok())
    assert httpx_retry.post_with_retry("https://flaky.example.com/api").status_code == 200
    # 닫힌 뒤에는 연속 호출도 정상
    assert httpx_retry.post_with_retry("https://flaky.example.com/api").status_code == 200
