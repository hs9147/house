"""의사 터미널(PTY) — 콘솔의 PowerShell 창을 진짜 터미널로 만드는 계층.

**왜 필요한가.** 기존 /exec·/ws는 명령 한 줄을 받아 끝날 때까지 기다렸다가 출력을 통째로
돌려주는 방식이었다. 그래서 되묻는 명령(Read-Host, git commit, python REPL)이 그대로
멈추고, Ctrl+C가 없고, 30초를 넘는 작업은 진행 상황을 볼 수 없었다. 셸을 PTY에 붙이면
키 입력과 화면 출력을 바이트로 그대로 중계하므로 그 셋이 한 번에 해결된다.

**왜 SSH가 아닌가.** 브라우저는 SSH를 말하지 못해서 어차피 서버 쪽에 브리지를 두어야
하는데, 그러면 SSH가 지키는 구간이 localhost→localhost가 된다. 정작 사내망을 지나는
구간(브라우저→IIS, 80포트)은 그대로 평문이라 순서가 뒤집힌다. 게다가 윈도우 계정·키
관리가 새로 생기고 명령 감사가 끊긴다.

**백엔드.** pywinpty는 ConPTY와 winpty 두 가지를 모두 들고 다닌다(휠에 conpty.dll ·
OpenConsole.exe와 winpty.dll · winpty-agent.exe가 함께 들어 있다 — 3.0 기준이고, 2.x
휠에는 winpty만 있다). ConPTY는 Windows 10 1809 / Server 2019부터라 **Server 2016에서는
winpty 백엔드**가 필요하다. 이건 물어볼 것이 아니라 빌드 번호로 아는 것이므로
default_backend_code()가 정하고, 그 판정이 어긋나면 PAAS_PTY_BACKEND로 못 박는다.
"""
from __future__ import annotations

import os
import sys
import time

# 셸을 못 띄웠을 때 사용자에게 그대로 보여 줄 안내. 여기서 조용히 실패하면 화면에는
# 빈 터미널만 남아서 "왜 아무것도 안 나오지"가 된다.
INSTALL_HINT = (
    "PTY 백엔드가 없습니다 — 서버에서 `pip install pywinpty` 후 플랫폼을 재시작하세요."
    " (Windows Server 2016이면 ConPTY가 없으므로 PAAS_PTY_BACKEND=winpty를 함께"
    " 지정해야 할 수 있습니다.)"
)

# pywinpty의 backend 인자 값(winpty/enums.py의 Backend). 이름으로 받아 숫자로 옮긴다 —
# 설정 파일에 0/1을 적게 하면 어느 쪽인지 알 수 없다.
BACKENDS = {"conpty": 0, "winpty": 1}
# ConPTY가 들어온 윈도우 빌드(10 1809 / Server 2019). 그 아래에는 ConPTY 자체가 없다.
CONPTY_MIN_BUILD = 17763
# 열린 직후 셸이 죽는지 보려면 잠깐 기다려야 한다 — 너무 짧으면 exec 실패를 놓치고,
# 길면 진단 요청이 그만큼 늘어진다.
PROBE_SETTLE_SECONDS = 0.3

# probe()가 돌려주는 사유 코드. 화면 문구는 언제든 다듬지만 이 값은 계약이라 그대로 둔다
# (infra/terminal-doctor.ps1이 이걸로 판정한다).
REASON_NO_WS_LIBRARY = "no_ws_library"      # uvicorn이 업그레이드를 못 받는다
REASON_BAD_BACKEND = "bad_backend"          # PAAS_PTY_BACKEND 값이 잘못됐다
REASON_NO_BACKEND = "no_pty_backend"        # pywinpty가 없거나 셸을 못 띄웠다
REASON_SHELL_EXITED = "shell_exited"        # 열렸는데 즉시 죽었다(exit_status 참고)


class PtyUnavailable(RuntimeError):
    """이 서버에서는 PTY를 열 수 없다(백엔드 미설치·미지원)."""


def default_backend_code() -> int | None:
    """설정이 비었을 때 쓸 백엔드. 고를 이유가 없으면 None(=pywinpty에 맡긴다).

    **Server 2016에서는 맡기면 안 된다.** ConPTY는 빌드 17763(10 1809 / Server 2019)부터
    존재하는데, 자동 선택이 그걸 고르면 셸이 열리자마자 죽는다 — WebSocket은 101로 붙고
    화면에는 "세션이 끝났습니다"만 남아서, 겉보기로는 연결 문제와 구분되지 않는다.
    빌드 번호는 확실히 알 수 있으니 사람에게 묻지 말고 여기서 정한다.
    """
    if os.name != "nt":
        return None
    try:
        build = sys.getwindowsversion().build  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — 값을 못 읽으면 멋대로 정하지 않고 맡긴다
        return None
    return BACKENDS["winpty"] if build < CONPTY_MIN_BUILD else None


