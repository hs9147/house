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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from ..config import get_settings

DEFAULT_REGION = "us-east-1"
# bedrock-runtime.us-east-1.amazonaws.com → us-east-1
_REGION_IN_HOST = re.compile(r"bedrock[a-z-]*\.([a-z]{2}-[a-z]+-\d+)\.", re.IGNORECASE)


class BedrockError(RuntimeError):
    """자격증명·서명 단계의 실패. 메시지는 화면에 그대로 뜨는 것을 전제로 쓴다."""


def config_path() -> Path:
    # AWS CLI와 같은 규칙 — 서비스 계정으로 돌 때 프로필 위치를 지정할 수 있어야 한다.
    env = os.environ.get("AWS_CONFIG_FILE")
    return Path(env) if env else Path.home() / ".aws" / "config"


def _credentials_path() -> Path:
    env = os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
    return Path(env) if env else Path.home() / ".aws" / "credentials"


def config_state() -> dict:
    """프로필 목록이 비었을 때 **왜** 비었는지 가릴 수 있는 사실들.

    "프로필이 없습니다"만으로는 파일이 없는 것(서비스 계정 홈을 보고 있다)과 파일은 있는데
    프로필 섹션이 없는 것(로그인은 했지만 설정이 다르다)이 구분되지 않는다. 둘의 대처가
    다르므로 갈라서 보여 준다.
    """
    config, credentials = config_path(), _credentials_path()
    return {
        "config_path": str(config),
        "config_exists": config.is_file(),
        "credentials_exists": credentials.is_file(),
        # **어느 파이썬이 찾고 있는지.** "botocore가 없다"는 보고를 받고 venv에 설치했는데도
        # 그대로인 경우가 있다 — 서비스가 다른 인터프리터로 돌고 있으면 그 venv의
        # site-packages를 보지 않는다. 설치할 대상을 이 값으로 특정한다.
        "python": sys.executable,
        "botocore_available": botocore_available(),
    }


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
        parser.read([config_path(), _credentials_path()], encoding="utf-8")
    except (OSError, configparser.Error):
        return []
    for section in parser.sections():
        if section.startswith("sso-session "):
            continue  # 프로필이 아니라 로그인 세션 정의다
        name = section.removeprefix("profile ").strip() if section.startswith("profile ") else section
        if not name:
            continue
        values = parser[section]
        entry = found.setdefault(
            name, {"name": name, "region": "", "sso_session": "", "sso_start_url": ""})
        entry["region"] = entry["region"] or values.get("region", "")
        # sso_session(신규)과 sso_start_url(구형) — 둘 중 하나가 SSO 토큰 캐시를 찾는 열쇠다.
        entry["sso_session"] = entry["sso_session"] or values.get("sso_session", "")
        entry["sso_start_url"] = entry["sso_start_url"] or values.get("sso_start_url", "")
    return sorted(found.values(), key=lambda p: p["name"])


def login_command(profile: str) -> str:
    return f"aws sso login --profile {profile}"


