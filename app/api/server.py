"""서버구성 시각화 + 프로젝트별 redirect/rewrite 규칙 관리.

1차(small)의 리버스프록시(Caddy/IIS/Apache)·런타임(Docker/Windows Service) 선택과
등록된 사이트(도메인·상태·리다이렉트 규칙 수)를 한 화면에서 보여준다 — "서버구성
시각화" + "메뉴(라우팅/사이트 항목) 관리" 요건. redirect/rewrite 규칙은 다음
배포/롤백 때 프록시 설정에 반영된다(services/deployer.py의 redirects_for 참고).
"""
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..models import (
    ApiKey, BuildProfile, Deployment, DeploymentStatus, Project, ProjectType,
    RedirectKind, RedirectRule,
)
from ..schemas import (
    ComponentStatus, RedirectRuleCreate, RedirectRuleOut, RedirectRuleSummary,
    ServerConfigOut, ServerConfigSite, UnregisteredSite, WindowsServiceOut,
)
from ..security import require_api_key
from ..services import deployer
from ..services.build import COMPOSITE_COMPONENTS
from ..services.proxy import domain_for, get_proxy, path_prefix_for
from ..services.proxy.base import site_name

router = APIRouter(tags=["server"])


def _windows_services(projects: list[Project]) -> list[WindowsServiceOut]:
    """등록된 paas-* Windows Service를 프로젝트에 맞춰 분류한다.

    이름을 문자열로 역산하지 않고 DB 프로젝트에서 예상 이름을 만들어 맞춘다 —
    프로젝트 이름에 하이픈이 있으면(shop-api 등) 역산은 틀린다.
    """
    from ..services.runtime.windows_service_runtime import list_registered_services  # noqa: PLC0415
    from ..services.runtime.base import RuntimeSpec  # noqa: PLC0415

    expected: dict[str, tuple[str, BuildProfile, str]] = {}
    for p in projects:
        for profile in BuildProfile:
            unit = RuntimeSpec(p.name, "", 0, profile, "").unit_name
            for slot in ("a", "b"):
                expected[f"{unit}-{slot}"] = (p.name, profile, slot)

    found = list_registered_services()
    # 같은 프로젝트·프로필에 슬롯이 둘 다 남아 있으면 다음 배포가 막힌다 — 표시한다.
    seen_units: dict[tuple[str, BuildProfile], int] = {}
    for name, _ in found:
        if name in expected:
            project_name, profile, _slot = expected[name]
            seen_units[(project_name, profile)] = seen_units.get((project_name, profile), 0) + 1

    out = []
    for name, state in found:
        match = expected.get(name)
        if match is None:
            out.append(WindowsServiceOut(name=name, state=state))
            continue
        project_name, profile, slot = match
        out.append(WindowsServiceOut(
            name=name, state=state, project_name=project_name, profile=profile, slot=slot,
            duplicate_slot=seen_units.get((project_name, profile), 0) > 1,
        ))
    return out