def backend_code(name: str) -> int | None:
    """설정값 → pywinpty backend 코드. 비었으면 이 OS에 맞는 기본값을 고른다."""
    name = (name or "").strip().lower()
    if not name:
        return default_backend_code()
    if name not in BACKENDS:
        raise PtyUnavailable(
            f"알 수 없는 PTY 백엔드: {name} ({' 또는 '.join(BACKENDS)})")
    return BACKENDS[name]


def websocket_library() -> str:
    """이 프로세스에서 쓸 수 있는 WebSocket 구현 이름. 없으면 빈 문자열.

    uvicorn은 websockets·wsproto 중 하나가 있어야 업그레이드를 받는다. 없으면
    **HTTP는 전부 정상인데 WebSocket만 404**가 나고, 서버 로그에만 경고가 찍힌다
    ("No supported WebSocket library detected"). 밖에서 보면 프록시 문제와 똑같이
    보여서, 여기서 보지 않으면 IIS만 뒤지게 된다(실제로 그랬다).

    **설치 여부만 본다.** 라이브러리가 있어도 서버가 `--ws none`으로 떠 있으면 결과는
    같은 404다 — 그건 이 프로세스 안에서 알 수 없으므로 기동 명령을 봐야 한다.
    """
    for name in ("websockets", "wsproto"):
        try:
            __import__(name)
            return name
        except ImportError:
            continue
    return ""


def probe(shell: str, backend: str) -> dict:
    """터미널을 열 수 있는 상태인지 실제로 한 번 열어 보고 닫는다.

    WebSocket이 프록시에 막히면 브라우저는 이유를 알려주지 않는다(닫힘 코드 1006뿐이다).
    같은 것을 REST로 물어보면 **서버가 준비됐는지**와 **길이 막혔는지**를 가를 수 있다 —
    여기가 ok인데 소켓이 안 열리면 원인은 서버가 아니라 그 사이(IIS/ARR의 WebSocket)다.

    import 여부만 보지 않고 실제로 셸을 띄우는 이유: Server 2016에서 ConPTY 자동 선택이
    실패하는 것처럼, 설치는 됐는데 열리지 않는 경우가 이 기능의 주된 실패 모양이다.
    """
    # reason은 기계가 읽는 사유 코드고, error·hint는 사람이 읽는 한국어 문장이다.
    # 콘솔은 후자를 그대로 띄우면 되지만, 서버에서 도는 진단 스크립트는 그럴 수 없다 —
    # Windows 콘솔 코드페이지에서 한글이 깨져 정작 가장 중요한 줄을 못 읽는다. 문장을
    # 파싱하게 두면 문구를 다듬을 때마다 조용히 깨지므로, 사유를 값으로 준다.
    info: dict = {"shell": shell, "backend": backend or "auto", "ok": False, "error": "",
                  "reason": "", "exit_status": None,
                  "resolved_backend": "", "websocket_library": websocket_library()}
    if not info["websocket_library"]:
        info["reason"] = REASON_NO_WS_LIBRARY
        info["error"] = (
            "이 서버에 WebSocket 구현이 없습니다 — HTTP는 정상이지만 터미널 소켓만"
            ' 404로 떨어집니다. `pip install "uvicorn[standard]"`(또는 websockets) 후'
            " 플랫폼을 재시작하세요."
        )
        return info
    try:
        resolved = backend_code(backend)
    except PtyUnavailable as e:
        info["reason"] = REASON_BAD_BACKEND
        info["error"] = str(e)
        return info
    # 실제로 무엇이 골라졌는지 함께 답한다 — "auto"만 보여 주면 Server 2016에서 무엇이
    # 쓰였는지 알 수 없어, 백엔드가 원인일 때 그 사실이 드러나지 않는다. POSIX는 표준
    # 라이브러리로 열어 고를 백엔드가 없으므로 비워 둔다(화면에서도 빠진다).
    names = {code: name for name, code in BACKENDS.items()}
    if os.name == "nt":
        info["resolved_backend"] = names.get(resolved, "pywinpty 자동")
    try:
        terminal = PtyTerminal([shell], cols=80, rows=24, backend=resolved)
    except PtyUnavailable as e:
        info["reason"] = REASON_NO_BACKEND
        info["error"] = str(e)
        return info
    try:
        # 열자마자 끝났는지 본다. POSIX에서 셸 경로가 틀리면 fork는 성공하고 자식의
        # exec만 실패하므로(종료코드 127), 여기서 보지 않으면 "열렸다"고 답하게 된다 —
        # 셸 경로 오타는 이 기능에서 가장 흔한 실패다.
        time.sleep(PROBE_SETTLE_SECONDS)
        status = terminal.exit_status()
        if status is None:
            info["ok"] = True
        else:
            info["reason"] = REASON_SHELL_EXITED
            info["exit_status"] = status
            info["error"] = (
                f"셸이 즉시 종료했습니다(종료코드 {status})."
                + (f" '{shell}'을 실행할 수 없습니다 — 경로를 확인하세요."
                   if status == 127 else "")
            )
    finally:
        terminal.close()
    return info


