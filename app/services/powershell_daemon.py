"""상주 PowerShell 데몬 — 명령마다 powershell.exe를 새로 띄우던 것을 분리한다.

기존 system.py는 /exec·/ws에서 명령마다 `subprocess.run(["powershell.exe", ...])`으로
프로세스를 새로 띄웠다. 그래서 (1) 세션 상태(cd·변수·import)가 명령 간에 유지되지 않았고,
(2) 동기 subprocess.run이 async WebSocket 핸들러 안에서 이벤트 루프를 블로킹했다.

여기서는 장수(long-lived) PowerShell 프로세스를 하나 띄워 stdin으로 명령을 흘려보내고,
전용 리더 스레드가 stdout을 큐로 모은다. 명령 뒤에 고유 sentinel을 출력시켜 그 명령의
출력 경계를 잡는다 — 같은 프로세스라 세션 상태가 유지된다. API는 run()을 스레드풀/
to_thread로 호출해 이벤트 루프를 블로킹하지 않는다.

두 클래스가 있다:
  PowerShellDaemon         — /ws(웹소켓 터미널) 전용. 연결마다 하나씩 뜨고 연결이 끊기면
                             같이 정리된다. stdin/stdout을 paas가 직접 PIPE로 물기 때문에
                             paas가 죽으면 이 프로세스도 죽는다 — 웹소켓 자체가 이미 paas의
                             생사에 묶여 있으므로 문제되지 않는다.
  BrokeredPowerShellDaemon — /exec(REST) 전용. paas가 재시작해도 세션이 죽지 않아야 하므로
                             직접 자식을 갖지 않고, 독립 브로커 프로세스(ps_broker.py)에
                             로컬 TCP로 붙는다. 실제 powershell.exe는 브로커가 소유해
                             paas 생사와 무관하게 살아남는다.
"""
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

# 분리의 단일 지점 — 실행기·인자를 여기서만 정한다.
POWERSHELL_EXE = "powershell.exe"
# -NoLogo: 배너 억제, 파이프된 stdin에서 REPL로 동작(명령을 한 줄씩 읽어 실행).
_ARGS = ["-NoProfile", "-NoLogo"]

# Windows 프로세스 생성 플래그 — paas 프로세스와 데몬을 분리하기 위한 것.
# BREAKAWAY_FROM_JOB이 핵심: nssm 등으로 paas가 Job Object에 묶여 있을 때, 데몬이 그 Job에서
# 벗어나 paas가 재시작(Stop-Process/Restart-Service)돼도 함께 죽지 않게 한다(self-kill 방지).
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _creation_flags(*, detached: bool, breakaway: bool) -> int:
    """비-Windows는 0. detached=True면 콘솔에서 완전 분리(파이프 불필요한 fire-and-forget용),
    아니면 창만 숨긴다(파이프로 계속 통신하는 상주 데몬용)."""
    if sys.platform != "win32":
        return 0
    flags = _CREATE_NEW_PROCESS_GROUP | (_DETACHED_PROCESS if detached else _CREATE_NO_WINDOW)
    if breakaway:
        flags |= _CREATE_BREAKAWAY_FROM_JOB
    return flags

MAX_OUTPUT_CHARS = 200_000
# 파이프된 stdin에서 PowerShell REPL은 입력 명령을 프롬프트와 함께 에코한다
# (예: `PS D:\proj> cmd /c exit 5`). 이 에코 줄은 실제 출력이 아니므로 걸러낸다.
_ECHO_RE = re.compile(r"^PS\b.*?>\s?")


@dataclass
class PsResult:
    output: str
    returncode: int | None


