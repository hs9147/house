"""AWS Bedrock — API 키가 아니라 AWS 자격증명(SigV4)으로 Converse API를 호출한다.

Bedrock에는 붙여넣을 수 있는 정적 키가 없다. 사내는 AWS SSO를 쓰므로 자격증명이 토큰
캐시(~/.aws/sso/cache)에 있고 보통 8시간이면 만료된다 — 만료되면 서버에서
`aws sso login --profile <이름>`으로 다시 받아야 한다. 그래서 실패를 "인증 오류"로
뭉개지 않고 어떤 프로필이 왜 안 되는지와 재로그인 명령까지 붙여 말한다.

botocore는 **선택 의존성**이다. Bedrock을 쓰지 않는 설치본에 boto 생태계를 강제하지
않는다(psutil·pywinpty와 같은 방식). 없으면 프로필 목록은 ~/.aws/config에서 그대로
읽어 보여 주고, 자격증명 확인과 서명만 '설치 필요'로 떨어진다.

응답은 OpenAI 모양(`{"choices": [{"message": ...}]}`)으로 되돌린다 — 호출부(llm.py)의
tool-call 루프를 Bedrock용으로 다시 쓰지 않기 위한 경계다.
"""
import configparser
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import httpx

DEFAULT_REGION = "us-east-1"
# bedrock-runtime.us-east-1.amazonaws.com → us-east-1
_REGION_IN_HOST = re.compile(r"bedrock[a-z-]*\.([a-z]{2}-[a-z]+-\d+)\.", re.IGNORECASE)


class BedrockError(RuntimeError):
    """자격증명·서명 단계의 실패. 메시지는 화면에 그대로 뜨는 것을 전제로 쓴다."""


def _config_path() -> Path:
    # AWS CLI와 같은 규칙 — 서비스 계정으로 돌 때 프로필 위치를 지정할 수 있어야 한다.
    env = os.environ.get("AWS_CONFIG_FILE")
    return Path(env) if env else Path.home() / ".aws" / "config"


def _credentials_path() -> Path:
    env = os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
    return Path(env) if env else Path.home() / ".aws" / "credentials"


def botocore_available() -> bool:
    try:
        import botocore.auth  # noqa: F401, PLC0415
        import botocore.session  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def list_profiles() -> list[dict]:
    """~/.aws의 프로필 목록. botocore 없이도 읽힌다 — 화면에서 고를 수 있어야 하므로.

    config는 `[profile 이름]`(default만 `[default]`), credentials는 `[이름]`이다.
    sso_session이 달린 프로필은 만료되는 토큰을 쓴다는 표시로 그대로 내보낸다.
    """
    found: dict[str, dict] = {}
    parser = configparser.ConfigParser()
    try:
        parser.read([_config_path(), _credentials_path()], encoding="utf-8")
    except (OSError, configparser.Error):
        return []
    for section in parser.sections():
        if section.startswith("sso-session "):
            continue  # 프로필이 아니라 로그인 세션 정의다
        name = section.removeprefix("profile ").strip() if section.startswith("profile ") else section
        if not name:
            continue
        values = parser[section]
        entry = found.setdefault(name, {"name": name, "region": "", "sso_session": ""})
        entry["region"] = entry["region"] or values.get("region", "")
        entry["sso_session"] = entry["sso_session"] or values.get("sso_session", "")
        if values.get("sso_start_url"):
            entry["sso_session"] = entry["sso_session"] or "(legacy sso)"
    return sorted(found.values(), key=lambda p: p["name"])


def login_command(profile: str) -> str:
    return f"aws sso login --profile {profile}"


