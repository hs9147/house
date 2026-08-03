"""상주 PowerShell 데몬 — 명령마다 powershell.exe를 새로 띄우던 것을 분리한다.

기존 system.py는 /exec·/ws에서 명령마다 `subprocess.run(["powershell.exe", ...])`으로
프로세스를 새로 띄웠다. 그래서 (1) 세션 상태(cd·변수·import)가 명령 간에 유지되지 않았고,
(2) 동기 subprocess.run이 async WebSocket 핸들러 안에서 이벤트 루프를 블로킹했다.

여기서는 장수(long-lived) PowerShell 프로세스를 하나 띄워 stdin으로 명령을 흘려보내고,
전용 리더 스레드가 stdout을 큐로 모은다. 명령 뒤에 고유 sentinel을 출력시켜 그 명령의
출력 경계를 잡는다 — 같은 프로세스라 세션 상태가 유지된다. API는 run()을 스레드풀/
to_thread로 호출해 이벤트 루프를 블로킹하지 않는다.
"""
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

# 분리의 단일 지점 — 실행기·인자를 여기서만 정한다.
POWERSHELL_EXE = "powershell.exe"
# -NoLogo: 배너 억제, 파이프된 stdin에서 REPL로 동작(명령을 한 줄씩 읽어 실행).
_ARGS = ["-NoProfile", "-NoLogo"]

MAX_OUTPUT_CHARS = 200_000
# 파이프된 stdin에서 PowerShell REPL은 입력 명령을 프롬프트와 함께 에코한다
# (예: `PS D:\proj> cmd /c exit 5`). 이 에코 줄은 실제 출력이 아니므로 걸러낸다.
_ECHO_RE = re.compile(r"^PS\b.*?>\s?")


@dataclass
class PsResult:
    output: str
    returncode: int | None


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
        self._proc = subprocess.Popen(
            [self._exe, *_ARGS],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self._cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
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

            lines: list[str] = []
            returncode: int | None = None
            deadline = time.monotonic() + timeout
            total = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"PowerShell 명령이 {timeout:.0f}초 내에 끝나지 않았습니다.")
                try:
                    line = self._q.get(timeout=remaining)
                except queue.Empty:
                    raise TimeoutError(f"PowerShell 명령이 {timeout:.0f}초 내에 끝나지 않았습니다.")
                if line is None:  # 프로세스 종료(EOF)
                    self._proc = None
                    break
                stripped = line.strip()
                # 실제 marker 출력 줄은 프롬프트 접두어 없이 marker로 시작한다.
                # (에코된 명령 줄은 `PS ...> Write-Output "<marker>..."`라 marker로 시작하지 않는다)
                if stripped.startswith(marker):
                    tail = stripped[len(marker):].strip()
                    if tail and tail.lstrip("-").isdigit():
                        returncode = int(tail)
                    break
                if _ECHO_RE.match(line):
                    continue  # 프롬프트+에코된 입력 줄은 버린다
                lines.append(line)
                total += len(line)
                if total > MAX_OUTPUT_CHARS:
                    lines.append("\n...(출력이 잘렸습니다)\n")
                    # 남은 출력은 계속 소비해 다음 명령과 섞이지 않게 marker까지 읽는다.

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


# --- 공유 데몬 (REST /exec 용 — 호출 간 세션 유지) ---
_shared: PowerShellDaemon | None = None
_shared_lock = threading.Lock()


def shared_daemon(cwd: str | None = None) -> PowerShellDaemon:
    """프로세스 전역 공유 데몬. 죽었으면 지연 재기동한다."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = PowerShellDaemon(cwd=cwd)
        if not _shared.alive:
            _shared.start()
        return _shared


def shutdown_shared() -> None:
    """앱 종료 시 공유 데몬을 정리한다."""
    global _shared
    with _shared_lock:
        if _shared is not None:
            _shared.stop()
            _shared = None