def _drain_until_marker(
    q: "queue.Queue[str | None]", marker: str, timeout: float,
) -> tuple[list[str], int | None, bool]:
    """큐에서 줄을 모아 marker 줄(또는 EOF·타임아웃)까지 읽는다.

    PowerShellDaemon(직접 파이프)과 BrokeredPowerShellDaemon(소켓)이 같은 마커/에코
    프로토콜을 쓰므로 이 부분은 전송 방식과 무관하게 공유한다.
    반환: (모은 출력 줄, 종료코드, EOF로 끝났는지). EOF면 호출측이 연결을 정리해야 한다.
    """
    lines: list[str] = []
    returncode: int | None = None
    deadline = time.monotonic() + timeout
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"PowerShell 명령이 {timeout:.0f}초 내에 끝나지 않았습니다.")
        try:
            line = q.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError(f"PowerShell 명령이 {timeout:.0f}초 내에 끝나지 않았습니다.")
        if line is None:  # 프로세스/연결 종료(EOF)
            return lines, returncode, True
        stripped = line.strip()
        # 실제 marker 출력 줄은 프롬프트 접두어 없이 marker로 시작한다.
        # (에코된 명령 줄은 `PS ...> Write-Output "<marker>..."`라 marker로 시작하지 않는다)
        if stripped.startswith(marker):
            tail = stripped[len(marker):].strip()
            if tail and tail.lstrip("-").isdigit():
                returncode = int(tail)
            return lines, returncode, False
        if _ECHO_RE.match(line):
            continue  # 프롬프트+에코된 입력 줄은 버린다
        lines.append(line)
        total += len(line)
        if total > MAX_OUTPUT_CHARS:
            lines.append("\n...(출력이 잘렸습니다)\n")
            # 남은 출력은 계속 소비해 다음 명령과 섞이지 않게 marker까지 읽는다.


