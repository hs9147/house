import os
import secrets
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from .api import (
    a2a_gateway, llm, mcp_servers, modules, oidc_provider, orgs, planning, previews, projects,
    proxy_gateway, server, storage, system, webhooks,
)
from .config import get_settings
from .db import Base, engine
from .features import enabled_features

# 모든 엔드포인트(health/status 포함)를 이 서비스 이름 아래 묶는 공통 prefix —
# 여러 내부 서비스가 같은 게이트웨이/도메인을 공유할 때 경로로 구분하기 위함.
PAAS_PREFIX = "/paas"
# 버전 prefix. health/status는 PAAS_PREFIX만 받고 버전은 안 받는다(로드밸런서/k8s probe·
# 콘솔 로그인 프로브가 버전과 무관하게 고정 경로를 기대함) — system.health_router 참고.
# webhooks도 버전을 안 받는다: 외부(Gitea/GitHub)가 한 번 등록해두는 콜백 URL이라
# API 버전이 올라가도 안 깨지는 게 안전 — services/gitea.py의 ensure_webhook과 맞출 것.
API_PREFIX = f"{PAAS_PREFIX}/api/v1"


def _make_console_output_safe() -> None:
    """기동 로그·트레이스백이 콘솔 인코딩 때문에 죽지 않게 한다.

    Windows의 기본 출력 인코딩(한국어판은 cp949, nssm이 로그로 리다이렉트하면 cp1252나
    ascii가 되기도 한다)에서는 한글은 물론 em dash("—") 하나도 UnicodeEncodeError를
    낸다. 그게 기동 경로에서 터지면 create_app이 예외로 끝나 **플랫폼 전체가 안 뜨고**,
    리버스프록시 쪽에서는 전 경로 502로만 보인다 — 로그 한 줄 때문에 서비스가 죽는
    셈이다. 게다가 설정 오류 메시지(config.SettingsError)도 한글이라, 진짜 원인이
    트레이스백 출력 도중 또 다른 인코딩 오류에 가려진다.

    errors="replace"가 핵심이다(인코딩을 못 하면 죽는 대신 대체 문자로 흘린다).
    utf-8로 맞추는 것은 nssm 로그 파일을 나중에 열었을 때 한글이 그대로 읽히게 하기
    위함이다. 캡처된 스트림(pytest 등) 등 reconfigure를 지원하지 않으면 그냥 넘어간다.
    """
    import sys  # noqa: PLC0415

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — 출력 보호가 기동을 막으면 본말전도다
            pass