def profile_status(profile: str) -> dict:
    """지금 이 프로필로 호출이 되는지. 안 되면 무엇을 해야 하는지까지 돌려준다.

    ok=None은 '판정 불가'다(botocore 없음) — 되는 것처럼도, 안 되는 것처럼도 말하지 않는다.
    """
    result = {
        "profile": profile, "ok": None, "reason": "",
        # ok와 무관하게 싣는다 — 만료됐을 때가 오히려 "언제 끊겼는지"를 알아야 할 때다.
        "expires_at": sso_token_expiry(profile) or None,
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
    if not result["expires_at"]:
        # SSO가 아니면(정적 키·AssumeRole) 자격증명 자체의 만료로 떨어진다. 표시용이라
        # 없으면 비운다 — 동작은 같고, 진짜 만료는 위 확정 단계에서 드러난다.
        expiry = getattr(creds, "_expiry_time", None)
        if expiry is not None:
            result["expires_at"] = expiry.isoformat()
    return result


def sso_token_expiry(profile: str) -> str:
    """SSO 액세스 토큰의 만료 시각(ISO 문자열). SSO 프로필이 아니거나 못 읽으면 빈 문자열.

    **자격증명 만료와 다른 값이다.** 실측에서 토큰은 08:15Z에, 그 토큰으로 받은 STS
    자격증명은 19:25Z에 만료였다 — 재로그인이 필요해지는 시각은 앞쪽이다. 긴 쪽을 보여
    주면 이미 재로그인이 필요해진 뒤에도 여유가 있는 것처럼 읽힌다.

    캐시 파일 이름은 sso_session 이름(구형은 start_url)의 sha1이다. botocore의
    SSOTokenLoader를 쓰려면 파일 캐시를 직접 주입해야 하는데(기본값이 빈 dict라
    "Token does not exist"가 된다) 그 경로는 사설 상수다 — 규칙이 한 줄이라 직접 읽는다.
    그래서 botocore가 없어도 만료 시각은 보인다.
    """
    entry = next((p for p in list_profiles() if p["name"] == profile), None)
    if entry is None:
        return ""
    key = entry.get("sso_session") or entry.get("sso_start_url") or ""
    if not key:
        return ""
    cache = Path.home() / ".aws" / "sso" / "cache" / f"{hashlib.sha1(key.encode()).hexdigest()}.json"
    try:
        token = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""  # 로그인 전이거나 규칙이 바뀌었다 — 없는 것으로 둔다
    return str(token.get("expiresAt") or "") if isinstance(token, dict) else ""


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


# `aws sso login` 출력에서 뽑는 것들. 버전마다 문구가 달라지므로 형태로 찾는다.
# **코드가 박힌 URL을 먼저 찾는다.** device code 방식은 세 가지를 찍는데(맨 URL, 코드,
# 코드가 박힌 URL) AWS가 마지막 것을 "which will autofill the code upon loading"이라고
# 안내한다 — 그것을 쓰면 사람이 코드를 옮겨 적는 단계가 아예 없어진다.
_LOGIN_AUTOFILL_RE = re.compile(r"https://\S*user_code=\S+")
_LOGIN_URL_RE = re.compile(r"https://\S+")
_LOGIN_CODE_RE = re.compile(r"\b([A-Z]{4}-[A-Z]{4})\b")
LOGIN_WAIT_SECONDS = 20


def aws_cli_path() -> str:
    """서버의 aws CLI 경로. 없으면 빈 문자열 — device 흐름의 폴링·토큰 저장을 CLI가 한다."""
    return shutil.which("aws") or ""


def start_sso_login(profile: str, log_dir: Path) -> dict:
    """서버에서 SSO 로그인을 시작하고 **승인 한 번으로 끝나는 주소**를 돌려준다.

    **`--use-device-code`가 반드시 필요하다.** 최신 CLI(2.36 실측)의 기본값은 authorization
    code + PKCE라서 `redirect_uri=http://127.0.0.1:{포트}/oauth/callback`로 돌아온다 —
    그 리스너는 **CLI가 도는 기계(서버)** 에 있다. 사용자가 자기 브라우저로 그 주소를 열면
    승인 후 자기 PC의 127.0.0.1로 리다이렉트되어 로그인이 영원히 완료되지 않는다.
    device code 방식은 CLI가 AWS에 폴링해 확인하므로 **어느 기계 브라우저에서 승인해도** 된다.

    **자동화할 수 있는 것과 없는 것.** 코드 입력은 없앨 수 있다(코드가 박힌 URL). 대기·폴링·
    토큰 저장도 자동이다. 남는 것은 브라우저의 '허용' 클릭 하나뿐이고, 그것은 이 방식의
    보안 경계라서 자동화 대상이 아니다.

    토큰은 이 프로세스가 받으므로 **서비스 계정의 홈**에 떨어진다 — "내 계정으로 로그인했는데
    서비스는 그 토큰을 못 본다"는 문제도 같이 풀린다.

    프로세스는 승인을 기다리며 계속 돈다. 우리는 주소를 읽어 내면 바로 돌아오고, 성공 여부는
    호출부가 profile_status를 다시 물어 확인한다. 주소를 못 읽어도 로그 꼬리를 함께 돌려준다 —
    판정 불가를 감추면 사람이 볼 것이 아무것도 없다.
    """
    exe = aws_cli_path()
    if not exe:
        raise BedrockError(
            "서버에 aws CLI가 없어 SSO 로그인을 시작할 수 없습니다 "
            "(AWS CLI v2를 설치하거나 서버에서 직접 `aws sso login`을 실행하세요)."
        )
    argv = [exe, "sso", "login", "--profile", profile, "--no-browser", "--use-device-code"]
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "aws-sso-login.log"
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[paas] {' '.join(argv[1:])}\n")
        log.flush()
        # 콘솔 창을 띄우지 않는다. DETACHED_PROCESS는 쓰지 않는다 — 실측에서 그 플래그를
        # 주면 자식이 조용히 죽었다(services/powershell_daemon.py에 같은 기록이 있다).
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=flags,
        )
    # 주소가 찍힐 때까지 짧게 기다린다 — 바로 읽으면 빈 파일이다. 코드가 박힌 URL은 맨 URL
    # 뒤에 찍히므로, 그것이 나올 때까지는 계속 본다(먼저 멈추면 코드를 옮겨 적게 된다).
    url = code = ""
    autofilled = False
    deadline = time.monotonic() + LOGIN_WAIT_SECONDS
    while time.monotonic() < deadline and not autofilled:
        time.sleep(0.5)
        text = log_path.read_text(encoding="utf-8", errors="replace")
        matched = _LOGIN_CODE_RE.search(text)
        code = matched.group(1) if matched else code
        found = _LOGIN_AUTOFILL_RE.search(text)
        if found:
            autofilled = True
        else:
            found = _LOGIN_URL_RE.search(text)
        if found:
            url = found.group(0).rstrip(".,)")
    return {
        "profile": profile,
        "verification_url": url,
        # 코드가 박힌 주소면 사람이 옮겨 적을 것이 없다 — 화면이 그 차이를 말할 수 있어야 한다.
        "code_autofilled": autofilled,
        "user_code": code,
        "log_path": str(log_path),
        # 주소를 못 뽑았을 때 사람이 볼 것 — 문구가 바뀌었는지, 오류인지 여기서 드러난다.
        "log_tail": log_path.read_text(encoding="utf-8", errors="replace")[-1500:],
    }


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