def profile_status(profile: str) -> dict:
    """지금 이 프로필로 호출이 되는지. 안 되면 무엇을 해야 하는지까지 돌려준다.

    ok=None은 '판정 불가'다(botocore 없음) — 되는 것처럼도, 안 되는 것처럼도 말하지 않는다.
    """
    result = {
        "profile": profile, "ok": None, "reason": "", "expires_at": None,
        "login_command": login_command(profile),
    }
    if not botocore_available():
        result["reason"] = "botocore가 설치되지 않아 자격증명을 확인할 수 없습니다 (pip install botocore)"
        return result
    try:
        creds = _credentials(profile)
        # 반드시 확정까지 해 본다. SSO 자격증명은 지연 객체라서 get_credentials()만으로는
        # 토큰을 읽지 않는다 — 만료된 프로필도 ok로 보고하게 된다(실측으로 확인한 함정).
        _freeze(creds, profile)
    except BedrockError as e:
        result["ok"] = False
        result["reason"] = str(e)
        return result
    result["ok"] = True
    # 만료 시각은 갱신형 자격증명(SSO·AssumeRole)만 가진다. 표시용이라 없으면 비운다 —
    # 여기 값이 없어도 동작은 같고, 실제 만료는 위 확정 단계에서 드러난다.
    expiry = getattr(creds, "_expiry_time", None)
    if expiry is not None:
        result["expires_at"] = expiry.isoformat()
    return result


def _credentials(profile: str):
    """botocore로 자격증명 객체를 얻는다. 실패는 사람이 읽을 수 있는 문장으로 바꾼다."""
    import botocore.exceptions as exc  # noqa: PLC0415
    import botocore.session  # noqa: PLC0415

    try:
        session = botocore.session.Session(profile=profile or None)
        creds = session.get_credentials()
    except exc.ProfileNotFound:
        raise BedrockError(f"AWS 프로필 '{profile}'을 찾을 수 없습니다 (~/.aws/config 확인)")
    except (exc.UnauthorizedSSOTokenError, exc.TokenRetrievalError, exc.SSOTokenLoadError):
        raise BedrockError(
            f"AWS SSO 토큰이 만료됐습니다 (프로필 '{profile}'). "
            f"서버에서 `{login_command(profile)}`로 재로그인하세요."
        )
    except exc.BotoCoreError as e:
        raise BedrockError(f"AWS 자격증명을 읽지 못했습니다 (프로필 '{profile}'): {e}")
    if creds is None:
        raise BedrockError(
            f"AWS 프로필 '{profile}'에 자격증명이 없습니다. "
            f"SSO 프로필이면 `{login_command(profile)}`로 로그인하세요."
        )
    return creds


def _frozen(profile: str):
    """서명에 쓸 확정된 키."""
    return _freeze(_credentials(profile), profile)


def _freeze(creds, profile: str):
    """지연 자격증명을 실제 키로 확정한다 — 만료된 SSO는 여기서 드러난다."""
    import botocore.exceptions as exc  # noqa: PLC0415

    try:
        return creds.get_frozen_credentials()
    except (exc.UnauthorizedSSOTokenError, exc.TokenRetrievalError, exc.SSOTokenLoadError):
        raise BedrockError(
            f"AWS SSO 토큰이 만료됐습니다 (프로필 '{profile}'). "
            f"서버에서 `{login_command(profile)}`로 재로그인하세요."
        )
    except exc.BotoCoreError as e:
        raise BedrockError(f"AWS 자격증명 갱신에 실패했습니다 (프로필 '{profile}'): {e}")


