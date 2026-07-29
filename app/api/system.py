import hmac
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, WebSocket
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..models import ApiKey, AuditEvent, EnvVar, LlmProvider, Module, UserAccount, UserSession, utcnow
from ..schemas import (
    ApiKeyCreate,
    ApiKeyIssued,
    UserAccountOut,
    UserLoginOut,
    UserLoginRequest,
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
    }


@health_router.get("/status")
def system_status(_: ApiKey = Depends(require_admin)):
    return monitor.snapshot()


@router.get("/auth/me")
def get_current_user_profile(key: ApiKey = Depends(require_api_key)):
    settings = get_settings()
    return {
        "name": key.name,
        "is_admin": key.is_admin,
        "allowed_email_domain": settings.allowed_email_domain,
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


@router.get("/auth/accounts", response_model=list[UserAccountOut])
def list_user_accounts(
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_admin),
):
    """계정 목록 — 승인 대기가 먼저 온다."""
    rows = db.execute(
        select(UserAccount).order_by(UserAccount.is_approved, UserAccount.created_at)
    ).scalars()
    return [
        UserAccountOut(id=u.id, email=u.email, name=u.name,
                       is_approved=u.is_approved, is_admin=u.is_admin)
        for u in rows
    ]


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
    return UserAccountOut(id=user.id, email=user.email, name=user.name,
                          is_approved=user.is_approved, is_admin=user.is_admin)


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
    """admin 전용 PowerShell 명령어 실행 API."""
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    cmd = body.get("command", "").strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="command field is required")

    settings = get_settings()
    cwd_dir = os.path.abspath(settings.powershell_start_dir) if settings.powershell_start_dir else None

    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
            cwd=cwd_dir,
            capture_output=True,
            text=True,
            timeout=30.0,
            encoding="utf-8",
            errors="replace",
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        audit.record(db, admin.name, "powershell.exec", cmd[:100], {"returncode": proc.returncode})
        return {
            "command": cmd,
            "returncode": proc.returncode,
            "output": output or "(no output)",
            "cwd": cwd_dir or os.getcwd(),
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="PowerShell command execution timed out (30s)")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PowerShell execution failed: {e}")


@router.websocket("/system/powershell/ws")
async def powershell_websocket_terminal(websocket: WebSocket):
    """admin 전용 실시간 PowerShell WebSocket 터미널."""
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    await websocket.accept()
    settings = get_settings()
    cwd_dir = os.path.abspath(settings.powershell_start_dir) if settings.powershell_start_dir else None
    start_info = f"WorkDir: {cwd_dir or os.getcwd()}"

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

            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd_str],
                cwd=cwd_dir,
                capture_output=True,
                text=True,
                timeout=30.0,
                encoding="utf-8",
                errors="replace",
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            if not out.strip():
                out = "(completed)"
            await websocket.send_text(f"{out}\n\nPS > ")
    except Exception:
        pass


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
    """Self-Kill 대응: 백엔드가 자기 자신을 재시작할 때 부모 프로세스 종속성 없이 DETACHED 독립 프로세스로 2초 후 안전 재기동한다."""
    import sys  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    py_exe = sys.executable
    restart_script = (
        f"Start-Sleep -Seconds 2; & '{py_exe}' -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
    )

    try:
        flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", restart_script],
            creationflags=flags,
            close_fds=True,
        )
        actor_name = getattr(admin, "name", "admin")
        audit.record(db, actor_name, "system.restart", "backend_service", {"py_exe": py_exe})
        return {
            "status": "restarting",
            "message": "PaaS 백엔드 서비스가 2초 후 디태치 독립 프로세스로 안전하게 재기동됩니다.",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to schedule service restart: {e}")


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
