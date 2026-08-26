import hmac
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Response, WebSocket
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..models import ApiKey, AuditEvent, EnvVar, LlmProvider, Module, Organization, UserAccount, UserOrganization, UserSession, utcnow
from ..schemas import (
    ApiKeyCreate,
    ApiKeyIssued,
    UserAccountOut,
    UserAccountOrganizationUpdate,
    UserAccountOrgModifyRequest,
    UserLoginOut,
    UserLoginRequest,
    UserOrgOut,
    UserRegisterOut,
    UserRegisterRequest,
)
from ..security import (
    hash_key,
    hash_password,
    issue_key,
    issue_session_token,
    require_admin,
    require_api_key,
    resolve_token,
    rotate_token,
    validate_email_domain,
    verify_password,
)
from ..services import gitea, monitor

# 헬스체크·상태 프로브는 버전 prefix 밖에 둔다(로드밸런서/k8s liveness probe, 콘솔 로그인
# 프로브가 API 버전과 무관하게 고정된 경로를 기대함) — router.py 참고.
health_router = APIRouter(tags=["system"])
router = APIRouter(tags=["system"])

# 로그인 세션 수명. 짧게 두고 재로그인시키는 편이, 새어 나간 토큰이 오래 사는 것보다 낫다.
SESSION_TTL = timedelta(hours=12)


def _start_session(db: Session, email: str, is_admin: bool) -> str:
    """난수 세션 토큰을 발급하고 해시만 저장한다 — 원문은 이 반환값이 유일하다."""
    token = issue_session_token()
    db.add(UserSession(
        token_hash=hash_key(token),
        email=email,
        is_admin=is_admin,
        expires_at=utcnow() + SESSION_TTL,
    ))
    db.commit()
    return token


@health_router.get("/health")
def health():
    from ..features import enabled_features  # noqa: PLC0415
    from ..services.host import get_host_caps  # noqa: PLC0415

    settings = get_settings()
    return {
        "ok": True,
        "platform_name": settings.platform_name,
        "allowed_email_domain": settings.allowed_email_domain,
        "tier": settings.tier,
        "host_os": get_host_caps().os,
        "features": sorted(enabled_features()),
        "gitea_url": settings.gitea_url or None,
        "base_domain": settings.base_domain,
        # 배포된 앱 주소를 만들 때 쓸 스킴. base_domain은 호스트만이라 스킴 정보가 없어
        # 콘솔이 https로 박아 두고 있었다 — 80포트만 여는 구성에서는 죽은 링크가 된다.
        # 공개 주소가 설정돼 있으면 그 스킴이 정답이고, 없으면 None(콘솔이 자기 자신이
        # 열린 스킴을 쓴다 — 같은 프록시 뒤에 있으므로 그게 맞다).
        "public_scheme": urlsplit(settings.platform_public_url).scheme or None,
    }


@health_router.get("/status")
def system_status(_: ApiKey = Depends(require_admin)):
    return monitor.snapshot()


