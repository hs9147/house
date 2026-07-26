"""외부 호출 재시도(갭7) + 서킷브레이커(후속4).

재시도: 네트워크 오류(연결·타임아웃)만 백오프 재시도. HTTP 4xx/5xx 응답은
재시도하지 않는다 — 토스 confirm 같은 비멱등 호출의 중복 실행 방지.

서킷브레이커: 호스트별로 연속 네트워크 실패가 임계(FAILURE_THRESHOLD)에 달하면
쿨다운 동안 즉시 차단(CircuitOpenError)해 죽은 외부 서비스에 대한 대기 낭비와
연쇄 지연을 막는다. 쿨다운이 지나면 half-open으로 1회 시도를 허용하고,
성공 시 회로를 닫는다.
"""
import threading
import time
from urllib.parse import urlparse

import httpx

RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 0.5

FAILURE_THRESHOLD = 5  # 연속 실패 이 횟수부터 차단
COOLDOWN_SECONDS = 60.0


class CircuitOpenError(RuntimeError):
    def __init__(self, host: str, remaining: float):
        super().__init__(
            f"외부 서비스 회로 차단 중: {host} (약 {remaining:.0f}초 후 재시도 허용)"
        )
        self.host = host


class _Breaker:
    def __init__(self) -> None:
        self.failures = 0
        self.opened_at: float | None = None


_breakers: dict[str, _Breaker] = {}
_lock = threading.Lock()


def _breaker(host: str) -> _Breaker:
    with _lock:
        if host not in _breakers:
            _breakers[host] = _Breaker()
        return _breakers[host]


def reset_breakers() -> None:
    """테스트·수동 복구용."""
    with _lock:
        _breakers.clear()


def _check_open(host: str) -> None:
    b = _breaker(host)
    if b.opened_at is None:
        return
    elapsed = time.monotonic() - b.opened_at
    if elapsed < COOLDOWN_SECONDS:
        raise CircuitOpenError(host, COOLDOWN_SECONDS - elapsed)
    # 쿨다운 경과 → half-open: 이번 1회 시도를 허용 (성공하면 닫히고, 실패하면 다시 열림)
    b.opened_at = None
    b.failures = FAILURE_THRESHOLD - 1


def _record_failure(host: str) -> None:
    b = _breaker(host)
    b.failures += 1
    if b.failures >= FAILURE_THRESHOLD:
        b.opened_at = time.monotonic()


def _record_success(host: str) -> None:
    b = _breaker(host)
    b.failures = 0
    b.opened_at = None


def post_with_retry(url: str, **kwargs) -> httpx.Response:
    return _request_with_retry(httpx.post, url, **kwargs)


def get_with_retry(url: str, **kwargs) -> httpx.Response:
    """GET은 멱등이라 재시도가 안전하다(외부 API 디렉터리 조회 등 읽기 전용 호출용)."""
    return _request_with_retry(httpx.get, url, **kwargs)


def _request_with_retry(fn, url: str, **kwargs) -> httpx.Response:
    host = urlparse(url).netloc or url
    _check_open(host)
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            res = fn(url, **kwargs)
            _record_success(host)
            return res
        except RETRYABLE as e:
            last = e
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(BACKOFF_SECONDS * (attempt + 1))
    _record_failure(host)
    raise last  # type: ignore[misc]
