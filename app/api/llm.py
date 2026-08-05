"""LLM 프로바이더 · 읽기 전용 코드 열람 · 코드 리뷰.

플랫폼 안에서 diff를 만들어 승인·커밋하던 '에이전트 빌더'는 제거됐다 — 구현은 외부
개발도구가 맡고(에이전트 기획 → 작업 지시 → MCP), 플랫폼은 제약·검증·모니터링만 한다.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import (
    ApiKey,
    LlmProvider,
    LlmProviderKind,
    Organization,
    Project,
)
from ..schemas import (
    LlmProviderCreate,
    LlmProviderOut,
    ReviewRequest,
)
from ..security import encrypt_value, require_admin, require_api_key
from ..services import codemap as codemap_service
from ..services import llm as llm_service
from ..services import workspace
from ..services.build import BuildError, checkout

router = APIRouter(tags=["llm"])


def _provider_out(p: LlmProvider) -> LlmProviderOut:
    kind_val = p.kind.value if hasattr(p.kind, 'value') else str(p.kind)
    if kind_val == "external":
        kind_val = "openai"
    return LlmProviderOut(
        id=p.id, name=p.name, kind=kind_val, base_url=p.base_url,
        model=p.model, has_api_key=bool(p.api_key_encrypted),
        organization_id=p.organization_id,
        org_name=p.organization.name if p.organization_id and p.organization else None,
    )


@router.post("/llm/providers", response_model=LlmProviderOut, status_code=201)
def create_provider(
    body: LlmProviderCreate,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    if db.execute(select(LlmProvider).where(LlmProvider.name == body.name)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="provider name already exists")
    if body.organization_id is not None and db.get(Organization, body.organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    row = LlmProvider(
        name=body.name,
        kind=LlmProviderKind(body.kind),
        base_url=body.base_url,
        api_key_encrypted=encrypt_value(body.api_key) if body.api_key else None,
        model=body.model,
        organization_id=body.organization_id,
    )
    db.add(row)
    db.commit()
    audit.record(db, admin.name, "llm.provider.create", body.name, {"kind": body.kind})
    return _provider_out(row)


@router.get("/llm/providers", response_model=list[LlmProviderOut])
def list_providers(db: Session = Depends(get_db), _: ApiKey = Depends(require_api_key)):
    rows = db.execute(select(LlmProvider).order_by(LlmProvider.id)).scalars()
    return [_provider_out(p) for p in rows]


@router.delete("/llm/providers/{provider_id}", status_code=204)
def delete_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """admin 권한으로 LLM 프로바이더를 삭제한다."""
    row = db.get(LlmProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")
    provider_name = row.name
    db.delete(row)
    db.commit()
    audit.record(db, admin.name, "llm.provider.delete", provider_name, {"provider_id": provider_id})
    return None


@router.get("/projects/{project_id}/files")
def project_files(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """읽기 전용 파일 트리 — 코드 확인 화면. 플랫폼에는 수정 경로가 없다(구현은 외부 빌더)."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        workdir, _sha = checkout(project)
    except BuildError as e:
        raise HTTPException(status_code=502, detail=str(e)[:1000])
    return {"files": workspace.file_tree(workdir)}


@router.get("/projects/{project_id}/files/content")
def project_file_content(
    project_id: int,
    path: str,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """읽기 전용 단일 파일 내용 — 코드 확인 화면. 저장·수정 엔드포인트는 존재하지 않는다."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        workdir, _sha = checkout(project)
    except BuildError as e:
        raise HTTPException(status_code=502, detail=str(e)[:1000])
    try:
        content = workspace.read_file(workdir, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file not found")
    except ValueError as e:
        raise HTTPException(status_code=413, detail=str(e))
    return {"path": path, "content": content}


@router.get("/projects/{project_id}/codemap")
def project_codemap(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """코드 구조 트리 — 파일→클래스/함수 계층 + 항목별 요약(정적 파싱). 코드 확인 화면의
    확대/축소 시각화용이며, 같은 개요가 에이전트 기획의 LLM 컨텍스트에도 주입된다."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        workdir, _sha = checkout(project)
    except BuildError as e:
        raise HTTPException(status_code=502, detail=str(e)[:1000])
    return {"files": codemap_service.build_code_map(workdir)}


@router.post("/projects/{project_id}/review")
async def review_project(
    project_id: int,
    body: ReviewRequest,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, project_id)
    provider = db.get(LlmProvider, body.provider_id)
    if project is None or provider is None:
        raise HTTPException(status_code=404, detail="project or provider not found")
    llm_service.require_provider_access(provider, project, key)

    diff = body.diff
    if diff is None:
        try:
            workdir, _sha = await asyncio.to_thread(checkout, project)
        except Exception:
            workdir = workspace.workdir_for(project)

        if not workdir.exists():
            raise HTTPException(status_code=409, detail="no workspace; pass diff explicitly")
        base = body.base_ref or f"origin/{project.branch}"
        try:
            diff = await asyncio.to_thread(workspace.diff_between, workdir, base)
        except Exception as e:
            # git diff 실패 시 500 대신 422/502 응답
            raise HTTPException(status_code=422, detail=f"git diff failed between '{base}': {e}")

    if not diff or not diff.strip():
        return {"findings": [], "max_severity": "none"}

    try:
        findings = await asyncio.to_thread(llm_service.review_diff, provider, diff, db)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"llm review call failed: {e}")

    severity = llm_service.max_severity(findings)
    audit.record(db, key.name, "code.review", project.name,
                 {"findings": len(findings), "max_severity": severity})
    return {"findings": findings, "max_severity": severity}
