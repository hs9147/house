"""SSO 만료를 삼킨 요청도 그 사람의 화면에 승인 창을 띄운다(api/llm.SsoWatchMiddleware).

LLM을 부르는 입구 대부분이 실패를 「LLM 호출 실패」 문구로 돌려주거나 삼킨다(에이전트 검토·배포 점검 조언 …).
그래도 요청 중에 만료가 났으면 응답 머리글(x-paas-sso-login)에 승인 정보가 실린다 — 동기 엔드포인트(스레드)와
비동기 둘 다. 브라우저가 없는 호출(MCP·게이트웨이)은 건너뛴다."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import llm as llm_api
from app.services import bedrock


@pytest.fixture
def started(monkeypatch):
    calls: list[str] = []

    def start(profile, log_dir):
        calls.append(profile)
        return {"profile": profile, "code_autofilled": True, "user_code": "AAAA-BBBB",
                "verification_url": "https://d-1.awsapps.com/start/#/device?user_code=AAAA-BBBB",
                "log_path": "", "log_tail": "server log"}

    monkeypatch.setattr(bedrock, "start_sso_login", start)
    return calls


def _app() -> TestClient:
    app = FastAPI()

    def swallow():
        try:
            raise bedrock.SsoExpired("bedrock-dev")
        except bedrock.BedrockError:
            return {"advice": "", "error": "LLM 호출 실패"}   # 「LLM이 없어도 화면은 돈다」

    @app.get("/paas/api/v1/sync")
    def sync_route():
        return swallow()

    @app.get("/paas/api/v1/async")
    async def async_route():
        return swallow()

    @app.get("/paas/api/v1/mcp/docs")
    def mcp_route():
        return swallow()

    @app.get("/paas/api/v1/fine")
    def fine():
        return {"ok": True}

    app.add_middleware(llm_api.SsoWatchMiddleware)
    return TestClient(app)


def test_swallowed_expiry_still_hands_the_login_to_the_browser(started):
    c = _app()
    for path in ("/paas/api/v1/sync", "/paas/api/v1/async"):
        r = c.get(path)
        assert r.status_code == 200 and r.json()["error"] == "LLM 호출 실패"
        login = json.loads(r.headers["x-paas-sso-login"])
        assert login["verification_url"].endswith("user_code=AAAA-BBBB") and login["profile"] == "bedrock-dev"
        assert "log_tail" not in login            # 서버 로그는 내보내지 않는다
    assert started == ["bedrock-dev", "bedrock-dev"]


def test_no_header_without_expiry_or_without_a_browser(started):
    c = _app()
    assert "x-paas-sso-login" not in c.get("/paas/api/v1/fine").headers
    assert "x-paas-sso-login" not in c.get("/paas/api/v1/mcp/docs").headers   # 승인할 사람이 없다
    assert started == []