@router.get("/server-config", response_model=ServerConfigOut)
def server_config(db: Session = Depends(get_db), _: ApiKey = Depends(require_api_key)):
    settings = get_settings()
    runtime = deployer.get_runtime()
    # windows_service(IIS 프록시) 등에서는 "연결된 프로젝트"의 근거가 web.config에
    # 실제 구성된 라우팅이다 — 프록시가 알려주면 사이트별 in_proxy로 실어 보내고,
    # DB에 없는 항목은 unregistered(이름·rewrite 주소)로 별도 표시한다. 추적하지 않는
    # 백엔드(caddy/apache)는 None → 프런트는 기존처럼 상태로만 판단.
    configured_routes = get_proxy().configured_routes() if settings.tier == "small" else None
    configured_names = None if configured_routes is None else {name for name, _ in configured_routes}
    projects = db.execute(select(Project).order_by(Project.id)).scalars().all()
    rules_by_project: dict[int, list[RedirectRule]] = defaultdict(list)
    for rule in db.execute(select(RedirectRule).order_by(RedirectRule.id)).scalars():
        rules_by_project[rule.project_id].append(rule)
    # composite 컴포넌트의 내부 포트 — 롤백 없이도 마지막으로 running이었던 값을 보여준다
    ports = {
        (project_id, profile, component): port
        for project_id, profile, component, port in db.execute(
            select(
                Deployment.project_id, Deployment.profile, Deployment.component,
                Deployment.internal_port,
            ).where(
                Deployment.status == DeploymentStatus.running,
                Deployment.component.is_not(None),
            )
        ).all()
    }
    # 일반 프로젝트(컴포넌트 없음)의 업스트림 — 서버구성 화면의 URL 칸이 공개 주소가
    # 아니라 프록시가 실제로 전달하는 곳을 보여주기 위한 값이다.
    #
    # host_port를 읽는다. internal_port는 **컨테이너 내부** 포트(8000 등)라 프록시가
    # 바라보는 주소가 아니고, 일반 프로젝트의 배포 레코드에는 채워지지도 않는다
    # (composite 컴포넌트 재기동용으로만 저장된다) — 그걸 읽으면 항상 비어 보인다.
    upstreams = {
        (project_id, profile): port
        for project_id, profile, port in db.execute(
            select(
                Deployment.project_id, Deployment.profile, Deployment.host_port,
            ).where(
                Deployment.status == DeploymentStatus.running,
                Deployment.component.is_(None),
            )
        ).all()
        if port is not None
    }
    sites = []
    for p in projects:
        for profile in BuildProfile:
            components = None
            if p.type == ProjectType.composite:
                # composite는 {name}-backend/{name}-frontend로 따로 등록되므로(런타임
                # 유닛 이름 규칙은 RuntimeSpec.unit_name과 동일), 컴포넌트별로 조회하고
                # 전체 상태는 둘의 상태를 종합해 요약한다 — {name} 단독 유닛은 없다.
                components = []
                for name in COMPOSITE_COMPONENTS:
                    try:
                        comp_status = runtime.status(f"{p.name}-{name}", profile)
                    except Exception as e:  # noqa: BLE001
                        comp_status = f"unknown ({e})"
                    components.append(ComponentStatus(
                        name=name, status=comp_status,
                        internal_port=ports.get((p.id, profile, name)),
                    ))
                statuses = {c.status for c in components}
                status = statuses.pop() if len(statuses) == 1 else "partial"
            else:
                try:
                    status = runtime.status(p.name, profile)
                except Exception as e:  # noqa: BLE001 — 런타임 미설치/미접근이 전체 화면을 막지 않게
                    status = f"unknown ({e})"
            org_name = p.organization.name if p.organization else None
            project_rules = rules_by_project.get(p.id, [])
            in_proxy = (
                None if configured_names is None
                else site_name(p.name, profile) in configured_names
            )
            sites.append(ServerConfigSite(
                project_id=p.id,
                project_name=p.name,
                profile=profile,
                domain=domain_for(p.name, p.domain, profile),
                path_prefix=path_prefix_for(org_name, p.name, p.domain, profile),
                status=status,
                internal_host="127.0.0.1" if upstreams.get((p.id, profile)) else None,
                internal_port=upstreams.get((p.id, profile)),
                redirect_count=len(project_rules),
                redirects=[
                    RedirectRuleSummary(
                        from_path=r.from_path, to_path=r.to_path,
                        kind=r.kind, status_code=r.status_code,
                    )
                    for r in project_rules
                ],
                components=components,
                in_proxy=in_proxy,
            ))
    unregistered: list[UnregisteredSite] = []
    if configured_routes is not None:
        registered = {
            site_name(p.name, profile) for p in projects for profile in BuildProfile
        }
        unregistered = [
            UnregisteredSite(name=name, rewrite_targets=targets)
            for name, targets in configured_routes
            if name not in registered
        ]

    return ServerConfigOut(
        runtime_backend=settings.runtime_backend if settings.tier == "small" else "kubernetes",
        proxy_backend=settings.proxy_backend if settings.tier == "small" else "k8s-ingress",
        sites=sites,
        unregistered=unregistered,
        windows_services=(
            _windows_services(projects)
            if settings.tier == "small" and settings.runtime_backend == "windows_service"
            else []
        ),
    )


@router.post("/projects/{project_id}/redirects", response_model=RedirectRuleOut, status_code=201)
def create_redirect(
    project_id: int,
    body: RedirectRuleCreate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    row = RedirectRule(
        project_id=project_id, from_path=body.from_path, to_path=body.to_path,
        kind=RedirectKind(body.kind), status_code=body.status_code,
    )
    db.add(row)
    db.commit()
    audit.record(db, key.name, "redirect.create", project.name,
                 {"from": body.from_path, "to": body.to_path, "kind": body.kind})
    return row


@router.get("/projects/{project_id}/redirects", response_model=list[RedirectRuleOut])
def list_redirects(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return list(
        db.execute(
            select(RedirectRule)
            .where(RedirectRule.project_id == project_id)
            .order_by(RedirectRule.id)
        ).scalars()
    )


@router.delete("/redirects/{redirect_id}", status_code=204)
def delete_redirect(
    redirect_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    row = db.get(RedirectRule, redirect_id)
    if row is None:
        raise HTTPException(status_code=404, detail="redirect not found")
    project = db.get(Project, row.project_id)
    db.delete(row)
    db.commit()
    audit.record(db, key.name, "redirect.delete",
                 project.name if project else str(row.project_id), {"id": redirect_id})
