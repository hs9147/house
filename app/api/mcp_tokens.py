"""개인 MCP 토큰 — 외주 개발 에이전트가 MCP 서버에 붙을 때 쓰는 자격증명.

외주 에이전트에게는 API 키가 없다. 관리자가 키를 나눠 주는 것도 답이 아니다 — 누구에게
나갔는지·언제 회수하는지가 남지 않고 범위도 좁힐 수 없다. 그래서 **SSO로 로그인한 사람이
자기 몫을 직접 발급한다**: 발급 주체가 요청자 자신이고(남의 토큰을 만들 수 없다), 그 사람의
조직 권한으로 프로젝트 접근이 판정된다(security.require_project_mcp_access).

원문은 발급 응답에 **한 번만** 실린다. 다시 볼 수 없고(해시만 저장한다), 잃으면 새로
발급받아 쓰던 것을 폐기한다 — 그 편이 "어딘가에 적어 둔 값을 다시 보여 주는" 것보다 안전하다.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, McpToken, Project, utcnow
from ..schemas import McpTokenCreate, McpTokenIssued, McpTokenOut
from ..security import (
    MCP_TOKEN_TTL,
    issue_mcp_token,
    require_api_key,
    require_project_mcp_access,
)
from ..services import mcp_search

router = APIRouter(tags=["mcp"])


def _out(row: McpToken) -> McpTokenOut:
    return McpTokenOut(
        id=row.id, label=row.label, created_at=row.created_at, expires_at=row.expires_at,
        last_used_at=row.last_used_at, is_expired=_expired(row),
    )


def _expired(row: McpToken) -> bool:
    from datetime import timezone  # noqa: PLC0415

    expires = row.expires_at
    if expires.tzinfo is None:  # SQLite는 tz를 보존하지 않음
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= utcnow()


@router.post("/mcp/tokens", response_model=McpTokenIssued, status_code=201)
def create_mcp_token(
    body: McpTokenCreate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """자기 몫의 MCP 토큰을 발급한다 — 남의 것은 만들 수 없다(주체가 요청자 자신이다).

    project를 주면 그 프로젝트의 MCP 주소까지 함께 돌려준다. 붙여 넣을 설정을 화면이 바로
    만들어 줄 수 있어야 한다 — 주소와 토큰을 따로 찾아 조립하게 두면 거기서 틀린다.
    """
    token = issue_mcp_token(db, key.name, body.label, key.is_admin)
    url = ""
    if body.project:
        project = db.execute(
            select(Project).where(Project.name == body.project)
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail=f"project not found: {body.project}")
        # 발급 시점에 권한을 확인한다 — 쓸 수 없는 주소를 돌려주면 붙여 넣고 나서 401을 본다.
        require_project_mcp_access(db, key, project)
        base = mcp_search.internal_base_url()
        url = f"{base}/api/v1/plan/projects/{project.id}/mcp" if base else ""
    audit.record(db, key.name, "mcp.token.issue", body.label or "(라벨 없음)",
                 {"project": body.project or None, "ttl_days": MCP_TOKEN_TTL.days})
    return McpTokenIssued(token=token, url=url, ttl_days=MCP_TOKEN_TTL.days)


@router.get("/mcp/tokens", response_model=list[McpTokenOut])
def list_mcp_tokens(db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    """자기 토큰만 보인다 — 남의 토큰 목록은 폐기 대상을 고르는 데 필요하지 않다."""
    rows = db.execute(
        select(McpToken).where(McpToken.email == key.name).order_by(McpToken.id.desc())
    ).scalars()
    return [_out(r) for r in rows]


@router.delete("/mcp/tokens/{token_id}", status_code=204)
def revoke_mcp_token(
    token_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """폐기. 남의 토큰은 **없는 것으로** 답한다(404) — 존재 여부를 알려 주지 않는다."""
    row = db.get(McpToken, token_id)
    if row is None or row.email != key.name:
        raise HTTPException(status_code=404, detail="token not found")
    label = row.label
    db.delete(row)
    db.commit()
    audit.record(db, key.name, "mcp.token.revoke", label or str(token_id), {"id": token_id})
    return None
