import os
import secrets
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from .api import (
    a2a_gateway, llm, modules, oidc_provider, orgs, planning, previews, projects, proxy_gateway, server, storage,
    system, webhooks,
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


def create_app() -> FastAPI:
    settings = get_settings()
    Base.metadata.create_all(engine)

    if not settings.admin_api_key:
        # 부트스트랩 편의용 — 운영에서는 PAAS_ADMIN_API_KEY를 고정할 것
        settings.admin_api_key = "paas_" + secrets.token_urlsafe(24)
        print(f"[paas] bootstrap admin key (set PAAS_ADMIN_API_KEY to pin): {settings.admin_api_key}")

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
            print(f"[paas] OIDC Provider 활성화 — issuer={_oidc_issuer()} "
                  f"authorize={_oidc_browser()}/oauth2/authorize")
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
