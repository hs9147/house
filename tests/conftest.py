import os
import sys
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

import pytest  # noqa: E402


@pytest.fixture
def fresh_settings():
    """PAAS_* 환경변수를 monkeypatch한 테스트용 — 설정 캐시를 전후로 비운다."""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
