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


# 실제 응답에서 가려낸 모양(ap-northeast-2). 추론 프로필은 상태가 ACTIVE인 것만,
# 파운데이션 모델은 온디맨드로 부를 수 있는 텍스트 모델만 후보다.
_PROFILES_BODY = {"inferenceProfileSummaries": [
    {"inferenceProfileId": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
     "inferenceProfileName": "Claude Haiku 4.5", "status": "ACTIVE"},
    {"inferenceProfileId": "apac.anthropic.claude-sonnet-4-20250514-v1:0",
     "inferenceProfileName": "Claude Sonnet 4", "status": "ACTIVE"},
    {"inferenceProfileId": "zzz.retired", "status": "INACTIVE"},
]}
_MODELS_BODY = {"modelSummaries": [
    {"modelId": "anthropic.claude-haiku-4-5-20251001-v1:0", "modelName": "Haiku 4.5",
     "inferenceTypesSupported": ["INFERENCE_PROFILE"], "outputModalities": ["TEXT"]},
    {"modelId": "amazon.titan-text-express-v1", "modelName": "Titan Text",
     "inferenceTypesSupported": ["ON_DEMAND"], "outputModalities": ["TEXT"]},
    {"modelId": "amazon.titan-embed-text-v2:0", "modelName": "Titan Embed",
     "inferenceTypesSupported": ["ON_DEMAND"], "outputModalities": ["EMBEDDING"]},
    {"modelId": "old.model", "modelName": "Old", "inferenceTypesSupported": ["ON_DEMAND"],
     "outputModalities": ["TEXT"], "modelLifecycle": {"status": "LEGACY"}},
]}


def _stub_control_plane(monkeypatch):
    monkeypatch.setattr(bedrock, "botocore_available", lambda: True)
    monkeypatch.setattr(bedrock, "_frozen", lambda profile: object())

    def fake_get(url, frozen, region, timeout):
        return _PROFILES_BODY if "inference-profiles" in url else _MODELS_BODY

    monkeypatch.setattr(bedrock, "_get_signed", fake_get)