def runtime_url(region: str) -> str:
    """리전이 정해지면 런타임 주소도 정해진다 — 사람이 적을 값이 아니다."""
    return f"https://bedrock-runtime.{region}.amazonaws.com"


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
    # stopReason을 OpenAI의 finish_reason 자리로 옮긴다 — 잘림 판정을 호출부 한 곳에서
    # 하기 위해서다(llm.chat_completion). Bedrock은 "max_tokens", OpenAI는 "length"다.
    return {"choices": [{"message": reply, "finish_reason": data.get("stopReason") or ""}]}


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
    timeout: int = 0,  # 0이면 설정값(llm_timeout_seconds)
) -> dict:
    """Bedrock Converse 한 번 호출. 반환값은 OpenAI 모양이다."""
    if not botocore_available():
        raise BedrockError(
            "AWS 자격증명으로 Bedrock을 호출하려면 botocore가 필요합니다 "
            "(서버에서 `pip install botocore` 후 백엔드 재시작)."
        )
    frozen = _frozen(profile)
    timeout = timeout or get_settings().llm_timeout_seconds
    system, converse_messages = _to_converse(messages)
    body: dict = {"messages": converse_messages}
    # 출력 한도를 명시한다 — 안 주면 모델 기본값이 적용되고, 그 값이 작으면 산출물이
    # 문장 중간에서 잘린다(OpenAI 경로와 같은 이유·같은 설정).
    limit = get_settings().llm_max_output_tokens
    if limit > 0:
        body["inferenceConfig"] = {"maxTokens": limit}
    if system:
        body["system"] = system
    if tools:
        body["toolConfig"] = _tool_config(tools)
    # 모델 ID에는 `.`과 `:`이 들어간다(anthropic.claude-sonnet-4:0) — 경로 조각으로 인코딩한다.
    url = f"{runtime_url(region)}/model/{quote(model_id, safe='')}/converse"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return _from_converse(_post_signed(url, payload, frozen, region, timeout))


