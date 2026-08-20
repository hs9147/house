"""아웃바운드 검증 — 이 모듈로 나가는 플랫폼 호출에 내부 정보가 실리는지 본다.

플랫폼이 모듈 주소로 직접 나가는 곳은 둘이다:
  - A2A 게이트웨이 (services/a2a.relay_task)
  - 모듈 프록시   (api/proxy_gateway.proxy_module_call)

여기서 판정하는 것은 **그 두 경로가 보내는 것**뿐이다. 배포된 앱은 바인딩으로
`{PREFIX}_URL`을 받아 직접 부르기도 하므로, 앱이 무엇을 보내는지는 이 검사로 알 수 없다 —
화면 배지 문구도 딱 그 범위로 적는다. 없는 보증을 배지로 주면 안 된다.

판정 결과는 저장하지 않고 볼 때마다 계산한다. 주소나 설정을 바꾸면 즉시 반영돼야 하는데,
플래그로 굳혀 두면 바뀐 뒤에도 "검증됨"이 남는다.
"""
import ipaddress
from urllib.parse import parse_qsl, urlsplit

from ..config import get_settings
from ..models import Module, ModuleType
from .modules import decrypt_config

# 프록시가 대상에게 그대로 넘겨도 되는 요청 헤더. **허용 목록이어야 한다** — 들어온 헤더를
# 통째로 넘기면 호출자의 x-api-key·cookie·authorization(= 플랫폼 자격증명)이 외부 대상에게
# 그대로 나간다.
SAFE_FORWARD_HEADERS = frozenset({
    "accept", "accept-language", "content-type", "if-none-match", "if-modified-since",
})

# 사내 DNS에서 흔한 접미사. 단일 라벨 호스트(점이 없는 이름)도 사내로 본다.
_INTERNAL_SUFFIXES = (".local", ".internal", ".lan", ".intranet", ".corp", ".home.arpa")

# URL 쿼리에 이런 이름이 있으면 자격증명이 주소에 박혀 있는 것으로 본다.
_SECRET_PARAMS = ("key", "token", "secret", "password", "passwd", "pwd", "apikey", "access_token")


def host_of(url: str) -> str:
    if not url:
        return ""
    parsed = urlsplit(url if "://" in url else f"//{url}")
    return (parsed.hostname or "").lower()


def _platform_hosts(settings) -> set[str]:
    """플랫폼 자신·사내 구성요소의 호스트 — 여기로 가는 것은 망을 벗어나지 않는다."""
    candidates = [
        settings.gitea_url, settings.platform_public_url,
        settings.oidc_provider_backchannel_url, f"//{settings.base_domain}",
    ]
    hosts = {host_of(value) for value in candidates if value}
    hosts.discard("")
    return hosts


def is_internal_host(host: str, settings=None) -> bool:
    settings = settings or get_settings()
    host = (host or "").lower()
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return bool(address.is_private or address.is_loopback or address.is_link_local
                    or address.is_reserved)

    if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        return True
    if "." not in host:  # 단일 라벨 — 사내 DNS로만 풀리는 이름
        return True
    if host in _platform_hosts(settings):
        return True
    suffixes = [s.strip().lower().lstrip(".") for s in settings.internal_domains.split(",")]
    return any(suffix and (host == suffix or host.endswith(f".{suffix}")) for suffix in suffixes)


def is_internal_url(url: str, settings=None) -> bool:
    return is_internal_host(host_of(url), settings)


def forward_headers(incoming) -> dict[str, str]:
    """프록시가 대상에게 넘길 헤더만 남긴다(허용 목록)."""
    return {
        name: value for name, value in dict(incoming).items()
        if name.lower() in SAFE_FORWARD_HEADERS
    }


def _target_url(module: Module) -> str:
    cfg = decrypt_config(module.config or {})
    return str(cfg.get("url") or cfg.get("endpoint") or "")


def inspect_module(module: Module, settings=None) -> dict:
    """이 모듈로 나가는 플랫폼 호출을 점검한다.

    scope
      local    — 주소로 나가지 않는다(파일 저장소·DB 등). 나갈 것이 없으니 유출도 없다.
      internal — 사내 주소. 망을 벗어나지 않는다.
      external — 사외 주소. 무엇이 실려 나가는지가 문제가 된다.
      unknown  — 주소가 없거나 해석되지 않는다.
    """
    settings = settings or get_settings()
    url = _target_url(module)

    if module.type in (ModuleType.database, ModuleType.file_storage):
        return {"scope": "local", "host": None, "secured": True, "findings": [],
                "platform_sends": []}
    if not url:
        return {"scope": "unknown", "host": None, "secured": False,
                "findings": ["주소가 설정되어 있지 않습니다."], "platform_sends": []}

    parsed = urlsplit(url)
    host = host_of(url)
    if not host:
        return {"scope": "unknown", "host": None, "secured": False,
                "findings": [f"주소를 해석할 수 없습니다: {url}"], "platform_sends": []}

    internal = is_internal_host(host, settings)
    findings: list[str] = []
    if parsed.username or parsed.password:
        findings.append("주소에 자격증명이 박혀 있습니다(user:pass@) — 로그·설정에 그대로 남습니다.")
    query_names = {name.lower() for name, _ in parse_qsl(parsed.query)}
    leaked = sorted(n for n in query_names if any(s in n for s in _SECRET_PARAMS))
    if leaked:
        findings.append(f"주소 쿼리에 자격증명으로 보이는 값이 있습니다: {', '.join(leaked)}")
    if not internal and parsed.scheme == "http":
        findings.append("사외 주소인데 http입니다 — 자격증명과 본문이 평문으로 나갑니다.")

    # 게이트웨이가 붙이는 것. 사내 대상에만 호출자 신원을 싣는다(services/a2a.relay_task).
    platform_sends = ["authorization(대상 자격증명)"] if _has_credential(module) else []
    if internal:
        platform_sends += ["x-paas-calling-agent(호출자 신원)", "x-paas-a2a-gateway"]

    return {
        "scope": "internal" if internal else "external",
        "host": host,
        "secured": not findings,
        "findings": findings,
        "platform_sends": platform_sends,
    }


def _has_credential(module: Module) -> bool:
    cfg = decrypt_config(module.config or {})
    return bool(cfg.get("api_key") or cfg.get("secret_key"))
