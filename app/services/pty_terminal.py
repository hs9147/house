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
OpenConsole.exe와 winpty.dll · winpty-agent.exe가 함께 들어 있다). ConPTY는 Windows 10
1809 / Server 2019부터라 **Server 2016에서는 winpty 백엔드**가 필요하다 — 기본값은
자동 선택이고, 자동 선택이 어긋나면 PAAS_PTY_BACKEND로 못 박는다.
"""
from __future__ import annotations

import os

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


class PtyUnavailable(RuntimeError):
    """이 서버에서는 PTY를 열 수 없다(백엔드 미설치·미지원)."""


def backend_code(name: str) -> int | None:
    """설정값 → pywinpty backend 코드. 비었으면 None(=pywinpty가 알아서 고른다)."""
    name = (name or "").strip().lower()
    if not name:
        return None
    if name not in BACKENDS:
        raise PtyUnavailable(
            f"알 수 없는 PTY 백엔드: {name} ({' 또는 '.join(BACKENDS)})")
    return BACKENDS[name]


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

    def close(self):
        import signal  # noqa: PLC0415

        for step in (lambda: os.kill(self._pid, signal.SIGKILL),
                     lambda: os.waitpid(self._pid, 0),
                     lambda: os.close(self._fd)):
            try:
                step()
            except OSError:
                pass
