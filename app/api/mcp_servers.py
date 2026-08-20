"""사내 MCP 서버 — 플랫폼이 이미 가진 것을 MCP 도구로 노출한다.

왜 자체 개발인가: 공개 레지스트리의 동종 서버는 대부분 벤더 호스팅 원격 엔드포인트이거나
stdio 전용이다. 앞은 소스·운영 데이터를 사외로 내보내고(자체 Gitea를 두는 이유와 정면
충돌), 뒤는 이 플랫폼 클라이언트(services/mcp_client.py — streamable-http 단일 JSON
응답)로는 통신 자체가 안 된다. 그래서 필요한 것만 사내에서 만든다.

서버 5개:
  POST /mcp/ops                  운영 조회 — 배포 상태·로그·라우팅·호스트·감사(읽기 전용)
  POST /mcp/projects/{id}/code   프로젝트 코드 조회(읽기 전용)
  POST /mcp/docs                 사내 문서 본문 검색 — 저장소를 가로질러 한 번에(읽기 전용)
  POST /mcp/storage/{module}     file_storage 모듈 파일 — 모듈 루트 밖으로 나갈 수 없다
  POST /mcp/db/{module}          database 모듈 조회 — SELECT 전용, 허용 목록에 있는 모듈만

문서 검색이 두 곳에 있는 이유: /mcp/storage/{module}은 **그 저장소 안**을 다루는 도구
묶음이고(목록·읽기·쓰기와 함께 검색), /mcp/docs는 "사내 문서에서 찾아라"는 하나의 일을
위한 서버다 — 저장소 이름을 모르는 쪽에서 부르므로 전 저장소를 가로질러 찾고 결과에
어느 저장소인지를 실어 준다.

쓰는 방법: 이 주소를 mcp 타입 모듈로 등록해 프로젝트에 바인딩하면 기획 "솔루션 구성"
단계 대화가 도구로 쓴다(services/planning.solution_tools). 주소는 플랫폼 자신이므로
사내에서 실제로 닿는 주소를 넣는다 — 공개 도메인이 이 플랫폼으로 라우팅되지 않는
구성이면 내부 주소를 쓴다(예: http://localhost:7000/paas/api/v1/mcp/ops).

인증은 다른 엔드포인트와 같은 API 키다. 관리자 전용으로 올리지 않은 이유가 있다 —
mcp 모듈의 api_key는 배포된 앱의 환경변수로도 주입되므로(services/modules.binding_env),
여기에 관리자 키를 넣게 만들면 관리자 권한이 앱 env로 새어 나간다. 대신 위험은 도구
쪽에서 막는다: db 서버는 SELECT 전용 + 허용 목록, 나머지 셋은 읽기 전용이거나 모듈
루트 안에 갇혀 있다.
"""
import json
import re
from fnmatch import fnmatchcase
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..features import require_feature
from ..models import (
    ApiKey, AuditEvent, BuildProfile, Deployment, DeploymentStatus, Module, ModuleType,
    Project, ProjectType,
)
from ..security import require_api_key
from ..services import codemap as codemap_service
from ..services import deployer, docsearch, doctext, mcp_server, monitor, ports, workspace
from ..services import modules as modules_service
from ..services import storage as storage_service
from ..services.build import COMPOSITE_COMPONENTS, BuildError, checkout
from ..services.proxy import domain_for, path_prefix_for

router = APIRouter(tags=["mcp"])

# LLM이 인자를 넉넉하게 넣는 일이 잦아서 상한을 서버가 정한다.
_MAX_TAIL = 500
_MAX_ROWS = 200
_MAX_LIST = 50
_MAX_FILE_LIST = 1000
# 도구가 한 번에 돌려줄 텍스트 상한(문자 수) — 리포 파일 읽기와 같은 기준을 쓴다.
# 초과분은 오류가 아니라 잘라서 준다: 100쪽 PDF를 "너무 큽니다"로 거절하면 읽을 방법이
# 아예 없어지고, 문서는 본문만 뽑으면 원본보다 훨씬 작아지는 경우가 많다.
_MAX_TEXT_CHARS = workspace.MAX_CONTEXT_FILE_BYTES


def _truncate(text: str) -> str:
    if len(text) <= _MAX_TEXT_CHARS:
        return text
    return (f"{text[:_MAX_TEXT_CHARS]}\n\n[…앞 {_MAX_TEXT_CHARS}자만 표시 — 전체 "
            f"{len(text)}자]")


