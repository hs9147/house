from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, Module, ModuleBinding, ModuleType, Organization, Project
from ..schemas import (
    ApiModuleImport,
    GlobalModuleUsageSummary,
    ModuleBind,
    ModuleCreate,
    ModuleHistoryItem,
    PlatformModuleReportOut,
)
from ..security import require_admin, require_api_key
from ..services import a2a as a2a_service
from ..services import apisearch
from ..services import egress
from ..services import mcp_client, mcp_search
from ..services import modules as svc

router = APIRouter(tags=["modules"])


@router.post("/modules", status_code=201)
def create_module(
    body: ModuleCreate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    if db.execute(select(Module).where(Module.name == body.name)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="module name already exists")
    if body.organization_id is not None and db.get(Organization, body.organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")

    row = Module(
        name=body.name, type=ModuleType(body.type), category=body.category,
        organization_id=body.organization_id, config=svc.encrypt_config(body.config),
    )
    db.add(row)
    db.commit()
    audit.record(db, key.name, "module.create", body.name, {"type": body.type})
    return {"id": row.id, "name": row.name, "type": row.type.value, "category": row.category,
            "organization_id": row.organization_id, "config": svc.masked_config(row.config)}


@router.get("/modules")
def list_modules(db: Session = Depends(get_db), _: ApiKey = Depends(require_api_key)):
    rows = db.execute(select(Module).order_by(Module.id)).scalars()
    return [
        {"id": m.id, "name": m.name, "type": m.type.value, "category": m.category,
         "organization_id": m.organization_id, "config": svc.masked_config(m.config),
         # 이 모듈로 나가는 플랫폼 호출에 내부 정보가 실리는지 — 볼 때마다 계산한다
         # (주소를 바꾸면 즉시 반영돼야 하므로 플래그로 굳히지 않는다).
         "egress": egress.inspect_module(m)}
        for m in rows
    ]


@router.put("/modules/{module_id}")
def update_module(
    module_id: int,
    body: ModuleCreate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    row = db.get(Module, module_id)
    if row is None:
        raise HTTPException(status_code=404, detail="module not found")
    row.name = body.name
    row.category = body.category
    row.organization_id = body.organization_id
    if body.config:
        existing_cfg = svc.decrypt_config(row.config or {})
        existing_cfg.update(body.config)
        row.config = svc.encrypt_config(existing_cfg)
    db.commit()
    audit.record(db, key.name, "module.update", row.name, {"type": row.type.value})
    return {"id": row.id, "name": row.name, "type": row.type.value, "category": row.category,
            "organization_id": row.organization_id, "config": svc.masked_config(row.config)}


@router.delete("/modules/{module_id}", status_code=204)
def delete_module(
    module_id: int,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """admin 권한으로 모듈을 삭제한다."""
    row = db.get(Module, module_id)
    if row is None:
        raise HTTPException(status_code=404, detail="module not found")

    bindings = db.execute(select(ModuleBinding).where(ModuleBinding.module_id == module_id)).scalars().all()
    for b in bindings:
        db.delete(b)

    mod_name = row.name
    db.delete(row)
    db.commit()
    audit.record(db, admin.name, "module.delete", mod_name, {"module_id": module_id})
    return None


@router.post("/projects/{project_id}/modules/{module_id}/bind", status_code=201)
def bind_module(
    project_id: int,
    module_id: int,
    body: ModuleBind,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, project_id)
    module = db.get(Module, module_id)
    if project is None or module is None:
        raise HTTPException(status_code=404, detail="project or module not found")
    dup = db.execute(
        select(ModuleBinding).where(
            ModuleBinding.project_id == project_id,
            ModuleBinding.env_prefix == body.env_prefix,
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=409, detail="env_prefix already used in this project")
    db.add(ModuleBinding(project_id=project_id, module_id=module_id, env_prefix=body.env_prefix))
    db.commit()
    audit.record(db, key.name, "module.bind", project.name,
                 {"module": module.name, "prefix": body.env_prefix})
    # 주입될 환경변수 키를 미리 보여준다 (값은 배포 시에만 주입)
    return {"injected_env": sorted(svc.binding_env(module, body.env_prefix, db=db).keys())}


@router.delete("/projects/{project_id}/modules/bindings/{binding_id}", status_code=204)
def unbind_module(
    project_id: int,
    binding_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """프로젝트에서 모듈 바인딩을 해제한다. 모듈 정의 자체는 남고, 다음 배포부터
    이 바인딩의 환경변수만 더는 주입되지 않는다."""
    binding = db.get(ModuleBinding, binding_id)
    if binding is None or binding.project_id != project_id:
        raise HTTPException(status_code=404, detail="binding not found")
    project = db.get(Project, project_id)
    module = db.get(Module, binding.module_id)
    db.delete(binding)
    db.commit()
    audit.record(db, key.name, "module.unbind", project.name if project else str(project_id),
                 {"module": module.name if module else str(binding.module_id),
                  "prefix": binding.env_prefix})
    return None


@router.get("/modules/search")
def search_external_apis(
    keyword: str = "",
    category: str = "",
    source: str = "",
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_admin),
):
    """키워드·카테고리로 외부 API 카탈로그를 검색한다.

    아웃바운드 조회가 아니다 — 수집해 둔 표(api_catalog)만 읽는다. admin으로 두는 것은
    이 화면이 모듈 등록(admin 전용)으로 이어지기 때문이고, 읽기만 필요한 쪽에는 같은
    검색을 MCP로 열어 두었다(/mcp/apis).

    세 조건(keyword·category·source)은 AND이고 각각 비우면 그 조건은 걸지 않는다.
    category="기타"는 카테고리가 없는 항목만 고르고, source는 소스 하나만 본다
    (공공데이터만 보기 = source=publicdata — 값은 /modules/search/status에서 얻는다).
    반환된 항목은 POST /modules/import로 external_api 모듈에 추가할 수 있다."""
    try:
        return apisearch.search_apis(db, keyword, category, source)
    except apisearch.ApiSearchError as e:
        # 여기서 나는 오류는 상류 장애가 아니라 인자가 틀린 것이다(모르는 source).
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/modules/search/categories")
def list_api_categories(
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_admin),
):
    """검색 화면의 카테고리 선택지 — 카탈로그에 실제로 있는 값만 내려간다."""
    return {"categories": apisearch.list_categories(db),
            "uncategorized_label": apisearch.UNCATEGORIZED}


@router.get("/modules/search/status")
def api_catalog_status(
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_admin),
):
    """수집 현황 — 소스별 건수·마지막 갱신 시각과 그 소스를 켜 두었는지(enabled).

    검색 화면의 소스 선택지가 여기서 나온다. 0건인 소스도 함께 내려간다: 빼 버리면
    "공공데이터가 안 나온다"에 답할 자리가 사라진다 — 주소를 안 넣어 아예 안 부르는
    것인지(enabled=false), 불렀는데 못 받은 것인지가 여기서 갈린다."""
    return apisearch.catalog_status(db)


@router.post("/modules/search/refresh")
def refresh_external_api_directory(
    source: str = "",
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_admin),
):
    """카탈로그를 1일 1회 주기 외에 즉시 수집한다(아웃바운드).

    source를 주면 그 소스만 받는다(공공데이터만 최신화 = source=publicdata).
    응답의 added/updated/unchanged는 이번 수집이 실제로 무엇을 바꿨는지다(대부분은
    unchanged다). warnings는 실패한 소스이고, 그 소스의 행은 손대지 않는다.

    최소 간격을 걸지 않는다 — 사람이 버튼을 한 번 누른 행위다. 모델이 부르는
    /mcp/apis의 sync_catalog에는 간격이 걸려 있다."""
    try:
        return apisearch.sync_catalog(db, source)
    except apisearch.ApiSearchError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/modules/search/refresh-mcp")
def refresh_mcp_directory(
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_admin),
):
    """사내 MCP 서버 목록을 다시 만든다(현재 개수·기준 주소 확인용)."""
    return mcp_search.refresh_mcp_directory(db)


@router.post("/modules/import", status_code=201)
def import_api_module(
    body: ApiModuleImport,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """검색 결과를 external_api 모듈로 자동 추가한다 — 이름은 모듈명 규약으로 정규화."""
    name = apisearch.normalize_module_name(body.name)
    base, suffix = name, 2
    while db.execute(select(Module).where(Module.name == name)).scalar_one_or_none():
        name = f"{base[:37]}-{suffix}"
        suffix += 1
    row = Module(
        name=name, type=ModuleType.external_api, category=body.category,
        config=svc.encrypt_config({"url": body.url}),
    )
    db.add(row)
    db.commit()
    audit.record(db, admin.name, "module.import", name, {"source": body.name, "url": body.url})
    return {"id": row.id, "name": row.name, "type": row.type.value, "category": row.category,
            "organization_id": row.organization_id, "config": svc.masked_config(row.config)}


@router.get("/projects/{project_id}/modules")
def project_modules(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return a2a_service.list_project_a2a_cards(db, project)


@router.get("/projects/{project_id}/resources")
def project_resources(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """대화식 편집 화면용 — 바인딩 여부와 무관하게 이 프로젝트에서 쓸 수 있는 모든
    자원(카테고리별 API, 공유 파일 저장소, 조직별 DB 등)을 아이템화해 반환한다."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return svc.available_resources(db, project)


@router.get("/mcp/search")
def search_mcp_directory(
    q: str = "",
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """사내 MCP 서버 검색 — 이 플랫폼이 실제로 노출하는 서버만 나온다."""
    return mcp_search.search_mcp_servers(db, q)


@router.post("/modules/import-mcp", status_code=201)
def import_mcp_module(
    body: ApiModuleImport,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """검색된 MCP 서버를 mcp 타입 모듈로 자동 추가한다.

    **사내 서버면 전용 API 키를 함께 발급한다.** 사내 MCP 서버는 다른 엔드포인트와 같은
    키를 요구하는데(api/mcp_servers.py), 키 없이 등록하면 등록은 성공한 채 연결 확인이
    401로 떨어지고 바인딩된 앱도 붙지 못한다 — 원클릭 등록이 동작하지 않는 모듈을
    만들어 내는 셈이다.

    발급하는 키는 **비관리자**다. mcp 모듈의 api_key는 바인딩된 앱의 환경변수로도
    주입되므로(services/modules.binding_env), 관리자 키를 넣으면 관리자 권한이 앱 env로
    새어 나간다.
    """
    mod_name = apisearch.normalize_module_name(body.name)
    if db.execute(select(Module).where(Module.name == mod_name)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"module '{mod_name}' already exists")

    issued = _issue_module_key(db, admin, mod_name) if mcp_search.is_internal_server_url(body.url) else ""
    config = {"url": body.url, "api_key": issued}
    row = Module(
        name=mod_name,
        type=ModuleType.mcp,
        category=body.category or "mcp",
        config=svc.encrypt_config(config),
    )
    db.add(row)
    db.commit()
    audit.record(db, admin.name, "module.import_mcp", mod_name,
                 {"url": body.url, "key_issued": bool(issued)})
    return {"id": row.id, "name": row.name, "type": row.type.value, "category": row.category,
            "config": svc.masked_config(row.config),
            # 화면이 "키를 따로 넣을 필요가 없다"를 말할 수 있어야 한다
            "key_issued": bool(issued)}


def _issue_module_key(db: Session, admin: ApiKey, module_name: str) -> str:
    """이 모듈 전용 비관리자 키. 이름을 모듈에 맞춰 두어 키 목록에서 회수할 수 있게 한다."""
    from ..security import hash_key, issue_key  # noqa: PLC0415 — 순환 import 회피

    key_name = f"mcp-{module_name}"[:64]
    raw = issue_key()
    row = db.execute(select(ApiKey).where(ApiKey.name == key_name)).scalar_one_or_none()
    if row is None:
        db.add(ApiKey(name=key_name, key_hash=hash_key(raw), is_admin=False))
    else:
        # 같은 이름의 모듈을 지웠다 다시 가져온 경우다 — 옛 키는 갈아 끼운다.
        row.key_hash = hash_key(raw)
        row.is_admin = False
    audit.record(db, admin.name, "key.issue", key_name, {"is_admin": False, "for": module_name})
    return raw


@router.post("/modules/{module_id}/mcp-key")
def issue_mcp_module_key(
    module_id: int,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """이미 등록된 사내 MCP 모듈에 전용 키를 발급해 그 자리에서 넣는다.

    자동 발급은 '사내 MCP 검색'으로 가져올 때만 걸린다. 그 전에 등록됐거나 주소를 직접
    적어 만든 모듈은 키가 빈 채로 남고, 연결 확인이 401로 떨어진다. 예전 안내는 **모듈을
    지우고 다시 가져오라**고 했는데, 바인딩된 프로젝트가 있으면 그럴 수 없다 — 고치는
    방법이 곧 잃는 방법이면 안내가 아니다.

    **사내 주소일 때만 발급한다.** 발급한 키는 그 주소로 그대로 전송되므로(mcp_client의
    Authorization 헤더), 사외 주소에 붙이면 플랫폼 키를 남의 서버로 보내는 꼴이 된다.
    거절할 때는 지금 기준 주소가 무엇인지 함께 알려 준다 — 사내 서버인데 거절당했다면
    기준 주소 설정이 틀린 것이고, 그 값이 보여야 바로잡을 수 있다.
    """
    row = db.get(Module, module_id)
    if row is None:
        raise HTTPException(status_code=404, detail="module not found")
    if row.type != ModuleType.mcp:
        raise HTTPException(status_code=400, detail=f"mcp 타입 모듈이 아닙니다: {row.type.value}")
    config = svc.decrypt_config(row.config)
    url = config.get("url", "")
    if not mcp_search.is_internal_server_url(url):
        base = mcp_search.internal_base_url() or "(비어 있음 — PAAS_MCP_INTERNAL_BASE_URL 미설정)"
        raise HTTPException(status_code=400, detail=(
            f"사내 MCP 주소가 아니라 키를 발급하지 않습니다: {url} — 발급한 키는 이 주소로"
            " 전송되므로 사외 서버에는 붙일 수 없습니다. 사외 서버라면 모듈 수정에서"
            " config.api_key에 그 서버가 준 키를 넣으세요. 사내 서버인데 여기서 걸린다면"
            f" 기준 주소 설정을 보세요(현재 기준: {base})."))

    issued = _issue_module_key(db, admin, row.name)
    config["api_key"] = issued
    row.config = svc.encrypt_config(config)
    db.commit()
    audit.record(db, admin.name, "module.issue_mcp_key", row.name, {"url": url})
    return {"id": row.id, "name": row.name, "key_issued": True,
            "config": svc.masked_config(row.config)}


@router.post("/modules/{module_id}/mcp-check")
def check_mcp_module(
    module_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_admin),
):
    """이 MCP 모듈이 실제로 응답하는지 확인한다(tools/list 1회).

    등록만으로는 동작을 알 수 없다 — 주소가 틀렸거나 전송 방식이 안 맞으면 등록은
    성공한 채 조용히 죽어 있다. 확인 실패는 오류가 아니라 결과이므로 200으로 내려주고
    본문의 ok/error로 구분한다(화면이 여러 모듈을 나열하며 표시한다).
    """
    row = db.get(Module, module_id)
    if row is None:
        raise HTTPException(status_code=404, detail="module not found")
    if row.type != ModuleType.mcp:
        raise HTTPException(status_code=400, detail=f"mcp 타입 모듈이 아닙니다: {row.type.value}")
    config = svc.decrypt_config(row.config)
    url, api_key = config.get("url", ""), config.get("api_key") or None
    result = mcp_client.check_server(url, api_key)
    # 화면의 '키 발급' 버튼은 이 값으로 나온다 — 오류 문구를 파싱해 판단하게 두면
    # 문구를 다듬을 때마다 버튼이 조용히 사라진다.
    result["can_issue_key"] = not api_key and mcp_search.is_internal_server_url(url)
    if not result["ok"] and "401" in (result["error"] or "") and not api_key:
        # 401을 그대로 내주면 "주소가 틀렸나"를 먼저 의심하게 된다 — 원인은 키가 없는
        # 것이고, 사내 서버는 다시 가져오기만 하면 전용 키가 발급된다.
        result["error"] += (
            " — 이 모듈에 API 키가 없습니다."
            + (" 사내 MCP 서버는 인증이 필요합니다. '키 발급'으로 이 모듈에 전용 키를"
               " 넣으세요(모듈을 지울 필요 없습니다 — 바인딩도 그대로 유지됩니다)."
               if mcp_search.is_internal_server_url(url)
               else " 모듈 수정에서 config.api_key에 키를 넣으세요.")
        )
    return {"module_id": row.id, "name": row.name, "url": url, **result}


@router.get("/modules/usage-report", response_model=PlatformModuleReportOut)
def get_platform_module_report(
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """PaaS 플랫폼 전역 모듈 사용 이력 리포트 — 등록된 모든 모듈의 바인딩 프로젝트 현황 및 최근 모듈 관련 변경 로그를 종합 집계한다."""
    from sqlalchemy.orm import joinedload  # noqa: PLC0415
    from ..models import AuditEvent  # noqa: PLC0415

    modules = db.execute(
        select(Module).options(joinedload(Module.organization)).order_by(Module.id.desc())
    ).scalars().unique().all()

    bindings = db.execute(
        select(ModuleBinding, Project)
        .join(Project, ModuleBinding.project_id == Project.id)
    ).all()

    bindings_by_module: dict[int, list[str]] = {}
    for binding, proj in bindings:
        bindings_by_module.setdefault(binding.module_id, []).append(proj.name)

    summaries: list[GlobalModuleUsageSummary] = []
    total_bindings = len(bindings)

    for m in modules:
        proj_list = bindings_by_module.get(m.id, [])
        org_name = m.organization.name if m.organization else None
        summaries.append(
            GlobalModuleUsageSummary(
                module_id=m.id,
                module_name=m.name,
                type=m.type.value,
                category=m.category,
                organization_name=org_name,
                bound_project_count=len(proj_list),
                bound_projects=proj_list,
                created_at=m.created_at,
            )
        )

    # 최근 모듈 관련 감사 이벤트
    audit_rows = db.execute(
        select(AuditEvent)
        .where(AuditEvent.action.like("module.%"))
        .order_by(AuditEvent.created_at.desc())
        .limit(100)
    ).scalars()

    recent_history: list[ModuleHistoryItem] = [
        ModuleHistoryItem(
            id=r.id,
            actor=r.actor,
            action=r.action,
            target=r.target,
            # 감사 표의 컬럼 이름은 detail이다(models.AuditEvent) — 응답 필드 이름만
            # payload로 나간다.
            payload=r.detail or {},
            created_at=r.created_at,
        )
        for r in audit_rows
    ]

    return PlatformModuleReportOut(
        total_modules=len(modules),
        total_bindings=total_bindings,
        modules=summaries,
        recent_history=recent_history,
    )