@router.get("/auth/me")
def get_current_user_profile(
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    settings = get_settings()
    user = db.execute(select(UserAccount).where(UserAccount.email == key.name)).scalar_one_or_none()
    org_id = user.organization_id if user else None
    org_name = user.organization.name if (user and user.organization) else None
    org_list: list[dict] = []

    if user:
        try:
            if user.organizations:
                org_list = [{"id": o.id, "name": o.name} for o in user.organizations]
        except Exception:
            pass

        if not org_list and org_id and org_name:
            org_list = [{"id": org_id, "name": org_name}]

    return {
        "name": key.name,
        "is_admin": key.is_admin,
        "allowed_email_domain": settings.allowed_email_domain,
        "organization_id": org_id,
        "organization_name": org_name,
        "organizations": org_list,
    }


@router.post("/auth/register", response_model=UserRegisterOut, status_code=201)
def register_user_account(
    body: UserRegisterRequest,
    db: Session = Depends(get_db),
):
    settings = get_settings()
    email = body.email.strip()
    
    # 1. 계정 이메일 도메인 검증 (PAAS_ALLOWED_EMAIL_DOMAIN)
    if settings.allowed_email_domain and not validate_email_domain(email):
        allowed = settings.allowed_email_domain.replace("@", "")
        raise HTTPException(
            status_code=403,
            detail=f"@{allowed} 계정 이메일만 가입 가능합니다.",
        )

    # 2. 이미 존재하는 계정인지 중복 확인
    existing = db.execute(select(UserAccount).where(UserAccount.email == email)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="이미 등록된 계정 이메일입니다.")

    # 3. 비밀번호는 솔트 + scrypt로만 저장한다 — 원문도, 그 sha256도 남기지 않는다.
    password = body.password.strip()
    if not password:
        raise HTTPException(status_code=400, detail="비밀번호를 입력하세요.")
    db.add(UserAccount(
        email=email, name=body.name.strip(), password_hash=hash_password(password),
        is_approved=False, is_admin=False,
    ))
    db.commit()

    audit.record(db, email, "user.register", email, {"name": body.name})

    # 세션을 발급하지 않는다 — 관리자가 승인해야 로그인할 수 있다.
    return UserRegisterOut(name=body.name, email=email, is_approved=False, is_admin=False)


@router.post("/auth/login", response_model=UserLoginOut)
def login_user_account(
    body: UserLoginRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    settings = get_settings()
    email = body.email.strip()
    password = body.password.strip()

    # 1. 관리자 API 키 / 발급된 API 키로 로그인 — 난수 키라 그대로 쓴다(세션 불필요).
    if settings.admin_api_key and hmac.compare_digest(password, settings.admin_api_key):
        return UserLoginOut(name="bootstrap-admin", email=email or "admin@system", key=password, is_admin=True)
    key_row = db.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(password))).scalar_one_or_none()
    if key_row is not None:
        return UserLoginOut(name=key_row.name, email=email or key_row.name, key=password, is_admin=key_row.is_admin)

    # 2. 계정 이메일 도메인 검증
    if settings.allowed_email_domain and email and not validate_email_domain(email):
        allowed = settings.allowed_email_domain.replace("@", "")
        raise HTTPException(status_code=403, detail=f"@{allowed} 계정 이메일만 로그인 가능합니다.")

    # 3. 계정 비밀번호 검증 (IIS 팝업 방지를 위해 400 상태코드 사용).
    #
    # 계정이 없을 때도 같은 400을 돌려준다 — 어떤 이메일이 등록돼 있는지 알려주지 않는다.
    # 반환하는 key는 비밀번호에서 유도되지 않은 난수 세션 토큰이라, 새어도 폐기·만료된다.
    user = db.execute(select(UserAccount).where(UserAccount.email == email)).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=400, detail="이메일 또는 비밀번호가 올바르지 않습니다.")
    # 승인 여부는 비밀번호가 맞은 뒤에 본다 — 미승인 안내로 계정 존재를 흘리지 않는다.
    if not user.is_approved:
        raise HTTPException(status_code=403, detail="관리자 승인 대기 중인 계정입니다.")

    token = _start_session(db, user.email, user.is_admin)
    # 콘솔(SPA)은 이 토큰을 응답 본문의 key로 받아 x-api-key 헤더로 계속 쓴다. 쿠키로도
    # 같은 값을 심어 두는 건 OIDC Provider의 authorize 엔드포인트(services/oidc_provider.py)
    # 때문이다 — 그 엔드포인트는 Gitea 같은 외부 서비스의 리다이렉트로 열리는 일반 브라우저
    # 내비게이션이라 fetch 헤더가 아니라 쿠키로만 "이미 로그인돼 있음"을 알 수 있다.
    # samesite=lax여야 한다 — Gitea에서 시작한 SSO는 다른 사이트에서 이 도메인으로 오는
    # 최상위 GET 이동이라 strict면 쿠키가 안 실려 authorize가 매번 미로그인으로 보인다.
    # secure는 이 배포의 공개 주소가 https일 때만 — 로컬 http 개발에서 켜면 쿠키가 아예
    # 저장되지 않는다.
    response.set_cookie(
        "paas_session", token, httponly=True, samesite="lax",
        secure=settings.platform_public_url.startswith("https://"),
        max_age=int(SESSION_TTL.total_seconds()),
    )
    return UserLoginOut(
        name=user.name or user.email,
        email=user.email,
        key=token,
        is_admin=user.is_admin,
        organization_id=user.organization_id,
        organization_name=user.organization.name if user.organization else None,
    )


@router.post("/auth/logout", status_code=204)
def logout_user_session(
    response: Response,
    x_api_key: str = Header(default=""),
    db: Session = Depends(get_db),
):
    """세션 토큰을 서버에서 폐기한다. 브라우저 저장소만 비우면 토큰은 계속 유효하다."""
    if x_api_key:
        session = db.execute(
            select(UserSession).where(UserSession.token_hash == hash_key(x_api_key))
        ).scalar_one_or_none()
        if session is not None:
            db.delete(session)
            db.commit()
    response.delete_cookie("paas_session")
    return None


def _build_user_account_out(db: Session, u: UserAccount) -> UserAccountOut:
    org_out_list: list[UserOrgOut] = []
    try:
        if u.organizations:
            org_out_list = [UserOrgOut(id=o.id, name=o.name) for o in u.organizations]
    except Exception:
        pass

    if not org_out_list and u.organization_id and u.organization:
        org_out_list = [UserOrgOut(id=u.organization.id, name=u.organization.name)]

    primary_org_name = u.organization.name if u.organization else (org_out_list[0].name if org_out_list else None)

    return UserAccountOut(
        id=u.id,
        email=u.email,
        name=u.name,
        is_approved=u.is_approved,
        is_admin=u.is_admin,
        organization_id=u.organization_id or (org_out_list[0].id if org_out_list else None),
        organization_name=primary_org_name,
        organizations=org_out_list,
    )