def region_from_url(base_url: str, profile: str = "") -> str:
    """리전은 요청 URL과 서명 양쪽에 들어가므로 한 곳에서 정한다.

    등록된 Endpoint 호스트에 리전이 박혀 있으면 그것이 가장 구체적인 의사표시다.
    없으면 프로필 설정, 그다음 환경변수, 마지막으로 기본값.
    """
    match = _REGION_IN_HOST.search(base_url or "")
    if match:
        return match.group(1).lower()
    for entry in list_profiles():
        if entry["name"] == profile and entry["region"]:
            return entry["region"]
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def _to_converse(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """OpenAI 메시지 → Converse. system은 별도 필드이고, 도구 결과는 user 쪽에 실린다."""
    system: list[dict] = []
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system.append({"text": m.get("content") or ""})
            continue
        if role == "tool":
            block = {"toolResult": {
                "toolUseId": m.get("tool_call_id") or "",
                "content": [{"text": m.get("content") or ""}],
            }}
            # Bedrock은 역할이 번갈아야 한다 — 연달아 온 도구 결과는 한 메시지로 묶는다.
            if out and out[-1]["role"] == "user" and "toolResult" in out[-1]["content"][0]:
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        content: list[dict] = []
        if m.get("content"):
            content.append({"text": m["content"]})
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            content.append({"toolUse": {
                "toolUseId": tc.get("id") or "", "name": fn.get("name") or "", "input": arguments,
            }})
        if not content:
            continue  # 빈 content는 Bedrock이 거부한다
        out.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    return system, out


def _from_converse(data: dict) -> dict:
    """Converse 응답 → OpenAI 모양. 호출부의 tool-call 루프를 그대로 재사용하기 위한 변환."""
    message = (data.get("output") or {}).get("message") or {}
    texts: list[str] = []
    calls: list[dict] = []
    for block in message.get("content") or []:
        if block.get("text"):
            texts.append(block["text"])
        use = block.get("toolUse")
        if use:
            calls.append({
                "id": use.get("toolUseId") or "",
                "type": "function",
                "function": {
                    "name": use.get("name") or "",
                    "arguments": json.dumps(use.get("input") or {}, ensure_ascii=False),
                },
            })
    reply: dict = {"role": "assistant", "content": "\n".join(texts)}
    if calls:
        reply["tool_calls"] = calls
    return {"choices": [{"message": reply}]}


def _tool_config(tools: list[dict]) -> dict:
    specs = []
    for t in tools:
        fn = t.get("function") or t
        specs.append({"toolSpec": {
            "name": fn.get("name") or "",
            "description": fn.get("description") or "",
            "inputSchema": {"json": fn.get("parameters") or {"type": "object", "properties": {}}},
        }})
    return {"tools": specs}


def converse(
    *,
    profile: str,
    region: str,
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    timeout: int = 120,
) -> dict:
    """Bedrock Converse 한 번 호출. 반환값은 OpenAI 모양이다."""
    if not botocore_available():
        raise BedrockError(
            "AWS 자격증명으로 Bedrock을 호출하려면 botocore가 필요합니다 "
            "(서버에서 `pip install botocore` 후 백엔드 재시작)."
        )
    frozen = _frozen(profile)
    system, converse_messages = _to_converse(messages)
    body: dict = {"messages": converse_messages}
    if system:
        body["system"] = system
    if tools:
        body["toolConfig"] = _tool_config(tools)
    # 모델 ID에는 `.`과 `:`이 들어간다(anthropic.claude-sonnet-4:0) — 경로 조각으로 인코딩한다.
    url = f"https://bedrock-runtime.{region}.amazonaws.com/model/{quote(model_id, safe='')}/converse"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return _from_converse(_post_signed(url, payload, frozen, region, timeout))


def _post_signed(url: str, payload: bytes, frozen, region: str, timeout: int) -> dict:
    """서명 + 실제 HTTP 경계 (테스트에서 monkeypatch하는 지점)."""
    from botocore.auth import SigV4Auth  # noqa: PLC0415
    from botocore.awsrequest import AWSRequest  # noqa: PLC0415

    request = AWSRequest(method="POST", url=url, data=payload,
                         headers={"content-type": "application/json"})
    SigV4Auth(frozen, "bedrock", region).add_auth(request)
    res = httpx.post(url, headers=dict(request.headers), content=payload, timeout=timeout)
    if res.status_code >= 400:
        # 본문에 이유가 들어 있다(만료·권한 없음·모델 접근 미승인·리전에 없는 모델).
        # raise_for_status는 그 본문을 버려서 "400 Bad Request"만 남긴다 — 실측에서
        # 원인을 못 짚게 만든 자리라 여기서 본문을 그대로 싣는다.
        raise BedrockError(f"Bedrock 호출이 거부됐습니다 ({res.status_code}): {res.text[:800]}")
    return res.json()
