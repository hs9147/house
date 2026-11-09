"""배포 작업 큐 — 프로세스 내 ThreadPoolExecutor 기반.

멀티 서버·재시작 내구성이 필요해지면 이 모듈만 arq(Valkey) 구현으로 교체한다.
호출부는 submit() 경계만 사용하므로 교체 비용이 없다.
"""
from concurrent.futures import Future, ThreadPoolExecutor

from ..config import get_settings

_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=get_settings().deploy_workers, thread_name_prefix="paas-deploy"
        )
    return _executor


def submit(fn, *args, **kwargs) -> Future:
    # 그 안의 LLM 호출이 「(요청 밖)」으로 뭉치지 않게 함수 이름을 기능 이름으로(services/telemetry.feature)
    from . import telemetry  # noqa: PLC0415

    name = f"{fn.__module__.rsplit('.', 1)[-1]}.{getattr(fn, '__name__', 'job').lstrip('_')}"
    return _get_executor().submit(telemetry.run_as, name, fn, *args, **kwargs)
