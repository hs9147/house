import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("PAAS_DATABASE_URL", "sqlite:///./test-paas.db")
os.environ.setdefault("PAAS_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("PAAS_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("PAAS_BASE_DOMAIN", "apps.test")
os.environ.setdefault("PAAS_FEATURES", "deploy,workspace")
# 운영 기본값은 true(PAAS_GITEA_URL 미설정 시 프로젝트 등록 자체를 503으로 막음)이지만,
# git 정책과 무관한 대다수 테스트가 PAAS_GITEA_URL 없이 임의 git_url로 프로젝트를 만든다 —
# 정책 자체를 검증하는 test_git_policy.py/test_project_org_flow.py는 필요시 개별적으로
# monkeypatch로 켠다.
os.environ.setdefault("PAAS_GIT_INTERNAL_ONLY", "false")
# 색인 자리는 기본이 ./data/doc-index다 — 테스트가 거기에 쓰면 실행 사이에 남아
# "앞 테스트가 넣어 둔 문서가 다음 테스트 검색에 걸리는" 순서 의존 실패가 된다.
# 업로드가 색인을 바로 갱신하게 되면서 그런 테스트가 부쩍 늘었다. 개별 픽스처가
# tmp_path로 덮는 것과 별개로, 아무도 안 덮었을 때의 바닥을 여기서 옮긴다.
os.environ.setdefault("PAAS_DOC_INDEX_DIR", tempfile.mkdtemp(prefix="paas-test-index-"))

import pytest  # noqa: E402


def _disable_background_scheduler() -> None:
    """주기 갱신 스케줄러를 테스트에서는 띄우지 않는다.

    create_app이 데몬 스레드를 하나 띄우고 잠시 뒤 갱신을 시작하는데(services/scheduler),
    그 시각은 스위트 한복판이다. 스레드는 자기 세션을 열어 test-paas.db에 쓰고, 그때
    httpx가 어느 테스트의 스텁으로 갈려 있는지는 알 수 없다 — 무작위로 한 테스트만
    흔드는 종류의 실패가 된다. 스케줄러 자체는 test_scheduler에서 tick을 직접 불러 본다.

    **fixture로는 늦다.** app/main.py는 모듈 하단에서 app = create_app()을 하므로,
    테스트 모듈이 `from app.main import create_app`을 하는 순간 — 즉 수집 단계, 어떤
    fixture보다 먼저 — 스레드가 이미 뜬다. conftest는 테스트 모듈보다 먼저 import되니
    여기 import 시점에 막아야 실제로 막힌다.
    """
    from app.services import scheduler

    scheduler.start = lambda: None


_disable_background_scheduler()


@pytest.fixture
def fresh_settings():
    """PAAS_* 환경변수를 monkeypatch한 테스트용 — 설정 캐시를 전후로 비운다."""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_api_sync_throttle():
    """카탈로그 수집의 최소 간격 기록은 프로세스 전역이라 테스트 사이에 남는다 —
    앞 테스트가 찍어 둔 시각 때문에 다음 테스트의 수집이 조용히 건너뛰어지지 않게 비운다."""
    from app.services import apisearch

    apisearch.reset_sync_throttle()
    yield


@pytest.fixture(autouse=True)
def _clear_mcp_tools_cache():
    """MCP tools/list 캐시(TTL 60초)는 프로세스 전역이라 테스트 사이에 남는다 —
    앞 테스트가 같은 URL로 캐시해 둔 값이 다음 테스트의 목킹을 덮지 않게 비운다."""
    from app.services import mcp_client

    mcp_client.clear_tools_cache()
    yield
    mcp_client.clear_tools_cache()


@pytest.fixture(autouse=True)
def _clean_db():
    yield
    from app.db import engine

    engine.dispose()
    # 배포 큐(services/jobs.py)의 백그라운드 스레드가 방금 연 세션을 아직 닫는 중일 수 있다
    # (테스트는 이미 통과했지만 스레드 종료는 비동기). Windows는 열린 파일 삭제를 POSIX보다
    # 엄격히 막아 PermissionError(WinError 32)를 낸다 — 짧게 재시도해 흡수한다.
    db_path = Path("./test-paas.db")
    for attempt in range(20):
        try:
            db_path.unlink(missing_ok=True)
            break
        except PermissionError:
            time.sleep(0.1)
