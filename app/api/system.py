import hmac
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, WebSocket
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
    rotate_token,
    validate_email_domain,
    verify_password,
)
from ..services import monitor

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

    return UserLoginOut(
        name=user.name or user.email,
        email=user.email,
        key=_start_session(db, user.email, user.is_admin),
        is_admin=user.is_admin,
        organization_id=user.organization_id,
        organization_name=user.organization.name if user.organization else None,
    )


@router.post("/auth/logout", status_code=204)
def logout_user_session(
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
    
    if body.organization_id is not None:
        org = db.get(Organization, body.organization_id)
        if org and org not in user.organizations:
            user.organizations.append(org)
    user.organization_id = body.organization_id
    db.commit()
    db.refresh(user)
    audit.record(db, admin.name, "user.set_organization", user.email, {"organization_id": body.organization_id})
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


@router.websocket("/system/powershell/ws")
async def powershell_websocket_terminal(websocket: WebSocket):
    """admin 전용 실시간 PowerShell WebSocket 터미널.

    연결마다 상주 데몬을 하나 띄워 그 연결 동안 세션 상태(cd·변수)를 유지하고, 명령 실행은
    asyncio.to_thread로 돌려 이벤트 루프를 블로킹하지 않는다. 연결 종료 시 데몬을 정리한다.
    """
    import asyncio  # noqa: PLC0415
    import os  # noqa: PLC0415
    from ..services import powershell_daemon  # noqa: PLC0415
    await websocket.accept()
    settings = get_settings()
    cwd_dir = os.path.abspath(settings.powershell_start_dir) if settings.powershell_start_dir else None
    start_info = f"WorkDir: {cwd_dir or os.getcwd()}"

    daemon = powershell_daemon.PowerShellDaemon(cwd=cwd_dir)
    try:
        await websocket.send_text(f"Windows PowerShell Interactive Console Connected ({start_info}).\nType commands or click Disconnect to end session.\n\nPS > ")
        while True:
            cmd = await websocket.receive_text()
            cmd_str = cmd.strip()
            if not cmd_str:
                await websocket.send_text("PS > ")
                continue
            if cmd_str.lower() in ["exit", "quit"]:
                await websocket.send_text("PowerShell Session Closed.\n")
                await websocket.close()
                break

            try:
                result = await asyncio.to_thread(daemon.run, cmd_str, 30.0)
                out = result.output
            except TimeoutError:
                out = "(timed out after 30s)"
            if not out.strip():
                out = "(completed)"
            await websocket.send_text(f"{out}\n\nPS > ")
    except Exception:
        pass
    finally:
        await asyncio.to_thread(daemon.stop)


@router.get("/system/build-logs")
def list_build_log_files(
    _: ApiKey = Depends(require_admin),
):
    """PAAS_BUILD_LOG_DIR 하위의 .txt 빌드 로그 파일 목록을 최신순으로 반환한다."""
    from pathlib import Path  # noqa: PLC0415

    try:
        settings = get_settings()
        log_dir = Path(settings.build_log_dir).resolve()
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
    """SW 업데이트: 프로젝트 폴더에서 git pull 후 paas·console Windows 서비스를 재시작한다.

    환경설정(pip/npm install)은 여기서 하지 않는다 — 그건 프로젝트 배포 파이프라인의
    책임이다(windows_service 런타임의 start.cmd, 또는 Docker 런타임의 이미지 빌드가
    배포마다 이미 수행한다). sw-update는 플랫폼 자신(paas·콘솔) 코드를 최신화하고
    이미 구성된 서비스를 재시작하는 것으로 끝난다.

    Restart-Service가 paas 서비스(현재 프로세스)를 stop→start 하므로, paas의 Job에서 분리된
    독립 PowerShell 프로세스(run_detached_script)로 띄워 백엔드가 내려가도 업데이트가 끝까지
    진행되게 한다(self-kill 방지).
    """
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
    update_script = (
        "$ErrorActionPreference = 'Continue'; "
        f"Set-Location '{escaped_repo}'; "
        "Write-Host '[SW Update] git pull...'; "
        "git pull; "
        "Start-Sleep -Seconds 1; "
        "Write-Host '[SW Update] Restarting services...'; "
        + restart_lines
    )

    try:
        powershell_daemon.run_detached_script(update_script, cwd=repo_dir)
        audit.record(db, getattr(admin, "name", "admin"), "system.sw_update", "backend_service",
                     {"repo_dir": repo_dir, "services": services})
        return {
            "status": "updating",
            "message": f"git pull 후 서비스 {', '.join(services)} 재시작을 시작했습니다. 잠시 후 연결을 확인하세요.",
            "error": None,
            "services": services,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to schedule SW update: {e}")


@router.get("/system/build-logs/content")
def get_build_log_content(
    filename: str,
    tail_lines: int = 1000,
    _: ApiKey = Depends(require_admin),
):
    """PAAS_BUILD_LOG_DIR 하위의 .txt 로그 파일 내용을 파일 끝(Tail)을 기본으로 반환한다."""
    from pathlib import Path  # noqa: PLC0415

    settings = get_settings()
    log_dir = Path(settings.build_log_dir).resolve()
    target_path = (log_dir / filename).resolve()

    if not str(target_path).startswith(str(log_dir)):
        raise HTTPException(status_code=403, detail="Access denied: outside log directory")

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