@router.get("/auth/accounts", response_model=list[UserAccountOut])
def list_user_accounts(
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_admin),
):
    """계정 목록 — 승인 대기가 먼저 온다."""
    rows = db.execute(
        select(UserAccount).order_by(UserAccount.is_approved, UserAccount.created_at)
    ).scalars()
    return [_build_user_account_out(db, u) for u in rows]


@router.post("/auth/accounts/{account_id}/approve", response_model=UserAccountOut)
def approve_user_account(
    account_id: int,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    user = db.get(UserAccount, account_id)
    if user is None:
        raise HTTPException(status_code=404, detail="account not found")
    user.is_approved = True
    db.commit()
    audit.record(db, admin.name, "user.approve", user.email, {})
    return _build_user_account_out(db, user)


def _sync_gitea_membership(db: Session, actor: str, user: UserAccount, org: Organization, member: bool) -> None:
    """플랫폼 조직 소속을 Gitea 팀 소속으로 반영한다 — 개발자가 push하려면 필요하다.

    조직 소속만 바꾸고 Gitea를 그대로 두면 리포는 clone되는데 push만 403이 나서,
    증상만 봐서는 원인을 찾기 어렵다. 반대로 Gitea 반영이 실패했다고 플랫폼 쪽 소속
    변경까지 되돌리지는 않는다 — 소속 자체는 이미 커밋됐고, 실패는 감사 로그에 남겨
    관리자가 다시 시도할 수 있게 한다(사용자가 아직 Gitea에 로그인한 적이 없으면
    계정이 없어 반영할 대상 자체가 없다).
    """
    try:
        applied = gitea.set_org_membership(org.name, user.email, member, full_name=user.name or "")
    except (gitea.GiteaError, httpx.HTTPError) as e:
        # httpx도 함께 잡는다 — Gitea가 꺼져 있거나 주소가 틀리면 GiteaError가 아니라
        # ConnectError가 난다. 그걸 흘려보내면 이미 커밋된 배지 변경 뒤에 500이 나가서,
        # 관리자에게는 조작이 실패한 것처럼 보인다.
        audit.record(db, actor, "user.gitea_sync.failed", user.email,
                     {"organization": org.name, "member": member, "error": str(e)[:300]})
        return
    if not applied:
        # 소속을 뺄 때만 나올 수 있다 — 줄 때는 계정이 없으면 만들어서라도 붙인다.
        audit.record(db, actor, "user.gitea_sync.pending", user.email,
                     {"organization": org.name, "reason": "Gitea 계정 없음"})


@router.post("/auth/accounts/{account_id}/organization", response_model=UserAccountOut)
def update_user_account_organization(
    account_id: int,
    body: UserAccountOrganizationUpdate,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    user = db.get(UserAccount, account_id)
    if user is None:
        raise HTTPException(status_code=404, detail="account not found")
    
    added: Organization | None = None
    if body.organization_id is not None:
        org = db.get(Organization, body.organization_id)
        if org and org not in user.organizations:
            user.organizations.append(org)
            added = org
    user.organization_id = body.organization_id
    db.commit()
    db.refresh(user)
    audit.record(db, admin.name, "user.set_organization", user.email, {"organization_id": body.organization_id})
    if added is not None:
        _sync_gitea_membership(db, admin.name, user, added, member=True)
    return _build_user_account_out(db, user)


@router.post("/auth/accounts/{account_id}/organizations/modify", response_model=UserAccountOut)
def modify_user_account_organization(
    account_id: int,
    body: UserAccountOrgModifyRequest,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """사용자의 소속 조직 뱃지를 추가(add) 또는 삭제(remove)한다."""
    user = db.get(UserAccount, account_id)
    if user is None:
        raise HTTPException(status_code=404, detail="account not found")

    org = db.get(Organization, body.organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization not found")

    if body.action == "add":
        if org not in user.organizations:
            user.organizations.append(org)
        if user.organization_id is None:
            user.organization_id = org.id
    elif body.action == "remove":
        if org in user.organizations:
            user.organizations.remove(org)
        if user.organization_id == org.id:
            user.organization_id = user.organizations[0].id if user.organizations else None

    db.commit()
    db.refresh(user)
    audit.record(db, admin.name, f"user.org.{body.action}", user.email, {"organization_id": body.organization_id})
    _sync_gitea_membership(db, admin.name, user, org, member=body.action == "add")
    return _build_user_account_out(db, user)


@router.delete("/auth/accounts/{account_id}", status_code=204)
def reject_user_account(
    account_id: int,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """가입 거절 또는 계정 삭제. 이미 발급된 세션도 함께 폐기한다."""
    user = db.get(UserAccount, account_id)
    if user is None:
        raise HTTPException(status_code=404, detail="account not found")
    email = user.email
    for session in db.execute(select(UserSession).where(UserSession.email == email)).scalars():
        db.delete(session)
    db.delete(user)
    db.commit()
    audit.record(db, admin.name, "user.reject", email, {})
    return None


@router.post("/keys", response_model=ApiKeyIssued, status_code=201)
def create_key(
    body: ApiKeyCreate,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    raw = issue_key()
    db.add(ApiKey(name=body.name, key_hash=hash_key(raw), is_admin=body.is_admin))
    db.commit()
    audit.record(db, admin.name, "key.issue", body.name, {"is_admin": body.is_admin})
    return ApiKeyIssued(name=body.name, key=raw, is_admin=body.is_admin)


@router.post("/admin/rotate-secrets")
def rotate_secrets(
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """키 회전(후속2): 저장된 모든 암호문을 현행 Fernet 키로 재암호화.

    절차: 새 키를 PAAS_FERNET_KEY로, 기존 키를 PAAS_FERNET_KEYS_OLD로 옮겨 재기동한 뒤
    이 엔드포인트를 호출하고, 완료 후 구 키를 제거한다.
    """
    rotated = 0
    for row in db.execute(select(EnvVar)).scalars():
        row.value_encrypted = rotate_token(row.value_encrypted)
        rotated += 1
    for provider in db.execute(select(LlmProvider)).scalars():
        if provider.api_key_encrypted:
            provider.api_key_encrypted = rotate_token(provider.api_key_encrypted)
            rotated += 1
    for module in db.execute(select(Module)).scalars():
        config = dict(module.config or {})
        changed = False
        for k, v in config.items():
            if isinstance(v, dict) and "__enc__" in v:
                config[k] = {"__enc__": rotate_token(v["__enc__"])}
                changed = True
                rotated += 1
        if changed:
            module.config = config
    db.commit()
    audit.record(db, admin.name, "secrets.rotate", "all", {"rotated": rotated})
    return {"rotated": rotated}


@router.get("/audit")
def audit_log(
    limit: int = 100,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_admin),
):
    rows = db.execute(
        select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(limit, 500))
    ).scalars()
    return [
        {"actor": r.actor, "action": r.action, "target": r.target,
         "detail": r.detail, "at": r.created_at.isoformat()}
        for r in rows
    ]


@router.post("/system/powershell/exec")
def exec_powershell_cmd(
    body: dict,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """admin 전용 PowerShell 명령어 실행 API. 상주 공유 데몬을 사용해 호출 간 세션이 유지된다."""
    import os  # noqa: PLC0415
    from ..services import powershell_daemon  # noqa: PLC0415
    cmd = body.get("command", "").strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="command field is required")

    settings = get_settings()
    cwd_dir = os.path.abspath(settings.powershell_start_dir) if settings.powershell_start_dir else None

    try:
        daemon = powershell_daemon.shared_daemon(cwd=cwd_dir)
        result = daemon.run(cmd, timeout=30.0)
        audit.record(db, admin.name, "powershell.exec", cmd[:100], {"returncode": result.returncode})
        return {
            "command": cmd,
            "returncode": result.returncode,
            "output": result.output or "(no output)",
            "cwd": cwd_dir or os.getcwd(),
        }
    except TimeoutError:
        raise HTTPException(status_code=504, detail="PowerShell command execution timed out (30s)")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PowerShell execution failed: {e}")


# 브라우저는 WebSocket 핸드셰이크에 임의 헤더를 붙일 수 없다. 쿼리스트링으로 받으면
# IIS/ARR 접근 로그에 관리자 키가 그대로 남으므로, 표준으로 보낼 수 있는 자리인
# 서브프로토콜로 받는다: new WebSocket(url, ["paas-terminal", "paas-key." + key]).
# 서버는 비밀값이 아닌 "paas-terminal" 쪽을 골라 되돌려 준다.
WS_SUBPROTOCOL = "paas-terminal"
WS_KEY_PREFIX = "paas-key."
# 이보다 빨리 끝난 세션은 사용자가 나간 것이 아니라 셸이 뜨지 못한 것으로 본다.
SHORT_SESSION_SECONDS = 3.0


@router.get("/system/terminal/preflight")
def terminal_preflight(_: ApiKey = Depends(require_admin)):
    """터미널이 안 열릴 때 원인을 서버 쪽에서 먼저 가른다.

    WebSocket 핸드셰이크가 프록시에 막히면 브라우저는 이유를 알려주지 않는다 — 닫힘
    코드 1006 하나뿐이고, 그건 "서버가 PTY를 못 연다"와 "IIS가 업그레이드를 안 넘긴다"를
    구분하지 못한다. 여기가 ok인데 소켓이 안 열리면 원인은 서버가 아니라 그 사이다
    (IIS의 WebSocket Protocol 기능이 꺼져 있는 경우가 대부분).
    """
    from ..services import pty_terminal  # noqa: PLC0415

    settings = get_settings()
    result = pty_terminal.probe(settings.pty_shell, settings.pty_backend)
    if result["ok"]:
        hint = ("서버는 준비됐습니다. 그래도 터미널이 안 열리면 infra/ws-check.ps1로"
                " 백엔드에 직접 붙어 보세요 — 거기서 404면 서버가 --ws none으로 떠 있는"
                " 것이고, 거기는 되는데 IIS 경유가 안 되면 프록시가 업그레이드를 넘기지"
                " 않는 것입니다(Install-WindowsFeature Web-WebSockets 후 iisreset).")
    elif not result["websocket_library"]:
        # 여기가 비면 IIS를 아무리 고쳐도 안 된다 — 소켓이 서버에 닿아도 404가 난다.
        hint = ("IIS보다 여기가 먼저입니다. 이 상태에서는 프록시 설정과 무관하게"
                " 터미널 소켓이 404로 떨어집니다.")
    else:
        hint = ("이 서버에서 셸을 열지 못했습니다. Windows Server 2016은 ConPTY가 없으므로"
                " PAAS_PTY_BACKEND=winpty를 지정해 보세요.")
    result["hint"] = hint
    return result


@router.websocket("/system/powershell/ws")
async def powershell_websocket_terminal(
    websocket: WebSocket,
    db: Session = Depends(get_db),
):
    """admin 전용 실시간 터미널 — 셸을 PTY에 붙여 바이트를 그대로 중계한다.

    예전에는 명령 한 줄을 받아 끝날 때까지 기다렸다가 출력을 통째로 돌려줬다. 그래서
    되묻는 명령(Read-Host, git commit, python REPL)이 멈추고, Ctrl+C가 없고, 30초를 넘는
    작업은 진행 상황을 볼 수 없었다. PTY로 바꾸면 그 셋이 한 번에 풀린다
    (services/pty_terminal.py — Server 2016은 ConPTY가 없어 winpty 백엔드를 쓴다).

    **인증은 accept 전에 끝낸다.** 셸을 열어 두고 나중에 확인하면 이미 늦다 — 이 엔드포인트는
    관리자 셸을 그대로 내주므로 판정은 REST와 같은 경로(security.resolve_token)를 쓴다.

    프로토콜: 클라이언트→서버는 JSON({"type":"input"|"resize"}), 서버→클라이언트는 터미널
    출력 그대로. 입력을 JSON으로 감싸는 이유는 키 입력과 창 크기 변경을 구분해야 하는데,
    키 입력에는 어떤 바이트든 올 수 있어 구분자를 둘 자리가 없기 때문이다.
    """
    import asyncio  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    from ..services import pty_terminal  # noqa: PLC0415

    offered = [p.strip() for p in
               websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    token = next((p[len(WS_KEY_PREFIX):] for p in offered if p.startswith(WS_KEY_PREFIX)), "")
    key = resolve_token(db, token) if token else None
    if key is None or not key.is_admin:
        await websocket.close(code=1008)  # policy violation
        return

    await websocket.accept(subprotocol=WS_SUBPROTOCOL if WS_SUBPROTOCOL in offered else None)
    settings = get_settings()
    cwd_dir = os.path.abspath(settings.powershell_start_dir) if settings.powershell_start_dir else None

    try:
        terminal = await asyncio.to_thread(
            pty_terminal.PtyTerminal,
            [settings.pty_shell],
            cwd=cwd_dir,
            backend=pty_terminal.backend_code(settings.pty_backend),
        )
    except pty_terminal.PtyUnavailable as e:
        # 빈 화면만 남기지 않는다 — 무엇을 하면 되는지 터미널에 그대로 찍어 준다.
        await websocket.send_text(f"\r\n[터미널을 열 수 없습니다] {e}\r\n")
        await websocket.close()
        return

    audit.record(db, key.name, "powershell.ws_open", "terminal",
                 {"shell": settings.pty_shell, "backend": settings.pty_backend or "auto"})

    async def pump_output() -> None:
        """PTY 읽기는 블로킹이라 스레드에서 돌린다 — 이벤트 루프를 잡으면 입력이 막힌다."""
        while True:
            chunk = await asyncio.to_thread(terminal.read)
            if not chunk:
                break
            await websocket.send_text(chunk)

    async def pump_input() -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                message = _json.loads(raw)
            except ValueError:
                continue  # 규약에 없는 프레임은 무시한다(셸에 흘려보내지 않는다)
            kind = message.get("type")
            if kind == "input":
                terminal.write(str(message.get("data", "")))
            elif kind == "resize":
                try:
                    terminal.resize(int(message["cols"]), int(message["rows"]))
                except (KeyError, TypeError, ValueError):
                    pass

    output_task = asyncio.create_task(pump_output())
    input_task = asyncio.create_task(pump_input())
    opened_at = time.monotonic()
    try:
        # 둘 중 하나라도 끝나면 세션이 끝난 것이다(셸 종료 또는 연결 끊김).
        done, _ = await asyncio.wait({output_task, input_task},
                                     return_when=asyncio.FIRST_COMPLETED)
        # 열자마자 끝났으면 사용자가 나간 것이 아니라 **셸이 뜨지 못한 것**이다. 둘을
        # 구분하지 않으면 화면에는 "세션이 끝났습니다"만 남아서, 서버는 이유(종료코드)를
        # 알면서도 말해 주지 않는 꼴이 된다 — Server 2016에서 ConPTY가 자동 선택돼 셸이
        # 즉시 죽는 것이 이 기능의 주된 실패 모양이라 그대로 두면 원인을 찾을 수 없다.
        # 출력 펌프가 먼저 끝났다 = 셸이 죽었다. 입력 펌프가 먼저면 브라우저가 나간
        # 것이라 보낼 곳이 이미 없다.
        alive_seconds = time.monotonic() - opened_at
        status = terminal.exit_status()
        if output_task in done and status is not None and alive_seconds < SHORT_SESSION_SECONDS:
            await websocket.send_text(
                f"\r\n[셸이 바로 종료했습니다 — 종료코드 {status}] "
                f"'{settings.pty_shell}'을(를) 실행할 수 없습니다."
                " Windows Server 2016은 ConPTY가 없으므로 PAAS_PTY_BACKEND=winpty를"
                " 지정해 보세요(현재 백엔드: "
                f"{settings.pty_backend or 'auto'}).\r\n"
            )
    finally:
        # **셸을 먼저 끝낸다.** 출력 펌프는 블로킹 읽기 안에 있어서 cancel로는 깨울 수
        # 없고, 셸이 죽어야 그 읽기가 돌아온다 — 순서를 뒤집으면 출력이 없는 채로
        # 끊긴 세션에서 스레드가 그대로 묶인다. close는 kill·waitpid뿐이라 짧다.
        terminal.close()
        for task in (output_task, input_task):
            task.cancel()
        # 연결이 끊기면 셸도 끝낸다 — 남겨 두면 셸 프로세스가 계속 쌓인다.


def _server_log_dir():
    """서버 로그 폴더 — 플랫폼 실행 경로 하위의 logs/.

    배포 빌드 로그(PAAS_BUILD_LOG_DIR)와는 다른 것이다. 그쪽은 배포 레코드마다
    자기 로그를 따로 보여준다(api/projects.py의 deployment_build_log).
    """
    from pathlib import Path  # noqa: PLC0415

    return (get_settings().resolved_repo_root / "logs").resolve()


@router.get("/system/server-logs")
def list_server_log_files(
    _: ApiKey = Depends(require_admin),
):
    """실행 경로 하위 logs/의 .txt 서버 로그 파일 목록을 최신순으로 반환한다."""
    try:
        log_dir = _server_log_dir()
        if not log_dir.exists():
            log_dir.mkdir(parents=True, exist_ok=True)
            return {"files": [], "log_dir": str(log_dir)}

        txt_files = []
        for entry in log_dir.glob("**/*.txt"):
            try:
                if entry.is_file():
                    stat = entry.stat()
                    rel_path = entry.relative_to(log_dir).as_posix()
                    txt_files.append({
                        "filename": entry.name,
                        "relative_path": rel_path,
                        "size_bytes": stat.st_size,
                        "mtime": stat.st_mtime,
                    })
            except Exception:
                continue

        txt_files.sort(key=lambda x: x["mtime"], reverse=True)
        return {"files": txt_files, "log_dir": str(log_dir)}
    except Exception as e:
        return {"files": [], "log_dir": "", "error": str(e)}


@router.post("/system/restart")
def restart_backend_service(
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """Self-Kill 대응: 백엔드가 자기 자신을 재시작할 때 8000 포트 점유 프로세스를 해제하고,
    paas의 Job에서 분리된 독립 PowerShell 프로세스(powershell_daemon.run_detached_script)로
    안전 재기동한다 — paas가 내려가도 재시작 작업이 죽지 않는다."""
    import os  # noqa: PLC0415
    import sys  # noqa: PLC0415
    import threading  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from ..services import powershell_daemon  # noqa: PLC0415

    root_dir = Path(__file__).resolve().parent.parent.parent
    py_exe = sys.executable
    escaped_exe = py_exe.replace("'", "''")

    restart_script = (
        "Start-Sleep -Seconds 2; "
        "if (!(Test-Path '.venv')) { "
        "  Write-Host '[PaaS Provisioning] .venv missing. Creating Python virtual environment...'; "
        f"  & '{escaped_exe}' -m venv .venv; "
        "  if (Test-Path '.venv\\Scripts\\python.exe') { "
        "    & '.venv\\Scripts\\python.exe' -m pip install --upgrade pip --disable-pip-version-check; "
        "    if (Test-Path 'requirements.txt') { & '.venv\\Scripts\\python.exe' -m pip install --disable-pip-version-check -r requirements.txt } "
        "  } "
        "}; "
        "Write-Host '[PaaS Restart] Releasing port 8000...'; "
        "$pids = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess; "
        "foreach ($p in $pids) { if ($p -and $p -gt 0 -and $p -ne $PID) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } }; "
        "Start-Sleep -Seconds 1; "
        "if (Test-Path '.venv\\Scripts\\python.exe') { "
        "  & '.venv\\Scripts\\python.exe' -m uvicorn app.main:app --host 0.0.0.0 --port 8000 "
        "} else { "
        f"  & '{escaped_exe}' -m uvicorn app.main:app --host 0.0.0.0 --port 8000 "
        "}"
    )

    try:
        powershell_daemon.run_detached_script(restart_script, cwd=str(root_dir))
        actor_name = getattr(admin, "name", "admin")
        audit.record(db, actor_name, "system.restart", "backend_service", {"py_exe": py_exe})

        # 2.5초 후 기존 백엔드 프로세스를 종료하여 8000번 포트를 완전히 비워준다
        def _terminate_self():
            os._exit(0)

        threading.Timer(2.5, _terminate_self).start()

        return {
            "status": "restarting",
            "message": "PaaS 백엔드 서비스가 2초 후 기존 포트 해제 및 디태치 독립 프로세스로 안전하게 재기동됩니다.",
            "error": None,
            "executable": py_exe,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to schedule service restart: {e}")


@router.post("/system/sw-update")
def sw_update(
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """SW 업데이트: git pull → 콘솔 재빌드 → paas·console Windows 서비스 재시작.

    **콘솔은 여기서 빌드한다.** 배포되는 *프로젝트*의 환경설정은 배포 파이프라인의
    책임이지만(windows_service 런타임의 start.cmd, Docker 런타임의 이미지 빌드),
    콘솔은 플랫폼 자신이라 그런 파이프라인이 없다 — `npm run build` 산출물을 백엔드가
    /console에 정적 서빙할 뿐이다. 그래서 git pull만 하면 콘솔 의존성이 늘었을 때
    아무도 설치하지 않고, 빌드가 실패해도 **예전 dist가 그대로 서빙돼** 업데이트가 안
    된 것이 드러나지 않는다(실제로 겪었다 — xterm 의존성 추가 후 "failed to resolve
    import"). npm이 없거나 콘솔 소스가 없는 설치본에서는 건너뛴다.

    **파이썬 의존성도 여기서 설치한다.** 예전에는 "가상환경 위치가 설치본마다 달라
    잘못된 인터프리터에 설치하면 조용히 어긋난다"는 이유로 하지 않았는데, 그 위험은
    sys.executable을 쓰면 사라진다 — 지금 도는 프로세스 자신의 인터프리터가 정답이고
    그건 이 프로세스만 확실히 안다. 실제로 uvicorn[standard]가 빠진 채로 돌아 콘솔
    터미널이 조용히 죽는 일을 겪었고, 그때 "어느 파이썬에 설치해야 하나"가 문제였다.

    윈도우에서는 이미 적재된 확장 모듈(.pyd)을 덮어쓰려 하면 실패할 수 있다 — 새 패키지
    설치는 문제없고, 실패해도 로그에 남기고 재시작까지는 진행한다.

    출력은 logs/sw-update.log에 남긴다. 이 스크립트는 분리된 프로세스라 stdout이 어디에도
    닿지 않는데, 그러면 실패했는지조차 알 수 없다 — 콘솔 "서버 로그" 탭에서 읽는다.

    Restart-Service가 paas 서비스(현재 프로세스)를 stop→start 하므로, paas의 Job에서 분리된
    독립 PowerShell 프로세스(run_detached_script)로 띄워 백엔드가 내려가도 업데이트가 끝까지
    진행되게 한다(self-kill 방지).
    """
    import sys  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from ..services import powershell_daemon  # noqa: PLC0415

    settings = get_settings()
    repo_dir = str(settings.resolved_repo_root)
    services = [s.strip() for s in settings.sw_update_services.split(",") if s.strip()]
    if not services:
        raise HTTPException(status_code=400, detail="PAAS_SW_UPDATE_SERVICES가 비어 있습니다.")

    escaped_repo = repo_dir.replace("'", "''")
    restart_lines = "".join(
        f"Restart-Service -Name '{s.replace(chr(39), chr(39) * 2)}' -Force -ErrorAction SilentlyContinue; "
        for s in services
    )
    log_dir = _server_log_dir()
    escaped_log_dir = str(log_dir).replace("'", "''")
    escaped_log = str(log_dir / "sw-update.log").replace("'", "''")
    escaped_console = str(Path(repo_dir) / "console").replace("'", "''")
    escaped_requirements = str(Path(repo_dir) / "requirements.txt").replace("'", "''")
    # 지금 이 프로세스를 돌리는 인터프리터 — "어느 venv인가"를 물어볼 필요가 없는
    # 유일한 답이다(서비스가 uvicorn.exe로 떠 있어도 그 venv의 python.exe가 나온다).
    escaped_python = sys.executable.replace("'", "''")

    update_script = (
        "$ErrorActionPreference = 'Continue'; "
        f"New-Item -ItemType Directory -Force -Path '{escaped_log_dir}' | Out-Null; "
        f"Start-Transcript -Path '{escaped_log}' -Force | Out-Null; "
        f"Set-Location '{escaped_repo}'; "
        "Write-Host '[SW Update] git pull...'; "
        "git pull; "
        "Start-Sleep -Seconds 1; "
        # 파이썬 의존성 — 지금 도는 프로세스의 인터프리터로 설치한다(어느 venv인지
        # 물어볼 필요가 없다). 재시작 전에 해야 새 의존성이 다음 기동에 반영된다.
        f"if (Test-Path '{escaped_requirements}') {{ "
        "  Write-Host '[SW Update] pip install...'; "
        f"  & '{escaped_python}' -m pip install -r '{escaped_requirements}'; "
        "  if ($LASTEXITCODE -ne 0) { "
        "    Write-Host '[SW Update] !! pip install 실패 — 적재 중인 모듈은 덮어쓰지 못할 수 있습니다'; } "
        "} "
        # 콘솔은 플랫폼 자신이라 배포 파이프라인이 없다 — 여기서 빌드하지 않으면
        # 의존성이 늘었을 때 예전 dist가 조용히 계속 서빙된다.
        f"if (Test-Path '{escaped_console}\package.json') {{ "
        "  if (Get-Command npm -ErrorAction SilentlyContinue) { "
        f"    Push-Location '{escaped_console}'; "
        "    Write-Host '[SW Update] console: npm install...'; "
        "    npm install; "
        "    Write-Host '[SW Update] console: npm run build...'; "
        "    npm run build; "
        "    if ($LASTEXITCODE -ne 0) { "
        "      Write-Host '[SW Update] !! 콘솔 빌드 실패 — 이전 dist가 그대로 서빙됩니다'; } "
        "    Pop-Location; "
        "  } else { Write-Host '[SW Update] npm이 없어 콘솔 빌드를 건너뜁니다'; } "
        "} else { Write-Host '[SW Update] 콘솔 소스가 없어 빌드를 건너뜁니다'; } "
        "Write-Host '[SW Update] Restarting services...'; "
        + restart_lines
        + "Stop-Transcript | Out-Null; "
    )

    try:
        powershell_daemon.run_detached_script(update_script, cwd=repo_dir)
        audit.record(db, getattr(admin, "name", "admin"), "system.sw_update", "backend_service",
                     {"repo_dir": repo_dir, "services": services})
        return {
            "status": "updating",
            "message": (f"git pull → 의존성 설치 → 콘솔 재빌드 →"
                        f" 서비스 {', '.join(services)} 재시작을 시작했습니다."
                        " 진행 상황은 서버 로그의 sw-update.log에서 볼 수 있습니다."),
            "error": None,
            "services": services,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to schedule SW update: {e}")


@router.get("/system/server-logs/content")
def get_server_log_content(
    filename: str,
    tail_lines: int = 1000,
    _: ApiKey = Depends(require_admin),
):
    """실행 경로 하위 logs/의 .txt 로그 파일 내용을 파일 끝(Tail)을 기본으로 반환한다."""
    log_dir = _server_log_dir()
    target_path = (log_dir / filename).resolve()

    # 문자열 startswith로 비교하면 형제 디렉터리(logs-old 등)가 통과한다 — 경로 단위로 본다.
    if not target_path.is_relative_to(log_dir):
        raise HTTPException(status_code=403, detail="Access denied: outside log directory")
    # 목록이 .txt만 보여주므로 읽기도 같은 범위로 맞춘다.
    if target_path.suffix.lower() != ".txt":
        raise HTTPException(status_code=403, detail="Access denied: .txt only")

    if not target_path.is_file():
        raise HTTPException(status_code=404, detail=f"Log file '{filename}' not found")

    try:
        content_full = target_path.read_text(encoding="utf-8", errors="replace")
        lines = content_full.splitlines()
        tail_content = "\n".join(lines[-tail_lines:]) if len(lines) > tail_lines else content_full
        return {
            "filename": filename,
            "total_lines": len(lines),
            "tail_lines": tail_lines,
            "content": tail_content,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read log file: {e}")
