"""AWS Bedrock — 자격증명 프로필로 서명하는 경로.

Bedrock에는 붙여넣을 정적 키가 없다. 여기서 지키는 것은 세 가지다:
  1. 프로필 목록은 botocore 없이도 읽힌다(화면에서 골라야 하므로)
  2. 만료는 재로그인 명령까지 붙여 드러난다(8시간마다 실제로 일어나는 일)
  3. Converse 응답이 OpenAI 모양으로 되돌아온다 — 호출부의 tool-call 루프를 그대로 쓴다
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import LlmProvider, LlmProviderKind
from app.services import bedrock
from app.services import llm as llm_service

ADMIN = {"x-api-key": "test-admin-key"}

CONFIG = """\
[default]
region = ap-northeast-2

[profile bedrock-dev]
sso_session = corp-sso
region = us-east-1

[sso-session corp-sso]
sso_start_url = https://example.awsapps.com/start
"""


@pytest.fixture
def aws_config(tmp_path, monkeypatch):
    path = tmp_path / "config"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    return path


def test_token_expiry_is_the_sso_token_not_the_credentials(aws_config, monkeypatch, tmp_path):
    """재로그인이 필요해지는 시각은 **SSO 토큰** 만료다 — 자격증명(STS) 만료는 더 길다.

    실측에서 토큰은 08:15Z, 그 토큰으로 받은 자격증명은 19:25Z 만료였다. 긴 쪽을 보여 주면
    이미 재로그인이 필요해진 뒤에도 여유가 있는 것처럼 읽힌다.
    """
    import hashlib
    import json

    # 캐시 파일 이름은 sso_session 이름의 sha1이다(botocore와 같은 규칙).
    cache = tmp_path / ".aws" / "sso" / "cache"
    cache.mkdir(parents=True)
    key = hashlib.sha1(b"corp-sso").hexdigest()
    (cache / f"{key}.json").write_text(
        json.dumps({"accessToken": "x", "expiresAt": "2026-09-21T08:15:09Z"}), encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    assert bedrock.sso_token_expiry("bedrock-dev") == "2026-09-21T08:15:09Z"
    # SSO가 아닌 프로필은 토큰이 없다 — 없는 것으로 둔다(자격증명 만료로 떨어진다)
    assert bedrock.sso_token_expiry("default") == ""


def test_expired_profile_still_reports_when_it_lapsed(aws_config, monkeypatch, tmp_path):
    """만료됐을 때가 오히려 "언제 끊겼는지"를 알아야 할 때다 — ok=false에도 시각을 싣는다."""
    import hashlib
    import json

    cache = tmp_path / ".aws" / "sso" / "cache"
    cache.mkdir(parents=True)
    (cache / f"{hashlib.sha1(b'corp-sso').hexdigest()}.json").write_text(
        json.dumps({"expiresAt": "2026-09-21T08:15:09Z"}), encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(bedrock, "_credentials",
                        lambda p: (_ for _ in ()).throw(bedrock.BedrockError("만료됐습니다")))

    status = bedrock.profile_status("bedrock-dev")
    assert status["ok"] is False
    assert status["expires_at"] == "2026-09-21T08:15:09Z"


def test_profiles_endpoint_reports_where_it_read(aws_config):
    """서비스로 돌면 홈이 서비스 계정 것이라 목록이 빈다 — 어디를 읽었는지 밝혀야 안다."""
    c = TestClient(create_app())
    body = c.get("/paas/api/v1/llm/aws/profiles", headers=ADMIN).json()
    assert body["config_path"] == str(aws_config)


def test_lists_profiles_without_botocore(aws_config, monkeypatch):
    """프로필은 화면에서 골라야 하므로 botocore가 없어도 읽혀야 한다."""
    monkeypatch.setattr(bedrock, "botocore_available", lambda: False)
    names = [p["name"] for p in bedrock.list_profiles()]
    assert names == ["bedrock-dev", "default"]
    by_name = {p["name"]: p for p in bedrock.list_profiles()}
    assert by_name["bedrock-dev"]["region"] == "us-east-1"
    assert by_name["bedrock-dev"]["sso_session"] == "corp-sso"
    # sso-session은 로그인 세션 정의지 프로필이 아니다 — 고를 수 있게 두면 안 된다
    assert "corp-sso" not in names


def test_status_without_botocore_says_unknown_not_ok(aws_config, monkeypatch):
    """판정 불가를 '됨'으로도 '안 됨'으로도 말하지 않는다."""
    monkeypatch.setattr(bedrock, "botocore_available", lambda: False)
    status = bedrock.profile_status("bedrock-dev")
    assert status["ok"] is None
    assert "botocore" in status["reason"]


def test_region_comes_from_endpoint_then_profile(aws_config):
    """리전은 요청 URL과 서명 양쪽에 들어가므로 한 곳에서 정한다."""
    assert bedrock.region_from_url(
        "https://bedrock-runtime.eu-west-1.amazonaws.com") == "eu-west-1"
    # Endpoint에 리전이 없으면 프로필 설정을 따른다
    assert bedrock.region_from_url("https://bedrock.internal", "bedrock-dev") == "us-east-1"


def test_expired_sso_token_names_the_login_command(monkeypatch):
    """8시간마다 실제로 일어나는 일이다 — '인증 오류'로 뭉개면 무엇을 할지 알 수 없다."""
    # botocore는 선택 의존성이다 — 없는 설치본에서는 이 경로 자체가 없다(위 테스트가 그쪽을 본다).
    exc = pytest.importorskip("botocore.exceptions")

    class _Session:
        def __init__(self, profile=None): ...
        def get_credentials(self):
            raise exc.UnauthorizedSSOTokenError()

    monkeypatch.setattr("botocore.session.Session", _Session)
    with pytest.raises(bedrock.BedrockError) as err:
        bedrock._credentials("bedrock-dev")
    assert "aws sso login --profile bedrock-dev" in str(err.value)


def test_openai_messages_become_converse_blocks():
    system, messages = bedrock._to_converse([
        {"role": "system", "content": "규칙"},
        {"role": "user", "content": "안녕"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "list_tasks", "arguments": '{"a": 1}'}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": "결과1"},
        {"role": "tool", "tool_call_id": "t2", "content": "결과2"},
    ])
    assert system == [{"text": "규칙"}]
    assert messages[0] == {"role": "user", "content": [{"text": "안녕"}]}
    assert messages[1]["content"][0]["toolUse"]["input"] == {"a": 1}
    # Bedrock은 역할이 번갈아야 한다 — 연달아 온 도구 결과가 한 메시지로 묶여야 한다
    assert len(messages) == 3
    assert [b["toolResult"]["toolUseId"] for b in messages[2]["content"]] == ["t1", "t2"]


def test_converse_reply_is_openai_shaped():
    """호출부(llm.chat_completion)의 tool-call 루프를 Bedrock용으로 다시 쓰지 않기 위한 경계."""
    out = bedrock._from_converse({"output": {"message": {"role": "assistant", "content": [
        {"text": "확인했습니다"},
        {"toolUse": {"toolUseId": "t9", "name": "report", "input": {"task": 3}}},
    ]}}})
    message = out["choices"][0]["message"]
    assert message["content"] == "확인했습니다"
    assert message["tool_calls"][0]["id"] == "t9"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"task": 3}


def test_chat_completion_routes_bedrock_by_profile(monkeypatch):
    """프로필이 있으면 서명 경로로, 없으면 기존 api_key 경로로 간다(하위 호환)."""
    signed = LlmProvider(name="bd", kind=LlmProviderKind.aws, model="anthropic.claude:0",
                         base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
                         aws_profile="bedrock-dev")
    legacy = LlmProvider(name="gw", kind=LlmProviderKind.aws, model="m",
                         base_url="https://gateway.internal", aws_profile=None)
    assert llm_service.uses_aws_credentials(signed) is True
    assert llm_service.uses_aws_credentials(legacy) is False

    calls = {}

    def fake_converse(*, profile, region, model_id, messages, tools=None, **kw):
        calls.update(profile=profile, region=region, model_id=model_id)
        return {"choices": [{"message": {"role": "assistant", "content": "서명됨"}}]}

    monkeypatch.setattr(bedrock, "converse", fake_converse)
    monkeypatch.setattr(llm_service, "_post_chat",
                        lambda *a: {"choices": [{"message": {"content": "키경로"}}]})
    assert llm_service.chat_completion(signed, [{"role": "user", "content": "x"}]) == "서명됨"
    assert calls == {"profile": "bedrock-dev", "region": "us-east-1",
                     "model_id": "anthropic.claude:0"}
    assert llm_service.chat_completion(legacy, [{"role": "user", "content": "x"}]) == "키경로"


def test_converse_url_encodes_the_model_id(monkeypatch):
    """모델 ID에는 `.`과 `:`이 들어간다 — 경로 조각으로 인코딩하지 않으면 404가 된다."""
    monkeypatch.setattr(bedrock, "botocore_available", lambda: True)
    monkeypatch.setattr(bedrock, "_frozen", lambda profile: object())
    seen = {}

    def fake_post(url, payload, frozen, region, timeout):
        seen["url"] = url
        seen["body"] = json.loads(payload)
        return {"output": {"message": {"content": [{"text": "ok"}]}}}

    monkeypatch.setattr(bedrock, "_post_signed", fake_post)
    bedrock.converse(profile="p", region="us-east-1",
                     model_id="anthropic.claude-sonnet-4:0",
                     messages=[{"role": "system", "content": "s"},
                               {"role": "user", "content": "u"}])
    assert "anthropic.claude-sonnet-4%3A0/converse" in seen["url"]
    assert seen["body"]["system"] == [{"text": "s"}]


def test_converse_without_botocore_says_how_to_install(monkeypatch):
    monkeypatch.setattr(bedrock, "botocore_available", lambda: False)
    with pytest.raises(bedrock.BedrockError, match="botocore"):
        bedrock.converse(profile="p", region="us-east-1", model_id="m",
                         messages=[{"role": "user", "content": "x"}])


def test_profiles_endpoint_is_admin_only(aws_config):
    c = TestClient(create_app())
    r = c.get("/paas/api/v1/llm/aws/profiles", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert [p["name"] for p in r.json()["profiles"]] == ["bedrock-dev", "default"]
    assert all("login_command" in p for p in r.json()["profiles"])
    assert c.get("/paas/api/v1/llm/aws/profiles").status_code in (401, 403)


def test_provider_stores_and_exposes_the_profile():
    """프로필은 비밀이 아니다 — 만료 시 어느 프로필로 재로그인할지 화면이 말해야 한다."""
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/llm/providers", json={
        "name": "bedrock-claude", "kind": "aws",
        "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
        "model": "anthropic.claude-sonnet-4:0", "aws_profile": "bedrock-dev",
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["aws_profile"] == "bedrock-dev"
    listing = c.get("/paas/api/v1/llm/providers", headers=ADMIN).json()
    assert any(p["aws_profile"] == "bedrock-dev" for p in listing)


def test_profile_on_non_aws_kind_is_rejected():
    """저장은 되고 호출에는 안 쓰이면, 설정한 사람은 적용됐다고 믿는다."""
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/llm/providers", json={
        "name": "wrong-kind", "kind": "openai", "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o", "aws_profile": "bedrock-dev",
    }, headers=ADMIN)
    assert r.status_code == 422
    assert "aws_profile" in r.text
