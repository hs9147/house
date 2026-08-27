"""사내 MCP 서버 디렉터리 — 이 플랫폼이 실제로 노출하는 MCP 서버를 찾아 준다.

예전에는 외부 MCP 서버 목록이었는데, 그 목록에 있던 주소는 어디에도 실재하지 않았고
(가져와 등록한 모듈은 등록 직후부터 죽어 있었다) 널리 쓰이는 공개 MCP 서버는 대부분
stdio 전용이라 URL이라는 개념 자체가 없었다. 그래서 **실재하는 것만 내주도록** 바꿨다 —
플랫폼이 직접 띄우는 사내 MCP 서버(api/mcp_servers.py)다.

목록은 고정 표가 아니라 **지금 있는 것에서 만든다**: 저장소 서버는 환경변수가 정한
저장소마다(PAAS_STORAGE_ROOT · PAAS_DOC_ROOTS), DB 서버는 허용 목록에 있는 모듈만.
없는 대상을 목록에 올리면 예전과 같은 실수를 반복하게 된다.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Module, ModuleType
from . import storage


def internal_base_url() -> str:
    """사내 MCP 서버 주소의 기준. 플랫폼이 **자기 자신에게** 닿는 주소여야 한다.

    공개 도메인이 이 플랫폼으로 라우팅되지 않는 구성이 있어서(서브패스 배포) 브라우저가
    쓰는 주소를 그대로 쓸 수 없다. 백채널 주소가 바로 그런 용도로 이미 있으므로 그것을
    쓰고, 없으면 공개 주소로 떨어진다. 둘 다 없으면 빈 문자열 — 목록은 주소 없이 나가고
    등록은 막힌다(동작하지 않을 주소를 만들어 주지 않는다).
    """
    settings = get_settings()
    if settings.mcp_internal_base_url:
        return settings.mcp_internal_base_url.rstrip("/")
    if settings.oidc_provider_backchannel_url:
        return settings.oidc_provider_backchannel_url.rstrip("/")
    if settings.platform_public_url:
        return f"{settings.platform_public_url.rstrip('/')}/paas"
    return ""


def is_internal_server_url(url: str) -> bool:
    """이 플랫폼 자신이 노출하는 MCP 서버 주소인가.

    사내 서버는 다른 엔드포인트와 같은 API 키를 요구하는데(api/mcp_servers.py), 목록에서
    가져와 등록할 때는 붙여 넣을 키가 따로 없다 — 그래서 이 판정으로 갈라 전용 키를
    발급해 준다. 주소는 목록이 내준 것(_entry)이므로 기준 주소로 시작하는지만 본다.
    """
    base = internal_base_url()
    return bool(base) and url.startswith(f"{base}/api/v1/mcp/")


def _entry(server_id: str, name: str, description: str, category: str, path: str) -> dict:
    base = internal_base_url()
    return {
        "id": server_id,
        "name": name,
        "description": description,
        "category": category,
        "vendor": "사내(이 플랫폼)",
        "url": f"{base}/api/v1{path}" if base else "",
        "path": f"/paas/api/v1{path}",
    }


def list_internal_servers(db: Session) -> list[dict]:
    """지금 열 수 있는 사내 MCP 서버 전부."""
    settings = get_settings()
    entries = [_entry(
        "paas-ops", "paas-ops",
        "운영 조회 — 배포 상태·이력·앱 로그·라우팅·포트 사용현황·호스트·감사(읽기 전용)",
        "ops", "/mcp/ops",
    )]

    entries.append(_entry(
        "paas-docs", "paas-docs",
        "사내 문서 본문 검색 — 열려 있는 문서 저장소를 가로질러 한 번에 찾는다(읽기 전용)",
        "docs", "/mcp/docs",
    ))
    # 저장소 목록은 환경변수가 정한다 — 설정이 잘못돼 있으면 저장소 항목만 빠지고
    # 나머지 서버는 그대로 나간다(디렉터리 전체가 500으로 죽으면 더 나쁘다).
    try:
        found = storage.stores()
    except storage.StorageError:
        found = []
    for store in found:
        entries.append(_entry(
            f"paas-storage-{store.name}", f"paas-storage-{store.name}",
            f"파일 저장소 '{store.name}' — 목록·읽기{'' if store.read_only else '·쓰기·삭제'}와"
            f" 그 저장소 안 문서 검색{'' if store.read_only else ' (쓰기 열림)'}",
            "storage", f"/mcp/storage/{store.name}",
        ))

    allowed = {n.strip() for n in settings.mcp_db_modules.split(",") if n.strip()}
    for module in db.execute(
        select(Module).where(Module.type == ModuleType.database).order_by(Module.name)
    ).scalars():
        if module.name not in allowed:
            continue  # 허용 목록에 없으면 서버가 403이라 목록에 올리지 않는다
        entries.append(_entry(
            f"paas-db-{module.name}", f"paas-db-{module.name}",
            f"데이터베이스 '{module.name}' 조회 — SELECT 한 문장만, 행 수 상한",
            "database", f"/mcp/db/{module.name}",
        ))

    # 코드 조회는 **서버 하나**다. 예전에는 프로젝트마다 하나씩 나갔는데, 프로젝트가
    # 늘어난 만큼 등록할 모듈과 발급할 키가 늘었다. 프로젝트는 이제 도구 인자로 받는다.
    entries.append(_entry(
        "paas-code", "paas-code",
        "프로젝트 코드 조회 — 파일 목록·내용·구조 개요(project 인자로 고른다, 읽기 전용)",
        "code", "/mcp/code",
    ))
    # 수집해 둔 카탈로그를 읽기만 한다 — 밖으로 나가지 않으므로 여기 올려도 된다.
    entries.append(_entry(
        "paas-apis", "paas-apis",
        "외부 API 카탈로그 검색 — 키워드·카테고리로 공개 API를 찾는다(읽기 전용)",
        "api", "/mcp/apis",
    ))
    return entries


def search_mcp_servers(db: Session, query: str = "") -> list[dict]:
    """사내 MCP 서버 키워드 검색.

    실재하는 서버만 나오지만, 응답까지 보장하지는 않는다 — 등록 후 연결 확인
    (mcp_client.check_server, 모듈 화면의 확인 버튼)으로 실제 응답을 본다.
    """
    servers = list_internal_servers(db)
    needle = query.lower().strip()
    if not needle:
        return servers
    return [
        item for item in servers
        if needle in item["name"].lower()
        or needle in item["description"].lower()
        or needle in item["category"].lower()
    ]


def refresh_mcp_directory(db: Session) -> dict:
    """목록을 다시 만든다 — 캐시가 없으므로 지금 개수를 세어 돌려주는 것이 전부다.

    (예전에는 외부 수집 루트를 재탐색하는 자리였다. 사내 목록은 DB에서 바로 만들기 때문에
    따로 갱신할 것이 없고, 모듈·프로젝트를 추가하면 다음 조회에 그대로 반영된다.)
    """
    servers = list_internal_servers(db)
    return {
        "status": "updated",
        "total_mcp_servers": len(servers),
        "base_url": internal_base_url(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
