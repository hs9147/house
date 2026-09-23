"""LLM 프로바이더 · 읽기 전용 코드 열람 · 코드 리뷰.

플랫폼 안에서 diff를 만들어 승인·커밋하던 '에이전트 빌더'는 제거됐다 — 구현은 외부
개발도구가 맡고(에이전트 기획 → 작업 지시 → MCP), 플랫폼은 제약·검증·모니터링만 한다.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
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
from ..services import bedrock
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
        aws_profile=p.aws_profile,
        organization_id=p.organization_id,
        org_name=p.organization.name if p.organization_id and p.organization else None,
        is_default=bool(p.is_default),
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
    base_url = body.base_url
    if not base_url and body.aws_profile:
        # 비워 온 것은 "프로필이 정하게 해 달라"는 뜻이다. 호출할 때마다 다시 유추하지 않고
        # 등록 시점에 확정해 기록한다 — 목록에 실제 주소가 보이고, 리전이 어디로 정해졌는지
        # 사람이 확인할 수 있다(호출 때만 정하면 어디로 나가는지 화면에 없다).
        base_url = bedrock.runtime_url(bedrock.region_from_url("", body.aws_profile))
    row = LlmProvider(
        name=body.name,
        kind=LlmProviderKind(body.kind),
        base_url=base_url,
        api_key_encrypted=encrypt_value(body.api_key) if body.api_key else None,
        aws_profile=body.aws_profile or None,
        model=body.model,
        organization_id=body.organization_id,
    )
    db.add(row)
    db.commit()
    audit.record(db, admin.name, "llm.provider.create", body.name, {"kind": body.kind})
    return _provider_out(row)


@router.get("/llm/aws/profiles")
def aws_profiles(_: ApiKey = Depends(require_admin)):
    """서버 ~/.aws에 있는 자격증명 프로필과 지금 쓸 수 있는지 여부.

    Bedrock은 정적 키가 없어 어떤 프로필을 쓸지 골라야 하고, 사내 SSO 토큰은 보통
    8시간이면 만료된다 — 등록할 때와 실패했을 때 '어느 프로필로 재로그인해야 하는지'를
    화면이 말할 수 있어야 하므로 상태를 같이 싣는다.

    프로필 이름은 비밀이 아니지만 서버의 계정 구성을 드러내므로 admin에게만 준다.
    """
    profiles = bedrock.list_profiles()
    return {
        # config_state가 읽은 경로·파일 존재·**어느 파이썬인지**·botocore 여부를 함께 낸다.
        # 어디를 읽었는지 밝힌다. 서비스로 돌면 홈 디렉터리가 로그인한 사람의 것이 아니라
        # 서비스 계정의 것이다(nssm 기본값은 LocalSystem →
        # C:\Windows\system32\config\systemprofile) — 그러면 `aws sso login`을 해도
        # 목록이 비어 있고, 경로를 보여 주지 않으면 왜 비었는지 알 방법이 없다.
        **bedrock.config_state(),
        "profiles": [{**entry, **bedrock.profile_status(entry["name"])} for entry in profiles],
    }


@router.post("/llm/aws/login")
def aws_sso_login(
    profile: str,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """서버에서 이 프로필의 SSO 로그인을 시작하고 승인용 주소·코드를 돌려준다.

    **완전 무인은 안 된다.** SSO는 사람이 브라우저에서 승인해야 토큰이 나온다. 없애는 것은
    "서버에 원격 접속해 명령을 치는 일"이다 — 플랫폼이 로그인을 시작하고, 사람은 화면에 뜬
    주소를 열어 코드를 승인한다. 토큰은 그 프로세스가 받으므로 **서비스 계정의 홈**에
    떨어진다: "내 계정으로 로그인했는데 서비스는 못 본다"는 문제도 같이 풀린다.

    승인이 끝났는지는 이 응답으로 알 수 없다(프로세스가 기다리는 중이다) — 호출부가
    /llm/aws/profiles를 다시 물어 ok가 되는지로 확인한다.
    """
    try:
        result = bedrock.start_sso_login(
            profile, get_settings().resolved_repo_root / "logs")
    except bedrock.BedrockError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(db, admin.name, "aws.sso.login", profile,
                 {"url_found": bool(result["verification_url"])})
    return result


@router.get("/llm/aws/models")
def aws_models(profile: str, base_url: str = "", _: ApiKey = Depends(require_admin)):
    """로그인된 자격증명으로 지금 부를 수 있는 모델 목록 — 등록 화면의 모델 선택용.

    모델 ID를 손으로 적게 두면 틀린다. 실측: ap-northeast-2에서 `anthropic.claude-…`를
    그대로 넣으면 "on-demand throughput isn't supported — use an inference profile"로
    거부된다. 실제로 통하는 ID를 계정에서 받아 고르게 한다(컨트롤 플레인 조회라 과금 없음).

    토큰이 만료됐으면 목록을 받을 수 없다 — 그 사유(재로그인 명령 포함)를 그대로 올린다.
    """
    region = bedrock.region_from_url(base_url, profile)
    try:
        models = bedrock.list_models(profile=profile, region=region)
    except bedrock.BedrockError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"region": region, "models": models}


@router.get("/llm/providers", response_model=list[LlmProviderOut])
def list_providers(db: Session = Depends(get_db), _: ApiKey = Depends(require_api_key)):
    rows = db.execute(select(LlmProvider).order_by(LlmProvider.id)).scalars()
    return [_provider_out(p) for p in rows]


@router.post("/llm/providers/{provider_id}/default", response_model=LlmProviderOut)
def set_default_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """이 프로바이더를 **기본값**으로 — 자동으로 도는 판단이 이 모델을 쓴다.

    배포 점검·실패 원인 분석·레포 검토는 사람이 모델을 고를 자리가 없다(버튼 하나로 돈다).
    그래서 기본값을 한 곳에서 정하고, 하나만 참으로 둔다 — 여러 개가 참이면 "어느 것으로
    돌았나"를 화면이 설명할 수 없다.
    """
    row = db.get(LlmProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")
    llm_service.set_default(db, row)
    audit.record(db, admin.name, "llm.provider.default", row.name, {"provider_id": row.id})
    db.refresh(row)
    return _provider_out(row)


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