def create_app() -> FastAPI:
    # get_settings()보다 먼저 — 설정 오류 메시지도 한글이라 여기서 보호돼야 한다.
    _make_console_output_safe()
    settings = get_settings()
    Base.metadata.create_all(engine)

    if not settings.admin_api_key:
        # 부트스트랩 편의용 — 운영에서는 PAAS_ADMIN_API_KEY를 고정할 것
        settings.admin_api_key = "paas_" + secrets.token_urlsafe(24)
        print(f"[paas] bootstrap admin key (set PAAS_ADMIN_API_KEY to pin): {settings.admin_api_key}")

    # WebSocket 구현이 없으면 콘솔 터미널만 조용히 죽는다 — HTTP는 전부 정상이라
    # 밖에서는 프록시 문제와 구분되지 않고, 서버 로그를 봐야만 알 수 있다. 기동할 때
    # 미리 말해 두면 "왜 터미널이 안 열리지"를 서버 로그에서 바로 찾을 수 있다.
    from .services.pty_terminal import websocket_library  # noqa: PLC0415

    if not websocket_library():
        print("[paas] WARNING: WebSocket 구현이 없습니다(websockets·wsproto 모두 없음) — "
              "콘솔 터미널이 열리지 않습니다(소켓만 404). "
              'pip install "uvicorn[standard]" 후 재시작하세요.')

    app = FastAPI(
        title="house",
        description=(
            "내부 PaaS 컨트롤 플레인. "
            f"tier={settings.tier} (small=Docker, enterprise=Kubernetes), "
            "빌드 프로필=development|release"
        ),
        version="0.1.0",
    )
    features = enabled_features()

    # core — 항상 켜짐 (projects 안의 배포 계열 엔드포인트는 require_feature("deploy")로 게이트)
    app.include_router(system.health_router, prefix=PAAS_PREFIX)  # /paas/health, /paas/status
    app.include_router(system.router, prefix=API_PREFIX)
    app.include_router(projects.router, prefix=API_PREFIX)
    app.include_router(orgs.router, prefix=API_PREFIX)
    app.include_router(modules.router, prefix=API_PREFIX)
    app.include_router(proxy_gateway.router, prefix=API_PREFIX)
    app.include_router(a2a_gateway.router, prefix=API_PREFIX)
    app.include_router(storage.router, prefix=API_PREFIX)
    # 사내 MCP 서버 — 엔드포인트별로 필요한 기능만 게이트한다(ops=deploy, code=workspace).
    app.include_router(mcp_servers.router, prefix=API_PREFIX)
    if settings.oidc_provider_enabled:  # /paas/.well-known/openid-configuration, /paas/oauth2/*
        app.include_router(oidc_provider.router, prefix=PAAS_PREFIX)
        # 발급자 주소는 클라이언트(Gitea 등)가 조회한 URL과 정확히 같아야 하고, 그
        # 호스트명은 서버 인증서에 들어 있는 이름이어야 한다. 설정이 아예 없으면 첫 SSO
        # 시도까지 기다리지 말고 지금 알린다 — 기동 자체를 막지는 않는다(SSO 설정 하나로
        # 플랫폼 전체를 못 뜨게 할 이유는 없다).
        from .services.oidc_provider import OidcProviderError  # noqa: PLC0415
        from .services.oidc_provider import browser_base as _oidc_browser  # noqa: PLC0415
        from .services.oidc_provider import issuer as _oidc_issuer  # noqa: PLC0415

        try:
            # 브라우저용 주소도 함께 확인한다 — 백채널만 설정하고 공개 주소를 빠뜨리면
            # 로그인 화면 주소가 내부 주소로 나가 사용자가 열 수 없다.
            resolved_issuer = _oidc_issuer()
            print(f"[paas] OIDC Provider 활성화 — issuer={resolved_issuer} "
                  f"authorize={_oidc_browser()}/oauth2/authorize")
            # 백채널이 설정되면 발급자 주소는 그 값이 되고 PAAS_OIDC_ISSUER는 내장
            # Provider에 관여하지 않는다(외부 발급자 신뢰용으로만 남는다). 이걸 모르고
            # 둘을 다르게 두면, 클라이언트에는 PAAS_OIDC_ISSUER 주소로 등록해 놓고 실제
            # 문서에는 백채널 주소가 실려 나가 "issuer did not match"로 거부된다.
            if (settings.oidc_provider_backchannel_url and settings.oidc_issuer
                    and settings.oidc_issuer.rstrip("/") != resolved_issuer):
                print(
                    "[paas] 주의: PAAS_OIDC_ISSUER"
                    f"({settings.oidc_issuer})는 내장 Provider의 발급자 주소로 쓰이지 "
                    "않습니다 — 백채널 주소가 우선합니다. 클라이언트(Gitea 등)에는 위 "
                    "issuer 주소로 등록하세요(다르면 issuer 불일치로 거부됩니다). "
                    "이 값은 외부 발급자를 함께 신뢰할 때만 의미가 있습니다."
                )
        except OidcProviderError as e:
            print(f"[paas] OIDC Provider 설정 오류: {e}")

    # 1일 1회 외부 API 수집 루트 백그라운드 탐색 및 갱신 스케줄러 시동
    from .services.apisearch import start_daily_api_directory_scheduler  # noqa: PLC0415
    start_daily_api_directory_scheduler()

    # 선택 모듈 (설치 빌드옵션)
    if "deploy" in features:
        app.include_router(webhooks.router, prefix=PAAS_PREFIX)  # /paas/webhooks/git — 버전 없음
        app.include_router(previews.router, prefix=API_PREFIX)
        app.include_router(server.router, prefix=API_PREFIX)
        # 콘솔 자기 배포(옵트인, PAAS_SELF_DEPLOY_CONSOLE) — services/self_deploy.py 참고
        from .services.self_deploy import bootstrap_console_deploy  # noqa: PLC0415

        bootstrap_console_deploy()
    if "workspace" in features:
        app.include_router(llm.router, prefix=API_PREFIX)
        app.include_router(planning.router, prefix=API_PREFIX)

    # 콘솔 UI(React 빌드 산출물) — dist가 있을 때만 마운트, 없어도 API는 동일 기동
    console_dist = Path(
        os.environ.get("PAAS_CONSOLE_DIST")
        or Path(__file__).resolve().parents[1] / "console" / "dist"
    )
    if console_dist.is_dir():
        app.mount("/console", StaticFiles(directory=console_dist, html=True), name="console")

    # 이 프로세스의 PowerShell 브로커 연결을 종료 시 닫는다 — 브로커·그 안의 세션
    # 자체는 건드리지 않는다(paas가 죽어도 살아있어야 하므로). services/ps_broker.py 참고.
    import atexit  # noqa: PLC0415

    from .services.powershell_daemon import shutdown_shared  # noqa: PLC0415

    atexit.register(shutdown_shared)

    return app


app = create_app()