def test_model_list_puts_inference_profiles_first(monkeypatch):
    """실측: 파운데이션 모델 ID를 그대로 넣으면 "use an inference profile"로 거부된다.

    그러니 실제로 통하는 ID가 목록 앞에 있어야 한다 — 뒤에 있으면 사람이 앞의 것을 고른다.
    """
    _stub_control_plane(monkeypatch)
    models = bedrock.list_models(profile="p", region="ap-northeast-2")
    assert [m["kind"] for m in models[:2]] == ["inference_profile", "inference_profile"]
    assert models[0]["id"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_model_list_drops_what_cannot_be_used_for_chat(monkeypatch):
    """임베딩·비활성·온디맨드 불가는 고를 수 있게 두면 안 된다 — 고르면 실패한다."""
    _stub_control_plane(monkeypatch)
    ids = [m["id"] for m in bedrock.list_models(profile="p", region="ap-northeast-2")]
    assert "amazon.titan-text-express-v1" in ids           # 온디맨드 텍스트 — 후보다
    assert "zzz.retired" not in ids                        # INACTIVE 추론 프로필
    assert "amazon.titan-embed-text-v2:0" not in ids       # 임베딩 전용
    assert "old.model" not in ids                          # LEGACY
    # 온디맨드가 안 되는 모델은 추론 프로필 쪽 ID로 불러야 한다 — 원본 ID는 후보가 아니다
    assert "anthropic.claude-haiku-4-5-20251001-v1:0" not in ids


def test_models_endpoint_surfaces_expiry_with_the_login_command(monkeypatch, aws_config):
    """토큰이 만료되면 목록을 받을 수 없다 — 왜인지와 무엇을 할지가 그대로 올라와야 한다."""
    monkeypatch.setattr(bedrock, "list_models", lambda **kw: (_ for _ in ()).throw(
        bedrock.BedrockError("AWS SSO 토큰이 만료됐습니다. `aws sso login --profile p`")))
    c = TestClient(create_app())
    r = c.get("/paas/api/v1/llm/aws/models?profile=p", headers=ADMIN)
    assert r.status_code == 502
    assert "aws sso login --profile p" in r.json()["detail"]


def test_models_endpoint_is_admin_only(aws_config, monkeypatch):
    monkeypatch.setattr(bedrock, "list_models", lambda **kw: [])
    c = TestClient(create_app())
    assert c.get("/paas/api/v1/llm/aws/models?profile=p", headers=ADMIN).status_code == 200
    assert c.get("/paas/api/v1/llm/aws/models?profile=p").status_code in (401, 403)


def test_models_endpoint_reports_the_region_it_listed(aws_config, monkeypatch):
    """리전이 다르면 목록도 다르다 — 어느 리전에서 받은 목록인지 화면이 말해야 한다."""
    monkeypatch.setattr(bedrock, "list_models", lambda **kw: [])
    c = TestClient(create_app())
    body = c.get("/paas/api/v1/llm/aws/models?profile=bedrock-dev"
                 "&base_url=https://bedrock-runtime.eu-west-1.amazonaws.com",
                 headers=ADMIN).json()
    assert body["region"] == "eu-west-1"
    # Endpoint에 리전이 없으면 프로필 설정을 따른다
    body = c.get("/paas/api/v1/llm/aws/models?profile=bedrock-dev", headers=ADMIN).json()
    assert body["region"] == "us-east-1"


def test_endpoint_is_optional_when_a_profile_is_chosen(aws_config):
    """Endpoint가 필요한지는 고른 프로필이 정한다 — 리전이 주소를 결정한다.

    사람이 `https://bedrock-runtime.<리전>.amazonaws.com`을 적게 두면 리전을 두 곳에
    적는 셈이고, 프로필 리전과 어긋나면 서명 리전과 요청 주소가 갈린다.
    """
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/llm/providers", json={
        "name": "bd-noendpoint", "kind": "aws", "model": "global.anthropic.claude-sonnet-5",
        "aws_profile": "bedrock-dev",  # 이 프로필의 region = us-east-1
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    # 비워 온 것을 등록 시점에 확정해 기록한다 — 목록에 실제 주소가 보여야 한다
    assert r.json()["base_url"] == "https://bedrock-runtime.us-east-1.amazonaws.com"


def test_endpoint_is_still_required_without_a_profile(aws_config):
    """프로필이 없으면 어디로 보낼지 알 수 없다 — 비워 두게 하면 안 된다."""
    c = TestClient(create_app())
    for body in (
        {"name": "no-url-aws", "kind": "aws", "model": "m"},
        {"name": "no-url-openai", "kind": "openai", "model": "m"},
    ):
        r = c.post("/paas/api/v1/llm/providers", json=body, headers=ADMIN)
        assert r.status_code == 422, r.text
        assert "base_url" in r.text


def test_explicit_endpoint_wins_over_the_profile_region(aws_config):
    """적어 준 주소가 가장 구체적인 의사표시다 — 유도값으로 덮지 않는다."""
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/llm/providers", json={
        "name": "bd-explicit", "kind": "aws", "model": "m", "aws_profile": "bedrock-dev",
        "base_url": "https://bedrock-runtime.eu-west-1.amazonaws.com",
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["base_url"] == "https://bedrock-runtime.eu-west-1.amazonaws.com"


def test_config_state_distinguishes_missing_file_from_no_profiles(tmp_path, monkeypatch):
    """둘의 대처가 다르다 — 파일이 없으면 홈이 남의 것이고, 있으면 설정이 다르다."""
    missing = tmp_path / "nope" / "config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(missing))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "nope" / "credentials"))
    state = bedrock.config_state()
    assert state["config_exists"] is False
    assert state["config_path"] == str(missing)

    empty = tmp_path / "config"
    empty.write_text("# 주석만 있다\n", encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(empty))
    assert bedrock.config_state()["config_exists"] is True
    assert bedrock.list_profiles() == []


def test_config_state_names_the_interpreter_that_is_looking(aws_config):
    """venv에 설치했는데도 "botocore가 없다"가 남는 경우가 있다 — 백엔드가 다른
    인터프리터로 돌면 그 venv의 site-packages를 보지 않는다. 설치 대상을 특정해야 한다."""
    import sys

    state = bedrock.config_state()
    assert state["python"] == sys.executable
    assert state["botocore_available"] is bedrock.botocore_available()


def test_profiles_endpoint_carries_the_interpreter(aws_config):
    c = TestClient(create_app())
    body = c.get("/paas/api/v1/llm/aws/profiles", headers=ADMIN).json()
    assert body["python"].endswith(("python.exe", "python", "python3"))
    assert "botocore_available" in body


def test_sso_login_without_the_cli_says_so(monkeypatch, tmp_path):
    """CLI가 없으면 시작조차 못 한다 — botocore로는 device authorization을 사람에게
    보여 줄 방법이 없다. '실패'로 뭉개지 않고 무엇이 없는지 말한다."""
    monkeypatch.setattr(bedrock, "aws_cli_path", lambda: "")
    with pytest.raises(bedrock.BedrockError, match="aws CLI"):
        bedrock.start_sso_login("p", tmp_path / "logs")


# AWS CLI 2.36이 `--no-browser --use-device-code`로 실제로 찍는 출력(실측).
# 마지막 주소가 코드를 자동 입력해 준다 — AWS가 문구로 그렇게 안내한다.
_DEVICE_OUTPUT = """Browser will not be automatically opened.
Please visit the following URL:

https://d-9b67717466.awsapps.com/start/#/device

Then enter the code:

GZHT-DLVD

Alternatively, you may visit the following URL which will autofill the code upon loading:
https://d-9b67717466.awsapps.com/start/#/device?user_code=GZHT-DLVD
"""


def _fake_cli(monkeypatch, output: str, wait: int = 2) -> dict:
    """CLI를 흉내내고 실행 인자를 잡아 둔다."""
    seen: dict = {}
    monkeypatch.setattr(bedrock, "aws_cli_path", lambda: "aws.exe")
    monkeypatch.setattr(bedrock, "LOGIN_WAIT_SECONDS", wait)

    class _Popen:
        def __init__(self, argv, **kw):
            seen["argv"] = argv
            kw["stdout"].write(output)
            kw["stdout"].flush()

    monkeypatch.setattr(bedrock.subprocess, "Popen", _Popen)
    return seen


def test_sso_login_uses_the_device_code_grant(monkeypatch, tmp_path):
    """기본값(authorization code + PKCE)은 서버에서 쓸 수 없다.

    실측: `redirect_uri=http://127.0.0.1:{포트}/oauth/callback`로 돌아오는데 그 리스너는
    CLI가 도는 **서버**에 있다. 사용자가 자기 브라우저로 그 주소를 열면 승인 후 자기 PC의
    127.0.0.1로 리다이렉트되어 로그인이 영원히 끝나지 않는다. device code 방식은 CLI가
    AWS에 폴링하므로 어느 기계 브라우저에서 승인해도 된다.
    """
    seen = _fake_cli(monkeypatch, _DEVICE_OUTPUT)
    bedrock.start_sso_login("bedrock-dev", tmp_path / "logs")
    assert "--use-device-code" in seen["argv"]
    assert "--no-browser" in seen["argv"]


def test_sso_login_prefers_the_url_that_autofills_the_code(monkeypatch, tmp_path):
    """코드 입력 단계는 없앨 수 있다 — AWS가 코드를 박은 주소를 함께 준다.

    맨 주소가 먼저 찍히므로 첫 URL을 잡으면 사람이 코드를 옮겨 적어야 한다. 남는 사람
    동작을 '허용' 클릭 하나로 줄이는 것이 이 선택의 목적이다.
    """
    _fake_cli(monkeypatch, _DEVICE_OUTPUT)
    out = bedrock.start_sso_login("bedrock-dev", tmp_path / "logs")
    assert out["verification_url"] == (
        "https://d-9b67717466.awsapps.com/start/#/device?user_code=GZHT-DLVD")
    assert out["code_autofilled"] is True
    assert out["user_code"] == "GZHT-DLVD"  # 주소가 막혔을 때 손으로 넣을 수 있게 남긴다
    assert out["profile"] == "bedrock-dev"


def test_sso_login_falls_back_to_the_plain_url(monkeypatch, tmp_path):
    """자동 입력 주소를 주지 않는 버전도 있다 — 그때는 맨 주소와 코드를 그대로 보여 준다."""
    _fake_cli(monkeypatch, (
        "Please visit the following URL:\n"
        "https://device.sso.ap-northeast-2.amazonaws.com/\n"
        "Then enter the code:\n\nWXYZ-ABCD\n"), wait=1)
    out = bedrock.start_sso_login("bedrock-dev", tmp_path / "logs")
    assert out["verification_url"] == "https://device.sso.ap-northeast-2.amazonaws.com/"
    assert out["code_autofilled"] is False
    assert out["user_code"] == "WXYZ-ABCD"


def test_sso_login_shows_the_log_when_it_cannot_parse(monkeypatch, tmp_path):
    """CLI 문구가 바뀌거나 오류면 주소를 못 뽑는다 — 판정 불가를 감추면 볼 것이 없다."""
    monkeypatch.setattr(bedrock, "aws_cli_path", lambda: "aws.exe")
    monkeypatch.setattr(bedrock, "LOGIN_WAIT_SECONDS", 1)

    class _Popen:
        def __init__(self, argv, **kw):
            kw["stdout"].write("Error loading SSO configuration: profile not found\n")
            kw["stdout"].flush()

    monkeypatch.setattr(bedrock.subprocess, "Popen", _Popen)
    out = bedrock.start_sso_login("nope", tmp_path / "logs")
    assert out["verification_url"] == ""
    assert "profile not found" in out["log_tail"]


def test_login_endpoint_is_admin_only(aws_config, monkeypatch):
    monkeypatch.setattr(bedrock, "start_sso_login", lambda profile, log_dir: {
        "profile": profile, "verification_url": "https://x/", "user_code": "AAAA-BBBB",
        "log_path": "", "log_tail": "",
    })
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/llm/aws/login?profile=bedrock-dev", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["user_code"] == "AAAA-BBBB"
    assert c.post("/paas/api/v1/llm/aws/login?profile=bedrock-dev").status_code in (401, 403)


def test_converse_sends_the_output_token_limit(monkeypatch, fresh_settings):
    """안 주면 모델 기본값이 적용되고, 그 값이 작으면 산출물이 문장 중간에서 잘린다."""
    from app.config import get_settings

    monkeypatch.setenv("PAAS_LLM_MAX_OUTPUT_TOKENS", "4096")
    get_settings.cache_clear()
    monkeypatch.setattr(bedrock, "botocore_available", lambda: True)
    monkeypatch.setattr(bedrock, "_frozen", lambda profile: object())
    seen = {}

    def fake_post(url, payload, frozen, region, timeout):
        seen.update(json.loads(payload))
        return {"output": {"message": {"content": [{"text": "ok"}]}}, "stopReason": "end_turn"}

    monkeypatch.setattr(bedrock, "_post_signed", fake_post)
    bedrock.converse(profile="p", region="us-east-1", model_id="m",
                     messages=[{"role": "user", "content": "x"}])
    assert seen["inferenceConfig"] == {"maxTokens": 4096}


def test_converse_reports_truncation_as_finish_reason(monkeypatch, fresh_settings):
    """잘림 판정을 호출부 한 곳(llm.chat_completion)에서 하도록 stopReason을 옮긴다."""
    monkeypatch.setattr(bedrock, "botocore_available", lambda: True)
    monkeypatch.setattr(bedrock, "_frozen", lambda profile: object())
    monkeypatch.setattr(bedrock, "_post_signed", lambda *a: {
        "output": {"message": {"content": [{"text": "중간에서"}]}}, "stopReason": "max_tokens"})
    out = bedrock.converse(profile="p", region="us-east-1", model_id="m",
                           messages=[{"role": "user", "content": "x"}])
    assert out["choices"][0]["finish_reason"] == "max_tokens"


def test_truncated_bedrock_reply_surfaces_through_chat_completion(monkeypatch, fresh_settings):
    """Bedrock 경로도 OpenAI 경로와 같은 예외로 드러나야 한다 — 화면이 하나의 문구를 쓴다."""
    provider = LlmProvider(name="bd", kind=LlmProviderKind.aws, model="m",
                           base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
                           aws_profile="bedrock-dev")
    monkeypatch.setattr(bedrock, "converse", lambda **kw: {
        "choices": [{"message": {"content": "부분"}, "finish_reason": "max_tokens"}]})
    with pytest.raises(llm_service.LlmTruncated) as cut:
        llm_service.chat_completion(provider, [{"role": "user", "content": "x"}])
    assert cut.value.partial == "부분"
