"""자기 재시작 — paas가 자기 자신을 내리고 다시 올리는 명령을 만든다.

**왜 별도 모듈인가.** 재시작 대상을 아는 곳이 여러 곳이었고, 그중 하나가 포트를 8000으로
추측해 두는 바람에 재시작하면 백엔드가 7000에서 사라졌다. 어디로 어떻게 다시 올릴지는
한 곳에서만 정한다.

**정책은 헬퍼 스크립트에 있다.** 윈도우에서는 infra/restart-paas.ps1을 부른다 — 터미널에
사람이 직접 치는 것과 **같은 스크립트**다. 여기서 PowerShell을 따로 생성하면 정책이 두
벌이 되고, 실제로 그렇게 만들었더니 분리된 프로세스의 출력이 어디에도 닿지 않아 재시작이
실패해도 알 수 없었다(services/*.py에서 같은 함정을 sw-update 주석이 이미 경고한다).
헬퍼는 logs/restart-paas.log에 남기고 헬스까지 확인한다.

**두 가지 모드.** 서비스로 등록된 설치본(운영 — nssm/systemd)에서는 서비스 관리자가 기동
명령을 이미 들고 있으므로 재시작만 지시하면 된다. 등록되지 않은 설치본(로컬 개발)에서는
uvicorn을 직접 다시 띄운다. 어느 쪽인지는 스크립트가 실행 시점에 판정한다.

**self-kill 방지.** 호출자는 이 명령을 paas의 Job에서 분리된 독립 프로세스로 실행해야
한다(powershell_daemon.run_detached_script) — paas가 내려가도 재시작이 끝까지 진행된다.
"""
import shlex
import subprocess
import sys
from pathlib import Path

# 새 프로세스가 포트를 잡기 전에 옛 프로세스가 완전히 빠질 시간. /system/restart는
# 응답을 보낸 뒤 os._exit로 스스로 빠지므로, 그보다 넉넉히 기다린다.
SETTLE_SECONDS = 4

# uvicorn 진입점 — Dockerfile·배포가이드·README와 같은 대상이어야 한다.
ASGI_TARGET = "app.main:app"

# 터미널에서도 쓰는 재시작 헬퍼(리포 기준 상대 경로).
HELPER_SCRIPT = "infra/restart-paas.ps1"


def is_windows() -> bool:
    return sys.platform == "win32"


def restart_script(
    *,
    services: list[str],
    host: str,
    port: int,
    repo_root: str,
    python_exe: str | None = None,
) -> str:
    """재시작 명령 본문. 플랫폼에 맞는 셸 문법으로 돌려준다.

    port는 호출자가 **실제 바인딩된 소켓**에서 읽어 넘겨야 한다(bound_port) — 추측하면
    재시작이 다른 포트로 살아난다. python_exe를 주면 그 인터프리터로 uvicorn을 띄운다
    (지금 도는 프로세스의 sys.executable이 "어느 venv인가"의 유일하게 확실한 답이다).
    """
    exe = python_exe or sys.executable
    if is_windows():
        return _windows_helper_call(
            services=services, host=host, port=port, repo_root=repo_root, python_exe=exe)
    return _posix_script(
        services=services, host=host, port=port, repo_root=repo_root, python_exe=exe)


def _windows_helper_call(*, services: list[str], host: str, port: int,
                         repo_root: str, python_exe: str) -> str:
    """터미널과 같은 헬퍼를 -Now로 부른다(이미 분리된 프로세스 안이므로 다시 분리하지 않는다)."""
    helper = Path(repo_root) / HELPER_SCRIPT
    parts = [
        f"& '{_ps_quote(str(helper))}'",
        "-Now",
        f"-RepoRoot '{_ps_quote(repo_root)}'",
        f"-BindHost '{_ps_quote(host)}'",
        f"-Port {int(port)}",
        f"-Python '{_ps_quote(python_exe)}'",
        f"-SettleSeconds {SETTLE_SECONDS}",
    ]
    names = [s for s in services if s]
    if names:
        joined = ",".join(f"'{_ps_quote(s)}'" for s in names)
        parts.append(f"-Service @({joined})")
    return " ".join(parts)


def _posix_script(*, services: list[str], host: str, port: int,
                  repo_root: str, python_exe: str) -> str:
    """sh — systemd 유닛이 있으면 systemctl restart, 없으면 uvicorn 직접 기동.

    리눅스 확장을 위한 자리다. 콘솔의 PowerShell 터미널은 윈도우 전용이지만(pywinpty),
    재시작 자체는 플랫폼을 가릴 이유가 없다. 윈도우처럼 헬퍼 스크립트를 두지 않은 이유는
    사람이 직접 치는 터미널이 리눅스에는 아직 없어서다 — 필요해지면 같은 정책의 .sh를
    두고 여기서 부르는 쪽으로 옮긴다.
    """
    names = " ".join(shlex.quote(s) for s in services if s)
    return (
        f"cd {shlex.quote(repo_root)}; "
        f"sleep {SETTLE_SECONDS}; "
        "found=''; "
        + (f"for n in {names}; do "
           "  if systemctl list-unit-files \"$n.service\" >/dev/null 2>&1; then found=\"$found $n\"; fi; "
           "done; " if names else "")
        + "if [ -n \"$found\" ]; then "
        "  echo \"[PaaS Restart] systemctl restart:$found\"; "
        "  for n in $found; do systemctl restart \"$n\"; done; "
        "else "
        f"  echo '[PaaS Restart] no unit registered -- starting uvicorn on {host}:{port}'; "
        f"  exec {shlex.quote(python_exe)} -m uvicorn {ASGI_TARGET} "
        f"--host {shlex.quote(host)} --port {int(port)}; "
        "fi"
    )


def launch(script: str, cwd: str) -> None:
    """재시작 스크립트를 paas와 분리된 프로세스로 띄운다(fire-and-forget).

    실행기도 플랫폼을 따라가야 한다 — 리눅스에는 powershell.exe가 없으므로, 스크립트만
    갈라 놓고 실행을 윈도우 헬퍼에 맡기면 posix에서는 조용히 아무 일도 일어나지 않는다.
    """
    if is_windows():
        from . import powershell_daemon  # noqa: PLC0415 — 윈도우에서만 쓰는 경로

        powershell_daemon.run_detached_script(script, cwd=cwd)
        return

    # POSIX: 새 세션으로 떼어내 paas가 죽어도 살아남게 한다(윈도우의 Job breakaway에 대응).
    # 출력은 파일로 돌린다 — 분리된 프로세스의 stdout은 어디에도 닿지 않아서, 로그가
    # 없으면 실패했는지조차 알 수 없다(윈도우 헬퍼가 logs/restart-paas.log에 남기는 것과 같다).
    log_dir = Path(cwd) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "restart-paas.log").open("a", encoding="utf-8") as log:
        subprocess.Popen(["/bin/sh", "-c", script], cwd=cwd, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)


def _ps_quote(value: str) -> str:
    """PowerShell 단일 인용 문자열 안의 ' 이스케이프."""
    return value.replace("'", "''")


def bound_port(scope: dict) -> int | None:
    """이 요청을 받은 소켓이 실제로 바인딩된 포트.

    Host 헤더(request.url.port)가 아니라 소켓의 주소다 — IIS 뒤에서 공개 도메인으로
    들어와도 실제 포트가 나온다. 유닉스 소켓(--uds)으로 떠 있으면 포트가 없어 None.
    """
    server = scope.get("server")
    if not server or len(server) < 2:
        return None
    port = server[1]
    return port if isinstance(port, int) else None