def list_models(*, profile: str, region: str, timeout: int = 20) -> list[dict]:
    """이 자격증명으로 지금 부를 수 있는 모델 후보 — 등록 화면에서 고르기 위한 목록.

    **추론 프로필을 먼저 싣는다.** 실측(ap-northeast-2): foundation-models에 있는 ID를
    그대로 넣으면 "Invocation of model ID ... with on-demand throughput isn't supported.
    Retry with the ID or ARN of an inference profile"로 거부된다. 그러니 실제로 통하는
    ID(global.anthropic.…·apac.anthropic.…)가 목록 앞에 있어야 한다.

    뒤에는 온디맨드로 부를 수 있는 텍스트 모델을 붙인다 — 리전에 따라 그쪽이 되는 곳도 있다.
    임베딩·이미지 모델은 대화에 쓸 수 없으므로 뺀다.
    """
    frozen = _frozen(profile)
    out: list[dict] = []
    profiles = _get_signed(
        f"https://bedrock.{region}.amazonaws.com/inference-profiles?maxResults=100",
        frozen, region, timeout)
    for entry in profiles.get("inferenceProfileSummaries") or []:
        if (entry.get("status") or "ACTIVE") != "ACTIVE":
            continue
        out.append({
            "id": entry.get("inferenceProfileId") or "",
            "name": entry.get("inferenceProfileName") or "",
            "kind": "inference_profile",
        })
    models = _get_signed(
        f"https://bedrock.{region}.amazonaws.com/foundation-models", frozen, region, timeout)
    for m in models.get("modelSummaries") or []:
        if "ON_DEMAND" not in (m.get("inferenceTypesSupported") or []):
            continue
        if "TEXT" not in (m.get("outputModalities") or []):
            continue
        if ((m.get("modelLifecycle") or {}).get("status") or "ACTIVE") != "ACTIVE":
            continue
        out.append({
            "id": m.get("modelId") or "",
            "name": m.get("modelName") or "",
            "kind": "on_demand",
        })
    return [e for e in out if e["id"]]


def _get_signed(url: str, frozen, region: str, timeout: int) -> dict:
    """서명한 GET (컨트롤 플레인 조회 — 추론이 아니라 과금 없음)."""
    return _signed(method="GET", url=url, payload=b"", frozen=frozen,
                   region=region, timeout=timeout)


def _post_signed(url: str, payload: bytes, frozen, region: str, timeout: int) -> dict:
    """서명한 POST (테스트에서 monkeypatch하는 지점)."""
    return _signed(method="POST", url=url, payload=payload, frozen=frozen,
                   region=region, timeout=timeout)


def _signed(*, method: str, url: str, payload: bytes, frozen, region: str, timeout: int) -> dict:
    """서명 + 실제 HTTP 경계."""
    from botocore.auth import SigV4Auth  # noqa: PLC0415
    from botocore.awsrequest import AWSRequest  # noqa: PLC0415

    request = AWSRequest(method=method, url=url, data=payload or None,
                         headers={"content-type": "application/json"})
    SigV4Auth(frozen, "bedrock", region).add_auth(request)
    try:
        res = httpx.request(method, url, headers=dict(request.headers),
                            content=payload or None, timeout=timeout)
    except httpx.TimeoutException:
        # llm.LlmTimeout과 같은 내용을 여기서 다시 쓴다 — llm.py를 import하면 순환이 된다
        # (llm이 bedrock을 쓴다). 문구는 사람이 보는 것이므로 같은 말이어야 한다.
        raise BedrockError(
            f"Bedrock이 {timeout}초 안에 응답하지 않았습니다 "
            f"(최대 출력 {get_settings().llm_max_output_tokens} 토큰). "
            "출력 한도가 크면 생성도 길어집니다 — PAAS_LLM_TIMEOUT_SECONDS를 늘리거나 "
            "PAAS_LLM_MAX_OUTPUT_TOKENS를 줄이거나, 요청 범위를 좁혀 다시 시도하세요."
        )
    if res.status_code >= 400:
        # 본문에 이유가 들어 있다(만료·권한 없음·모델 접근 미승인·리전에 없는 모델).
        # raise_for_status는 그 본문을 버려서 "400 Bad Request"만 남긴다 — 실측에서
        # 원인을 못 짚게 만든 자리라 여기서 본문을 그대로 싣는다.
        raise BedrockError(f"Bedrock 호출이 거부됐습니다 ({res.status_code}): {res.text[:800]}")
    return res.json()