class PtyTerminal:
    """셸 하나에 붙은 PTY. 바이트를 그대로 주고받는 것이 전부다.

    읽기는 블로킹이므로 호출자가 별도 스레드에서 돌린다(api/system.py의 WebSocket
    핸들러가 asyncio.to_thread로 감싼다).

    윈도우는 pywinpty, POSIX는 표준 라이브러리 pty로 연다. POSIX 쪽을 함께 두는 이유가
    둘 있다 — README가 운영 권장 OS로 리눅스를 들고 있고, 무엇보다 **중계 로직을 실제
    셸로 검증할 수 있어야** 한다(윈도우 전용으로 두면 이 코드는 서버에 올리기 전까지
    한 번도 돌려 볼 수 없다).
    """

    def __init__(self, argv: list[str], *, cwd: str | None = None,
                 cols: int = 120, rows: int = 30, backend: int | None = None):
        self._impl = (_WinPty(argv, cwd, cols, rows, backend) if os.name == "nt"
                      else _PosixPty(argv, cwd, cols, rows))

    def read(self, size: int = 4096) -> str:
        """터미널 출력 한 덩어리. 셸이 끝났으면 빈 문자열."""
        return self._impl.read(size)

    def write(self, data: str) -> None:
        self._impl.write(data)

    def resize(self, cols: int, rows: int) -> None:
        """브라우저 창 크기가 바뀌면 셸에도 알려야 한다 — 모르면 줄바꿈이 어긋난다."""
        self._impl.resize(cols, rows)

    def exit_status(self) -> int | None:
        """셸이 이미 끝났으면 종료코드, 살아 있으면 None."""
        return self._impl.exit_status()

    def close(self) -> None:
        """연결이 끊기면 셸도 끝낸다 — 남겨 두면 셸 프로세스가 계속 쌓인다."""
        self._impl.close()


class _WinPty:
    def __init__(self, argv, cwd, cols, rows, backend):
        try:
            from winpty import PtyProcess  # noqa: PLC0415 — 윈도우 전용 선택 의존성
        except ImportError as e:
            raise PtyUnavailable(f"{INSTALL_HINT} ({e})")
        try:
            # dimensions는 (행, 열) 순서다 — 뒤집으면 줄바꿈이 엉뚱한 자리에서 일어난다.
            self._proc = PtyProcess.spawn(
                argv, cwd=cwd, dimensions=(rows, cols), backend=backend)
        except Exception as e:  # noqa: BLE001 — 백엔드가 내는 예외 종류가 제각각이다
            raise PtyUnavailable(f"셸을 띄우지 못했습니다: {type(e).__name__}: {e}")

    def read(self, size):
        try:
            return self._proc.read(size)
        except EOFError:
            return ""

    def write(self, data):
        self._proc.write(data)

    def resize(self, cols, rows):
        self._proc.setwinsize(rows, cols)

    def exit_status(self):
        try:
            if self._proc.isalive():
                return None
            return self._proc.exitstatus
        except Exception:  # noqa: BLE001 — 이미 사라진 경우
            return -1

    def close(self):
        try:
            self._proc.terminate(force=True)
        except Exception:  # noqa: BLE001 — 이미 죽은 경우가 대부분이다
            pass


class _PosixPty:
    """표준 라이브러리만으로 여는 PTY(리눅스·macOS)."""

    def __init__(self, argv, cwd, cols, rows):
        import pty  # noqa: PLC0415 — POSIX 전용

        try:
            self._pid, self._fd = pty.fork()
        except OSError as e:
            raise PtyUnavailable(f"셸을 띄우지 못했습니다: {e}")
        if self._pid == 0:
            # 자식. 여기서는 곧바로 exec한다 — 파이썬 정리 절차(atexit·flush)를 타면
            # 부모의 상태를 복제한 채로 건드리게 된다.
            try:
                if cwd:
                    os.chdir(cwd)
                os.execvp(argv[0], argv)
            except BaseException:  # noqa: BLE001
                pass
            os._exit(127)
        self.resize(cols, rows)

    def read(self, size):
        try:
            data = os.read(self._fd, size)
        except OSError:  # 셸이 끝나면 EIO가 난다 — 종료 신호로 받는다
            return ""
        return data.decode("utf-8", "replace")

    def write(self, data):
        os.write(self._fd, data.encode("utf-8"))

    def resize(self, cols, rows):
        import fcntl  # noqa: PLC0415
        import struct  # noqa: PLC0415
        import termios  # noqa: PLC0415

        fcntl.ioctl(self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def exit_status(self):
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
        except OSError:
            return -1  # 이미 거둬졌다
        if pid == 0:
            return None  # 아직 살아 있다
        self._reaped = True
        return os.waitstatus_to_exitcode(status)

    def close(self):
        import signal  # noqa: PLC0415

        if getattr(self, "_reaped", False):
            try:
                os.close(self._fd)
            except OSError:
                pass
            return
        for step in (lambda: os.kill(self._pid, signal.SIGKILL),
                     lambda: os.waitpid(self._pid, 0),
                     lambda: os.close(self._fd)):
            try:
                step()
            except OSError:
                pass