def _dump(obj) -> str:
    """도구 응답 직렬화 — datetime·Enum이 섞여 오므로 default=str로 흘린다."""
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _int_arg(args: dict, name: str, default: int, cap: int) -> int:
    raw = args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise mcp_server.McpToolError(f"{name}은 정수여야 합니다: {raw!r}")
    return max(1, min(value, cap))


def _str_arg(args: dict, name: str, required: bool = True) -> str:
    value = str(args.get(name) or "").strip()
    if required and not value:
        raise mcp_server.McpToolError(f"{name}이 비어 있습니다.")
    return value


def _profile_arg(args: dict) -> BuildProfile:
    raw = str(args.get("profile") or BuildProfile.release.value)
    try:
        return BuildProfile(raw)
    except ValueError:
        raise mcp_server.McpToolError(
            f"unknown profile: {raw} (release|development)")


# --- 운영 조회 서버 (/mcp/ops) ---

_OPS_TOOLS = [
    {
        "name": "list_routes",
        "description": (
            "등록된 사이트(프로젝트×프로필)의 상태·공개 경로·내부 업스트림과, 프록시 설정에만"
            " 있고 플랫폼에 없는 라우트를 반환한다. 프로젝트 이름을 모를 때 여기서 찾는다."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_deploy_status",
        "description": "프로젝트의 프로필별 실행 상태·내부 업스트림·최근 배포 결과를 반환한다.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
    {
        "name": "list_deployments",
        "description": "프로젝트의 최근 배포 이력(상태·커밋·오류)을 최신순으로 반환한다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "limit": {"type": "integer", "description": f"기본 10, 최대 {_MAX_LIST}"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "tail_app_log",
        "description": "배포된 앱의 런타임 로그 tail. profile: release|development(기본 release)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "profile": {"type": "string"},
                "tail": {"type": "integer", "description": f"기본 100, 최대 {_MAX_TAIL}"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "host_snapshot",
        "description": "호스트 자원 스냅샷(CPU·메모리·디스크·GPU)과 런타임 지원 여부.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_ports",
        "description": (
            "호스트 포트 사용현황 — 배정 대장(어느 프로젝트·프로필이 어느 포트를 쓰는지)과"
            " 실제 리슨 여부. probe_range=true면 범위 전체를 훑어 대장에 없는 점유까지 찾는다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"probe_range": {"type": "boolean"}},
        },
    },
    {
        "name": "search_audit",
        "description": "감사 이벤트 검색 — 누가(actor) 언제 무엇(action)을 어디에(target) 했는지.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "actor": {"type": "string"},
                "action": {"type": "string", "description": "부분 일치(예: deploy, module)"},
                "target": {"type": "string"},
                "limit": {"type": "integer", "description": f"기본 20, 최대 {_MAX_LIST}"},
            },
        },
    },
]


@router.post("/mcp/ops", dependencies=[Depends(require_feature("deploy"))])
async def ops_mcp_server(
    request: Request,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """운영 조회 MCP 서버(JSON-RPC 2.0) — 읽기 전용."""
    return mcp_server.dispatch(
        await mcp_server.read_payload(request),
        server_name="paas-ops",
        tools=_OPS_TOOLS,
        call=lambda name, args: _ops_call(db, name, args),
    )


def _project_by_name(db: Session, args: dict) -> Project:
    name = _str_arg(args, "project")
    row = db.execute(select(Project).where(Project.name == name)).scalar_one_or_none()
    if row is None:
        raise mcp_server.McpToolError(
            f"프로젝트를 찾을 수 없습니다: {name} (list_routes로 이름을 확인하세요)")
    return row


def _ops_call(db: Session, name: str, args: dict) -> str:
    if name == "list_routes":
        # 서버구성 화면과 같은 함수를 쓴다 — 화면과 다른 값을 말하기 시작하면 둘 중
        # 어느 쪽이 사실인지 알 수 없게 된다.
        from .server import server_config  # noqa: PLC0415 — 순환 import 회피

        return _dump(server_config(db=db, _=None).model_dump())

    if name == "host_snapshot":
        return _dump(monitor.snapshot())

    if name == "list_ports":
        from ..services.runtime import upstream_host  # noqa: PLC0415

        return _dump(ports.usage(
            db, probe_host=upstream_host(get_settings()),
            probe_range=bool(args.get("probe_range")),
        ))

    if name == "search_audit":
        query = select(AuditEvent)
        if args.get("actor"):
            query = query.where(AuditEvent.actor == _str_arg(args, "actor"))
        if args.get("action"):
            query = query.where(AuditEvent.action.contains(_str_arg(args, "action")))
        if args.get("target"):
            query = query.where(AuditEvent.target == _str_arg(args, "target"))
        limit = _int_arg(args, "limit", 20, _MAX_LIST)
        events = db.execute(
            query.order_by(AuditEvent.id.desc()).limit(limit)).scalars().all()
        return _dump([
            {"actor": e.actor, "action": e.action, "target": e.target,
             "detail": e.detail, "created_at": e.created_at}
            for e in events
        ])

    project = _project_by_name(db, args)

    if name == "get_deploy_status":
        runtime = deployer.get_runtime()
        org_name = project.organization.name if project.organization else None
        # 프록시가 실제로 전달하는 곳(업스트림 포트)은 running 배포 행의 host_port다 —
        # internal_port는 컨테이너 내부 포트라 일반 프로젝트에는 채워지지도 않는다.
        # 이름을 running_ports로 둔다 — 모듈 상단의 ports(services/ports.py)를 가리면
        # 같은 함수의 list_ports 분기가 UnboundLocalError로 죽는다(실제로 그랬다).
        running_ports = {
            profile: port
            for profile, port in db.execute(
                select(Deployment.profile, Deployment.host_port).where(
                    Deployment.project_id == project.id,
                    Deployment.status == DeploymentStatus.running,
                    Deployment.component.is_(None),
                )
            ).all()
        }
        out = {"project": project.name, "type": project.type.value, "branch": project.branch,
               "profiles": {}}
        for profile in BuildProfile:
            latest = db.execute(
                select(Deployment)
                .where(Deployment.project_id == project.id, Deployment.profile == profile)
                .order_by(Deployment.id.desc())
            ).scalars().first()
            out["profiles"][profile.value] = {
                "status": _runtime_status(runtime, project, profile),
                "domain": domain_for(project.name, project.domain, profile),
                "path_prefix": path_prefix_for(org_name, project.name, project.domain, profile),
                "internal_port": running_ports.get(profile),
                "last_deployment": None if latest is None else {
                    "id": latest.id, "status": latest.status.value, "git_sha": latest.git_sha,
                    "created_at": latest.created_at, "finished_at": latest.finished_at,
                    "error": latest.error,
                },
            }
        return _dump(out)

    if name == "list_deployments":
        limit = _int_arg(args, "limit", 10, _MAX_LIST)
        rows = db.execute(
            select(Deployment)
            .where(Deployment.project_id == project.id)
            .order_by(Deployment.id.desc())
            .limit(limit)
        ).scalars().all()
        return _dump([
            {"id": d.id, "profile": d.profile.value, "status": d.status.value,
             "git_sha": d.git_sha, "component": d.component, "host_port": d.host_port,
             "created_at": d.created_at, "finished_at": d.finished_at, "error": d.error}
            for d in rows
        ])

    # tail_app_log
    profile = _profile_arg(args)
    tail = _int_arg(args, "tail", 100, _MAX_TAIL)
    runtime = deployer.get_runtime()
    if project.type == ProjectType.composite:
        return _dump({
            component: runtime.logs(f"{project.name}-{component}", profile, tail)
            for component in COMPOSITE_COMPONENTS
        })
    return runtime.logs(project.name, profile, tail)


def _runtime_status(runtime, project: Project, profile: BuildProfile) -> str:
    """런타임 미설치·미접근이 도구 호출 전체를 실패시키지 않게 감싼다(서버구성 화면과 동일)."""
    try:
        if project.type == ProjectType.composite:
            statuses = {
                runtime.status(f"{project.name}-{component}", profile)
                for component in COMPOSITE_COMPONENTS
            }
            return statuses.pop() if len(statuses) == 1 else "partial"
        return runtime.status(project.name, profile)
    except Exception as e:  # noqa: BLE001
        return f"unknown ({e})"


# --- 코드 조회 서버 (/mcp/projects/{id}/code) ---

_CODE_TOOLS = [
    {
        "name": "list_files",
        "description": "프로젝트 리포의 파일 경로 목록(워킹카피 기준).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_file",
        "description": "리포 파일 하나의 내용을 읽는다(경로는 리포 루트 기준 상대경로).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "read_file_at_ref",
        "description": "특정 브랜치·커밋(ref)의 파일 내용을 읽는다.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "ref": {"type": "string"}},
            "required": ["path", "ref"],
        },
    },
    {
        "name": "get_code_map",
        "description": "코드 구조 개요 — 파일 → 클래스/함수 계층(정적 파싱, LLM 호출 없음).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


@router.post("/mcp/projects/{project_id}/code",
             dependencies=[Depends(require_feature("workspace"))])
async def code_mcp_server(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """프로젝트 코드 조회 MCP 서버(JSON-RPC 2.0) — 읽기 전용."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return mcp_server.dispatch(
        await mcp_server.read_payload(request),
        server_name=f"paas-code-{project.name}",
        tools=_CODE_TOOLS,
        call=lambda name, args: _code_call(project, name, args),
    )


def _code_workdir(project: Project) -> Path:
    """워크스페이스 워킹카피 — 있으면 그대로 쓰고, 없을 때만 체크아웃한다.

    도구 호출마다 git fetch를 돌리면(코드 확인 화면 api/llm.py는 그렇게 한다) LLM 턴이
    매번 네트워크를 기다린다. 최신화는 배포·기획 확정 경로가 이미 하므로 여기서는 있는
    것을 읽는다.
    """
    workdir = workspace.workdir_for(project)
    if workdir.exists():
        return workdir
    try:
        workdir, _sha = checkout(project)
    except BuildError as e:
        raise mcp_server.McpToolError(f"리포를 가져올 수 없습니다: {str(e)[:300]}")
    return workdir


def _code_call(project: Project, name: str, args: dict) -> str:
    workdir = _code_workdir(project)

    if name == "list_files":
        return "\n".join(workspace.file_tree(workdir))
    if name == "get_code_map":
        return _dump(codemap_service.build_code_map(workdir))
    if name == "read_file":
        try:
            return workspace.read_file(workdir, _str_arg(args, "path"))
        except FileNotFoundError:
            raise mcp_server.McpToolError(f"파일이 없습니다: {args.get('path')}")
        except ValueError as e:  # 크기 상한 초과
            raise mcp_server.McpToolError(str(e))

    # read_file_at_ref
    path = _str_arg(args, "path")
    ref = _str_arg(args, "ref")
    content = workspace.read_file_at_ref(workdir, ref, path)
    if content is None:
        raise mcp_server.McpToolError(f"{ref}에서 파일을 읽을 수 없습니다: {path}")
    return content


# --- 사내 문서 검색 서버 (/mcp/docs) ---

# 저장소를 가로지르는 색인 작업의 시간 예산(초). 한 번에 끝내지 않고 남은 개수를 돌려주는
# 이유는 services/docsearch.reindex와 같다 — MCP 클라이언트의 요청 타임아웃이 30초다.
_DOCS_REINDEX_BUDGET = 20.0

_DOCS_TOOLS = [
    {
        "name": "list_sources",
        "description": (
            "검색 대상 문서 저장소 목록과 각 색인 상태. 저장소 이름을 몰라도 되지만,"
            " 범위를 좁히고 싶을 때 여기서 이름을 얻는다."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_docs",
        "description": (
            "사내 문서 **본문**을 검색한다(파일명이 아니다). 공백으로 끊은 낱말을 모두"
            " 포함하는 문서를 찾아 어느 저장소의 어느 경로인지와 일치 대목 발췌를 준다."
            " source를 주면 그 저장소만, 생략하면 전 저장소를 가로질러 찾는다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source": {"type": "string", "description": "저장소 이름(생략 = 전체)"},
                "limit": {"type": "integer", "description": f"기본 10, 최대 {_MAX_LIST}"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_doc",
        "description": (
            "검색 결과의 문서 본문을 읽는다. pdf·docx·xlsx·pptx는 본문 텍스트를 추출해서"
            " 준다(원본 바이트가 아니다)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"source": {"type": "string"}, "path": {"type": "string"}},
            "required": ["source", "path"],
        },
    },
    {
        "name": "reindex_docs",
        "description": (
            "바뀐 문서를 다시 추출해 색인을 갱신한다. source를 생략하면 전 저장소를 돈다."
            " 한 번에 정해진 시간만 진행하므로 done이 false면 다시 호출한다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "force": {"type": "boolean", "description": "바뀌지 않은 문서도 다시 추출"},
            },
        },
    },
    {
        "name": "index_status",
        "description": (
            "색인 커버리지 — 저장소별로 확장자별 성공·실패 건수와 실패 이유."
            " 검색 결과가 비었을 때 색인 문제인지 질의 문제인지 여기서 갈린다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"source": {"type": "string"}},
        },
    },
]


@router.post("/mcp/docs")
async def docs_mcp_server(
    request: Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """사내 문서 검색 MCP 서버(JSON-RPC 2.0) — 읽기 전용.

    대상은 등록된 file_storage 모듈 전부다. /mcp/storage/{module}에도 같은 검색이 있지만
    그쪽은 저장소 하나를 다루는 도구 묶음이고, 여기는 **저장소 이름을 모르는 쪽**이 부르는
    창구다 — 전 저장소를 가로질러 찾고 결과에 어느 저장소인지를 실어 준다.

    쓰기는 노출하지 않는다. 문서를 찾으러 붙는 서버라 고칠 일이 없고, 고쳐야 하면 그
    저장소의 /mcp/storage/{module}로 간다(거기서도 read_only면 막힌다).
    """
    return mcp_server.dispatch(
        await mcp_server.read_payload(request),
        server_name="paas-docs",
        tools=_DOCS_TOOLS,
        call=lambda name, args: _docs_call(db, key.name, name, args),
    )


def _doc_sources(db: Session) -> list[Module]:
    return list(db.execute(
        select(Module).where(Module.type == ModuleType.file_storage).order_by(Module.name)
    ).scalars())


def _doc_source(db: Session, name: str) -> Module:
    module = db.execute(
        select(Module).where(Module.name == name, Module.type == ModuleType.file_storage)
    ).scalar_one_or_none()
    if module is None:
        raise mcp_server.McpToolError(
            f"문서 저장소를 찾을 수 없습니다: {name} (list_sources로 이름을 확인하세요)")
    return module


def _docs_call(db: Session, actor: str, name: str, args: dict) -> str:
    source_name = _str_arg(args, "source", required=False)
    sources = [_doc_source(db, source_name)] if source_name else _doc_sources(db)
    if not sources:
        raise mcp_server.McpToolError(
            "등록된 문서 저장소가 없습니다 — file_storage 모듈을 먼저 등록하세요"
            "(사내 문서 폴더라면 config.endpoint에 절대 경로, read_only: true).")

    if name == "list_sources":
        return _dump([
            {"source": module.name,
             "read_only": _read_only(module),
             "index": {k: v for k, v in docsearch.status(module.name).items()
                       if k in ("total", "indexed", "failed")}}
            for module in sources
        ])

    if name == "index_status":
        return _dump({module.name: docsearch.status(module.name) for module in sources})

    if name == "reindex_docs":
        return _dump(_reindex_sources(db, actor, sources, force=bool(args.get("force"))))

    if name == "read_doc":
        if not source_name:
            raise mcp_server.McpToolError("source가 비어 있습니다(검색 결과의 source 값).")
        return _read_document(db, actor, sources[0], _str_arg(args, "path"))

    # search_docs — 저장소를 가로질러 한 번에
    query = _str_arg(args, "query")
    limit = _int_arg(args, "limit", 10, _MAX_LIST)
    hits: list[dict] = []
    truncated = False
    for module in sources:
        if len(hits) >= limit:
            truncated = True
            break
        found = docsearch.search(module.name, query, limit - len(hits))
        truncated = truncated or found["truncated"]
        hits += [{"source": module.name, **hit} for hit in found["hits"]]

    result = {"query": query, "hits": hits, "truncated": truncated,
              "searched": [module.name for module in sources]}
    if not hits:
        # 색인이 비어 있는 것과 "찾지 못한 것"은 다른 문제다 — 구분해서 알려 준다.
        coverage = {m.name: docsearch.status(m.name) for m in sources}
        if all(c["total"] == 0 for c in coverage.values()):
            raise mcp_server.McpToolError(
                "색인이 비어 있습니다 — reindex_docs를 먼저 실행하세요.")
        result["index"] = {n: {"indexed": c["indexed"], "failed": c["failed"]}
                           for n, c in coverage.items()}
    audit.record(db, actor, "mcp.docs.search", source_name or "*",
                 {"query": query, "hits": len(hits), "sources": len(sources)})
    return _dump(result)


def _reindex_sources(db: Session, actor: str, sources: list[Module], *, force: bool) -> dict:
    """저장소들을 한 예산 안에서 순서대로 색인한다.

    예산을 나눠 주지 않고 남은 만큼 넘기는 이유: 앞 저장소가 이미 최신이면 거의 시간을
    쓰지 않으므로, 나눠 주면 뒤 저장소가 쓸 수 있는 시간을 그냥 버리게 된다.
    """
    import time  # noqa: PLC0415

    started = time.monotonic()
    per_source = {}
    for module in sources:
        remaining = _DOCS_REINDEX_BUDGET - (time.monotonic() - started)
        result = docsearch.reindex(
            module.name, storage_service.root_for(module),
            force=force, budget_seconds=max(0.0, remaining),
        )
        audit.record(db, actor, "mcp.docs.reindex", module.name, result)
        per_source[module.name] = result
    return {
        "sources": per_source,
        "done": all(r["done"] for r in per_source.values()),
        "remaining": sum(r["remaining"] for r in per_source.values()),
    }


def _read_document(db: Session, actor: str, module: Module, path: str) -> str:
    """저장소 파일 하나를 본문 텍스트로 — /mcp/docs와 /mcp/storage가 같은 규칙을 쓴다."""
    try:
        target = storage_service.resolve(storage_service.root_for(module), path)
    except storage_service.StorageError as e:
        raise mcp_server.McpToolError(str(e))
    if not target.is_file():
        raise mcp_server.McpToolError(f"파일이 없습니다: {path}")
    try:
        text = doctext.extract_text(target)
    except doctext.ExtractError as e:
        raise mcp_server.McpToolError(str(e))
    audit.record(db, actor, "mcp.storage.read", module.name, {"path": path})
    return _truncate(text)


# --- 파일 저장소 서버 (/mcp/storage/{module}) ---

_STORAGE_READ_TOOLS = [
    {
        "name": "list_files",
        "description": (
            "이 저장소 모듈의 파일 목록(경로·크기). glob으로 걸러 낸다(예: **/*.pdf,"
            f" 규정*). 기본 {_MAX_LIST}건, 최대 {_MAX_FILE_LIST}건이며 잘리면 그렇다고 알린다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "glob": {"type": "string", "description": "경로 패턴(생략하면 전체)"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "read_file",
        "description": (
            "저장소 파일 하나를 텍스트로 읽는다. pdf·docx·xlsx·pptx는 본문 텍스트를"
            " 추출해서 준다(원본 바이트가 아니다)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "search_docs",
        "description": (
            "문서 **본문**을 검색한다(파일명이 아니다). 공백으로 끊은 낱말을 모두 포함하는"
            " 문서를 찾아 경로와 일치 대목 발췌를 준다. 색인이 비어 있으면 reindex_docs를"
            " 먼저 부른다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": f"기본 10, 최대 {_MAX_LIST}"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "reindex_docs",
        "description": (
            "바뀐 문서를 다시 추출해 검색 색인을 갱신한다. 한 번에 정해진 시간만 진행하므로"
            " 응답의 done이 false면 remaining이 0이 될 때까지 다시 호출한다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "force": {"type": "boolean", "description": "바뀌지 않은 문서도 다시 추출"},
            },
        },
    },
    {
        "name": "index_status",
        "description": (
            "검색 색인 커버리지 — 확장자별로 몇 건이 읽혔고 못 읽은 것은 왜인지."
            " 검색 결과가 비어 있을 때 색인 문제인지 질의 문제인지 여기서 갈린다."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_STORAGE_WRITE_TOOLS = [
    {
        "name": "write_file",
        "description": "저장소에 텍스트 파일을 쓴다(같은 경로면 덮어쓴다).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "delete_file",
        "description": "저장소 파일 하나를 지운다.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]


@router.post("/mcp/storage/{module_name}")
async def storage_mcp_server(
    module_name: str,
    request: Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """file_storage 모듈 파일 MCP 서버(JSON-RPC 2.0).

    경로는 storage.resolve가 모듈 루트 안으로 가둔다 — 이 서버의 존재 이유가 그
    가둠이다(공개 filesystem MCP 서버는 호스트 디스크를 그대로 연다).

    모듈 config에 `read_only: true`면 쓰기·삭제 도구를 아예 광고하지 않는다. 사내 문서
    공유 폴더처럼 플랫폼이 만든 것이 아닌 디렉터리를 붙일 때 쓴다 — 목록에 없는 도구는
    모델이 부르지도 않고, 불러도 unknown tool로 막힌다.
    """
    module = _typed_module(db, module_name, ModuleType.file_storage)
    tools = list(_STORAGE_READ_TOOLS)
    if not _read_only(module):
        tools += _STORAGE_WRITE_TOOLS
    return mcp_server.dispatch(
        await mcp_server.read_payload(request),
        server_name=f"paas-storage-{module.name}",
        tools=tools,
        call=lambda name, args: _storage_call(db, key.name, module, name, args),
    )


def _read_only(module: Module) -> bool:
    return bool((module.config or {}).get("read_only"))


def _file_listing(root: Path, args: dict) -> dict:
    """glob으로 걸러 상한까지만. 사내 문서 폴더는 파일이 수천 개라 전체 목록은 컨텍스트를
    통째로 먹는다 — 잘랐으면 잘랐다고 말해야 모델이 패턴을 좁힌다.

    패턴은 전체 경로와 파일명 양쪽에 맞춰 본다(`규정*`이 하위 폴더 파일에도 걸리도록).
    대소문자는 무시한다 — .PDF와 .pdf가 섞여 있는 폴더가 흔하다.
    """
    pattern = str(args.get("glob") or "").strip().lower()
    limit = _int_arg(args, "limit", _MAX_LIST, _MAX_FILE_LIST)
    files = storage_service.list_files(root)
    if pattern:
        files = [
            f for f in files
            if fnmatchcase(f["path"].lower(), pattern)
            or fnmatchcase(f["path"].lower().rsplit("/", 1)[-1], pattern)
        ]
    return {
        "total": len(files),
        "truncated": len(files) > limit,
        "files": files[:limit],
    }


def _typed_module(db: Session, module_name: str, expected: ModuleType) -> Module:
    row = db.execute(select(Module).where(Module.name == module_name)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"module '{module_name}' not found")
    if row.type != expected:
        raise HTTPException(
            status_code=400,
            detail=f"module '{module_name}' is not a {expected.value} module",
        )
    return row


def _storage_call(db: Session, actor: str, module: Module, name: str, args: dict) -> str:
    root = storage_service.root_for(module)

    if name == "list_files":
        return _dump(_file_listing(root, args))

    if name == "index_status":
        return _dump(docsearch.status(module.name))

    if name == "reindex_docs":
        result = docsearch.reindex(module.name, root, force=bool(args.get("force")))
        audit.record(db, actor, "mcp.docs.reindex", module.name, result)
        return _dump(result)

    if name == "search_docs":
        query = _str_arg(args, "query")
        limit = _int_arg(args, "limit", 10, _MAX_LIST)
        result = docsearch.search(module.name, query, limit)
        if not result["hits"]:
            # 색인이 비어 있는 것과 "찾지 못한 것"은 다른 문제다 — 구분해서 알려 준다.
            indexed = docsearch.status(module.name)
            if indexed["total"] == 0:
                raise mcp_server.McpToolError(
                    "색인이 비어 있습니다 — reindex_docs를 먼저 실행하세요.")
            result["index"] = {"indexed": indexed["indexed"], "failed": indexed["failed"]}
        audit.record(db, actor, "mcp.docs.search", module.name,
                     {"query": query, "hits": len(result["hits"])})
        return _dump(result)

    path = _str_arg(args, "path")
    try:
        target = storage_service.resolve(root, path)
    except storage_service.StorageError as e:
        raise mcp_server.McpToolError(str(e))

    if name == "read_file":
        return _read_document(db, actor, module, path)

    if name == "write_file":
        content = str(args.get("content") or "")
        try:
            saved = storage_service.write_file(root, path, content.encode("utf-8"))
        except storage_service.StorageError as e:
            raise mcp_server.McpToolError(str(e))
        audit.record(db, actor, "mcp.storage.write", module.name,
                     {"path": saved, "bytes": len(content.encode("utf-8"))})
        return f"wrote {saved}"

    # delete_file
    try:
        storage_service.delete_file(root, path)
    except storage_service.StorageError as e:
        raise mcp_server.McpToolError(str(e))
    except FileNotFoundError:
        raise mcp_server.McpToolError(f"파일이 없습니다: {path}")
    audit.record(db, actor, "mcp.storage.delete", module.name, {"path": path})
    return f"deleted {path}"


# --- DB 조회 서버 (/mcp/db/{module}) ---

_DB_TOOLS = [
    {
        "name": "list_tables",
        "description": "이 데이터베이스의 테이블 이름 목록.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "describe_table",
        "description": "테이블의 컬럼(이름·타입·NULL 허용)과 기본키를 반환한다.",
        "inputSchema": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
        },
    },
    {
        "name": "run_select",
        "description": (
            "SELECT 문 하나를 실행해 행을 반환한다. 쓰기·DDL·다중 문장은 거부되고,"
            f" 행 수는 최대 {_MAX_ROWS}행으로 잘린다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "limit": {"type": "integer", "description": f"기본 50, 최대 {_MAX_ROWS}"},
            },
            "required": ["sql"],
        },
    },
]

# SQL 주석 — 키워드 검사를 우회하는 데 쓰이므로 먼저 지운다.
_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
# 쓰기·부작용 키워드. 문자열 리터럴 안에 우연히 들어 있으면 오탐이지만, 이 서버는
# 조회용이므로 거부하는 쪽으로 기운다(거부 이유를 그대로 알려 준다).
_SQL_WRITE_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do|merge"
    r"|vacuum|lock|set|commit|rollback|savepoint|into|attach|pragma|execute|prepare)\b",
    re.I,
)


@router.post("/mcp/db/{module_name}")
async def db_mcp_server(
    module_name: str,
    request: Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """database 모듈 조회 MCP 서버(JSON-RPC 2.0) — SELECT 전용.

    허용 목록(PAAS_MCP_DB_MODULES)에 이름이 있는 모듈만 연다. 기본값은 빈 목록이라
    아무 것도 열리지 않는다 — 어떤 DB를 LLM에게 읽히는지는 등록만으로 정해질 일이
    아니라 명시적으로 고를 일이다.
    """
    allowed = {
        n.strip() for n in get_settings().mcp_db_modules.split(",") if n.strip()
    }
    if module_name not in allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                f"'{module_name}'은 MCP 조회 허용 목록에 없습니다 — "
                "PAAS_MCP_DB_MODULES에 추가하세요."
            ),
        )
    module = _typed_module(db, module_name, ModuleType.database)
    return mcp_server.dispatch(
        await mcp_server.read_payload(request),
        server_name=f"paas-db-{module.name}",
        tools=_DB_TOOLS,
        call=lambda name, args: _db_call(db, key.name, module, name, args),
    )


def _require_select_only(sql: str) -> str:
    """SELECT 한 문장인지 확인한다 — 통과하지 못하면 실행하지 않는다."""
    body = _SQL_COMMENT_RE.sub(" ", sql).strip().rstrip(";").strip()
    if not body:
        raise mcp_server.McpToolError("sql이 비어 있습니다.")
    if ";" in body:
        raise mcp_server.McpToolError("한 번에 한 문장만 실행합니다(; 를 제거하세요).")
    if not re.match(r"(select|with)\b", body, re.I):
        raise mcp_server.McpToolError("SELECT(또는 WITH … SELECT)만 실행할 수 있습니다.")
    hit = _SQL_WRITE_RE.search(body)
    if hit:
        raise mcp_server.McpToolError(f"쓰기·부작용 키워드가 있어 거부했습니다: {hit.group(1)}")
    return body


def _db_engine(module: Module):
    """모듈에 등록된 DSN으로 엔진을 만든다 — 호출마다 만들고 쓰고 버린다(NullPool).

    드라이버(psycopg 등)는 선택 의존성이다. 없으면 무엇을 설치해야 하는지 알려 준다.
    """
    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    dsn = modules_service.decrypt_config(module.config or {}).get("dsn", "")
    if not dsn:
        raise mcp_server.McpToolError(f"모듈 '{module.name}'에 dsn이 없습니다.")
    try:
        return create_engine(dsn, poolclass=NullPool)
    except ModuleNotFoundError as e:
        raise mcp_server.McpToolError(
            f"DB 드라이버가 설치되지 않았습니다({e}). 예: pip install 'psycopg[binary]'")
    except Exception as e:  # noqa: BLE001 — 잘못된 DSN 등
        raise mcp_server.McpToolError(f"DSN으로 연결할 수 없습니다: {str(e)[:200]}")


def _db_call(db: Session, actor: str, module: Module, name: str, args: dict) -> str:
    engine = _db_engine(module)
    try:
        if name == "list_tables":
            return _dump(sorted(sa_inspect(engine).get_table_names()))

        if name == "describe_table":
            table = _str_arg(args, "table")
            inspector = sa_inspect(engine)
            if table not in inspector.get_table_names():
                raise mcp_server.McpToolError(f"테이블이 없습니다: {table}")
            return _dump({
                "table": table,
                "columns": [
                    {"name": c["name"], "type": str(c["type"]), "nullable": c.get("nullable")}
                    for c in inspector.get_columns(table)
                ],
                "primary_key": inspector.get_pk_constraint(table).get("constrained_columns", []),
            })

        # run_select
        sql = _require_select_only(_str_arg(args, "sql"))
        limit = _int_arg(args, "limit", 50, _MAX_ROWS)
        try:
            with engine.connect() as conn:
                # 파서 검사를 통과한 뒤의 이중 방어 — 지원하는 DB에서는 트랜잭션 자체가
                # 쓰기를 거부한다(지원하지 않는 DB는 조용히 넘어간다).
                try:
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                except Exception:  # noqa: BLE001
                    pass
                result = conn.exec_driver_sql(sql)
                columns = list(result.keys())
                rows = [dict(zip(columns, row)) for row in result.fetchmany(limit)]
        except mcp_server.McpToolError:
            raise
        except Exception as e:  # noqa: BLE001 — SQL 오류는 대화로 돌려주면 모델이 고친다
            raise mcp_server.McpToolError(f"실행 실패: {str(e)[:500]}")
        audit.record(db, actor, "mcp.db.select", module.name,
                     {"sql": sql[:1000], "rows": len(rows)})
        return _dump({"columns": columns, "row_count": len(rows), "rows": rows,
                      "truncated": len(rows) == limit})
    finally:
        engine.dispose()