class PowerShellDaemon:
    """장수 PowerShell 프로세스 하나를 감싼 세션. 한 번에 한 명령(_lock 직렬화)."""

    def __init__(self, cwd: str | None = None, executable: str = POWERSHELL_EXE):
        self._cwd = cwd or None
        self._exe = executable
        self._proc: subprocess.Popen | None = None
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        kwargs = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self._cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        # paas의 Job에서 벗어나(breakaway) paas 재시작 시 함께 죽지 않도록 띄운다.
        # Job이 breakaway를 불허하면 OSError가 나므로 플래그를 빼고 재시도한다.
        try:
            self._proc = subprocess.Popen(
                [self._exe, *_ARGS], creationflags=_creation_flags(detached=False, breakaway=True), **kwargs
            )
        except OSError:
            self._proc = subprocess.Popen(
                [self._exe, *_ARGS], creationflags=_creation_flags(detached=False, breakaway=False), **kwargs
            )
        self._q = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True)
        self._reader.start()

    def _read_loop(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                self._q.put(line)
        finally:
            self._q.put(None)  # EOF 신호

    def run(self, command: str, timeout: float = 30.0) -> PsResult:
        """명령을 데몬에서 실행하고 그 명령의 출력·종료코드를 반환한다. 세션 상태는 유지된다."""
        with self._lock:
            if not self.alive:
                self.start()
            assert self._proc is not None and self._proc.stdin is not None

            marker = f"__PAAS_PS_DONE_{uuid4().hex}__"
            try:
                # 명령 실행 후 sentinel과 마지막 종료코드를 함께 출력시켜 경계를 잡는다.
                self._proc.stdin.write(command + "\n")
                self._proc.stdin.write(f'Write-Output "{marker} $LASTEXITCODE"\n')
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self.stop()
                raise RuntimeError(f"PowerShell 데몬에 명령을 쓸 수 없습니다: {e}")

            lines, returncode, eof = _drain_until_marker(self._q, marker, timeout)
            if eof:
                self._proc = None
            return PsResult(output="".join(lines), returncode=returncode)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                try:
                    proc.stdin.write("exit\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                proc.stdin.close()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            proc.kill()

    @property
    def cwd(self) -> str | None:
        return self._cwd


def run_detached_script(script: str, cwd: str | None = None) -> None:
    """paas와 분리된 독립 PowerShell 프로세스로 스크립트를 실행한다(fire-and-forget).

    paas가 자기 자신을 재시작할 때(포트 해제·Restart-Service) 쓰는 경로 — 이 프로세스는
    paas의 Job에서 breakaway해 살아남으므로, paas 프로세스가 내려가도 재시작 작업이 끝까지
    진행된다(self-kill 방지). Job이 breakaway를 불허하면 플래그를 빼고 재시도한다.
    """
    args = [POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Command", script]
    try:
        subprocess.Popen(args, cwd=cwd, creationflags=_creation_flags(detached=True, breakaway=True), close_fds=True)
    except OSError:
        subprocess.Popen(args, cwd=cwd, creationflags=_creation_flags(detached=True, breakaway=False), close_fds=True)


def kill_broker(port: int) -> None:
    """브로커와 그 안의 powershell.exe를 완전히 끈다.

    평소 운영에서는 부르지 않는다 — 브로커는 paas 재시작을 넘어 살아있는 것이 정상
    동작이다(idle timeout으로 스스로 정리되거나, 관리자가 명시적으로 정리하고 싶을
    때만 쓴다). "exit"를 그대로 흘려보내면 powershell.exe가 끝나고, run_broker의
    메인 루프가 그걸 보고 브로커 자신도 정리한다(ps_broker.run_broker 참고).
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(b"exit\n")
    except OSError:
        pass  # 이미 없으면 할 일 없음


def _spawn_broker(port: int, cwd: str | None) -> None:
    """ps_broker를 paas와 분리된 프로세스로 띄운다 — run_detached_script와 같은 방식
    (breakaway, stdin/stdout/stderr 미공유)이라 paas가 죽어도 브로커는 살아남는다."""
    args = [sys.executable, "-m", "app.services.ps_broker", "--port", str(port)]
    if cwd:
        args += ["--cwd", cwd]
    try:
        subprocess.Popen(args, creationflags=_creation_flags(detached=True, breakaway=True), close_fds=True)
    except OSError:
        subprocess.Popen(args, creationflags=_creation_flags(detached=True, breakaway=False), close_fds=True)


def _socket_read_loop(sock: socket.socket, q: "queue.Queue[str | None]") -> None:
    """소켓에서 받은 바이트를 줄 단위로 잘라 큐에 넣는다(PowerShellDaemon._read_loop의
    소켓 버전) — 줄에는 `for line in ...` 텍스트 이터레이션과 동일하게 개행을 남긴다."""
    buf = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw_line, buf = buf.split(b"\n", 1)
                q.put(raw_line.decode("utf-8", errors="replace") + "\n")
    except OSError:
        pass
    finally:
        q.put(None)  # EOF 신호


class BrokeredPowerShellDaemon:
    """paas 재시작을 넘어 살아남는 상주 PowerShell 세션(REST /exec 전용).

    직접 자식 프로세스를 갖지 않는다 — 독립 브로커 프로세스(ps_broker.py)에 로컬 TCP로
    붙어 명령을 보낸다. 실제 powershell.exe 자식은 브로커가 소유하므로, paas가 죽어도
    (정상 종료든 강제 종료든) 브로커와 그 안의 세션은 그대로 남는다. 다음 paas가 같은
    포트로 다시 연결하면(shared_daemon()) 세션 상태(cd·변수)가 그대로 이어진다.

    stop()은 이 연결만 끊는다 — 브로커·powershell.exe는 건드리지 않는다(다른/다음 paas가
    계속 쓸 수 있어야 하므로). 브로커 자체는 아무도 재연결하지 않으면 스스로 정리된다
    (ps_broker.IDLE_TIMEOUT_SECONDS).
    """

    def __init__(self, port: int, cwd: str | None = None):
        self._port = port
        self._cwd = cwd
        self._sock: socket.socket | None = None
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def alive(self) -> bool:
        return self._sock is not None

    def _try_connect(self, timeout: float) -> socket.socket | None:
        try:
            return socket.create_connection(("127.0.0.1", self._port), timeout=timeout)
        except OSError:
            return None

    def _connect(self) -> None:
        sock = self._try_connect(3)
        if sock is None:
            # 브로커가 없다(첫 실행이거나 idle timeout으로 이미 정리됨) — 새로 띄우고
            # 리슨을 시작할 시간을 준 뒤 재시도한다.
            _spawn_broker(self._port, self._cwd)
            deadline = time.monotonic() + 10
            while sock is None and time.monotonic() < deadline:
                time.sleep(0.3)
                sock = self._try_connect(1)
            if sock is None:
                raise RuntimeError(f"PowerShell 브로커에 연결할 수 없습니다 (port={self._port}).")
        # _try_connect의 timeout은 연결 시도 자체에만 쓰고 싶다 — create_connection의
        # timeout은 연결 후에도 그 소켓의 기본 타임아웃으로 남아, 출력이 잠깐만 느려도
        # (그 타임아웃보다 오래 걸리는 명령) 리더 스레드가 EOF로 오해하고 세션을 끊어
        # 버린다. 명시적으로 블로킹 모드로 되돌린다 — 연결을 끊을 때는(stop()) 타임아웃이
        # 아니라 shutdown()으로 리더의 recv()를 즉시 깨운다.
        sock.settimeout(None)
        self._sock = sock
        self._q = queue.Queue()
        self._reader = threading.Thread(target=_socket_read_loop, args=(sock, self._q), daemon=True)
        self._reader.start()

    def run(self, command: str, timeout: float = 30.0) -> PsResult:
        """명령을 브로커의 PowerShell 세션에서 실행한다. 세션 상태는 paas 재시작 후에도 유지된다."""
        with self._lock:
            if not self.alive:
                self._connect()
            assert self._sock is not None

            marker = f"__PAAS_PS_DONE_{uuid4().hex}__"
            payload = (command + "\n" + f'Write-Output "{marker} $LASTEXITCODE"' + "\n").encode("utf-8")
            try:
                self._sock.sendall(payload)
            except OSError as e:
                self._teardown()
                raise RuntimeError(f"PowerShell 브로커에 명령을 쓸 수 없습니다: {e}")

            lines, returncode, eof = _drain_until_marker(self._q, marker, timeout)
            if eof:
                self._teardown()
            return PsResult(output="".join(lines), returncode=returncode)

    def _teardown(self) -> None:
        if self._sock is not None:
            # shutdown()을 close() 전에 부른다 — close()만으로는 이 소켓에서 recv()로
            # 블로킹 중인 리더 스레드(_socket_read_loop)가 깨어난다는 보장이 없다(POSIX
            # 상 정의되지 않은 동작 — 실제로 다음 recv() 타임아웃까지 그냥 블로킹된
            # 채로 남는다). shutdown(SHUT_RDWR)은 TCP 프로토콜 레벨에서 끊어(FIN) 그
            # recv()를 즉시 에러/EOF로 풀어준다.
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def stop(self) -> None:
        """이 연결만 닫는다 — 브로커와 그 안의 powershell.exe는 그대로 둔다."""
        with self._lock:
            self._teardown()


# --- 공유 데몬 (REST /exec 용 — 호출 간·paas 재시작 간 세션 유지) ---
_shared: BrokeredPowerShellDaemon | None = None
_shared_lock = threading.Lock()


def shared_daemon(cwd: str | None = None) -> BrokeredPowerShellDaemon:
    """프로세스 전역 공유 데몬 — 브로커에 붙는 클라이언트다.

    paas가 재시작해 이 함수가 다시 호출돼도(새 BrokeredPowerShellDaemon을 새로 만들어도)
    같은 고정 포트의 브로커에 다시 연결되므로 세션은 그대로 이어진다.
    """
    global _shared
    with _shared_lock:
        if _shared is None:
            from ..config import get_settings  # noqa: PLC0415

            _shared = BrokeredPowerShellDaemon(port=get_settings().ps_broker_port, cwd=cwd)
        return _shared


def shutdown_shared() -> None:
    """paas 종료 시 이 프로세스의 브로커 연결만 닫는다.

    브로커와 그 안의 powershell.exe는 그대로 둔다 — 파워셀 데몬은 paas가 죽어도 살아
    있어야 한다. 다음 paas가 shared_daemon()으로 같은 포트에 다시 붙으면 세션이 이어진다.
    """
    global _shared
    with _shared_lock:
        if _shared is not None:
            _shared.stop()
            _shared = None
