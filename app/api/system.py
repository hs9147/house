import hmac
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..models import ApiKey, AuditEvent, EnvVar, LlmProvider, Module
from ..schemas import ApiKeyCreate, ApiKeyIssued, UserRegisterRequest, UserRegisterOut, UserLoginRequest, UserLoginOut
from ..security import hash_key, issue_key, require_admin, require_api_key, rotate_token, validate_email_domain
from ..services import monitor

# 헬스체크·상태 프로브는 버전 prefix 밖에 둔다(로드밸런서/k8s liveness probe, 콘솔 로그인
# 프로브가 API 버전과 무관하게 고정된 경로를 기대함) — router.py 참고.
health_router = APIRouter(tags=["system"])
router = APIRouter(tags=["system"])


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
    existing = db.execute(select(ApiKey).where(ApiKey.name == email)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="이미 등록된 계정 이메일입니다.")

    # 3. 비밀번호 및 키 생성 후 계정 저장
    password = body.password.strip()
    raw_key = issue_key()
    user_key = ApiKey(name=email, key_hash=hash_key(password), is_admin=False)
    db.add(user_key)
    db.commit()

    audit.record(db, email, "user.register", email, {"name": body.name})

    return UserRegisterOut(
        name=body.name,
        email=email,
        key=password,
        is_admin=False,
    )


@router.post("/auth/login", response_model=UserLoginOut)
def login_user_account(
    body: UserLoginRequest,
    db: Session = Depends(get_db),
):
    settings = get_settings()
    email = body.email.strip()
    password = body.password.strip()

    # 1. 관리자 API 키로 직접 로그인 시도 시
    if settings.admin_api_key and hmac.compare_digest(password, settings.admin_api_key):
        return UserLoginOut(name="bootstrap-admin", email=email or "admin@system", key=password, is_admin=True)

    # 2. 계정 이메일 도메인 검증
    if settings.allowed_email_domain and email and not validate_email_domain(email):
        allowed = settings.allowed_email_domain.replace("@", "")
        raise HTTPException(status_code=403, detail=f"@{allowed} 계정 이메일만 로그인 가능합니다.")

    # 3. 이메일 기반 계정 검색 및 비밀번호 검증 (IIS 팝업 방지를 위해 400 상태코드 사용)
    user_row = db.execute(select(ApiKey).where(ApiKey.name == email)).scalar_one_or_none()
    if user_row is not None:
        if user_row.key_hash and user_row.key_hash != hash_key(password) and user_row.key_hash != password:
            raise HTTPException(status_code=400, detail="비밀번호가 올바르지 않습니다.")
        # 비밀번호 일치 시 최신 암호화 해시로 자동 갱신
        if user_row.key_hash != hash_key(password):
            user_row.key_hash = hash_key(password)
            db.commit()
        return UserLoginOut(name=user_row.name, email=email, key=password, is_admin=user_row.is_admin)

    # 4. 해시 기반 키 검색 (기존 API 키 호환)
    key_row = db.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(password))).scalar_one_or_none()
    if key_row is not None:
        return UserLoginOut(name=key_row.name, email=email or key_row.name, key=password, is_admin=key_row.is_admin)

    # 신규 등록되지 않은 정상 도메인 계정 최초 로그인 시 자동 계정 등록 처리
    new_user = ApiKey(name=email, key_hash=hash_key(password), is_admin=False)
    db.add(new_user)
    db.commit()
    return UserLoginOut(name=email.split("@")[0] if "@" in email else email, email=email, key=password, is_admin=False)


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
    import subprocess  # noqa: PLC0415
    cmd = body.get("command", "").strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="command field is required")

    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
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
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="PowerShell command execution timed out (30s)")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PowerShell execution failed: {e}")


@router.websocket("/system/powershell/ws")
async def powershell_websocket_terminal(websocket: WebSocket):
    """admin 전용 실시간 PowerShell WebSocket 터미널."""
    import subprocess  # noqa: PLC0415
    await websocket.accept()
    try:
        await websocket.send_text("Windows PowerShell Interactive Console Connected.\nType commands or click Disconnect to end session.\n\nPS > ")
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
