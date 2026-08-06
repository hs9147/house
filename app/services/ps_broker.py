"""독립 PowerShell 브로커 — paas와 분리된 별도 프로세스로 떠서, paas가 재시작해도
상주 PowerShell 세션(cd·변수 등 상태)이 죽지 않게 한다.

기존 PowerShellDaemon(powershell_daemon.py)은 powershell.exe의 stdin/stdout을 paas가
직접 PIPE로 물었다 — 그 파이프의 쓰기측을 paas가 들고 있으므로, paas 프로세스가 죽으면
(정상 종료든 강제 종료든) OS가 그 핸들을 닫고 powershell.exe는 표준입력 EOF로 스스로
끝났다. Job Object breakaway는 강제 종료 캐스케이드만 막을 뿐 이 경로는 못 막는다.

여기서는 powershell.exe의 stdin/stdout을 **이 브로커 프로세스**가 대신 물고, paas는
로컬 TCP로 이 브로커에 붙어 명령을 보낸다. 브로커는 명령 프로토콜을 전혀 모른다 —
소켓과 powershell.exe의 파이프 사이를 그대로 중계(relay)만 한다. 클라이언트(paas) 연결이
끊겨도 powershell.exe 자식은 살려 두고 다음 연결을 기다린다 — paas가 재시작해 같은
포트로 다시 붙으면 세션이 그대로 이어진다. 아무도 재연결하지 않고 IDLE_TIMEOUT_SECONDS가
지나면 스스로 정리하고 종료한다(영구 orphan 방지).

powershell.exe의 stdout은 브로커 전체에 **하나뿐인** 리더 스레드가 큐로 읽어 들인다 —
연결마다 새로 읽기 스레드를 만들면, 클라이언트가 명령 중간에 끊기고 새 클라이언트가
곧바로 붙었을 때 두 스레드가 같은 파이프를 동시에 읽는 경쟁이 생겨 출력이 엉뚱한 쪽으로
새 나갈 수 있다(_relay 참고) — 그래서 파이프 읽기와 "지금 붙은 연결에 전달"을 분리했다.

이 브로커 자신은 powershell_daemon.run_detached_script와 같은 방식(breakaway, paas와
stdin/stdout/stderr 미공유)으로 spawn된다 — powershell_daemon._spawn_broker 참고.
"""
import argparse
import queue
import socket
import subprocess
import threading

IDLE_TIMEOUT_SECONDS = 30 * 60  # 아무도 재연결하지 않으면 이 시간 뒤 스스로 종료한다
_RECV_BUFSIZE = 65536
_POLL_INTERVAL = 0.2  # 연결 종료를 감지하는 주기 — 이 안에서 두 릴레이 스레드가 항상 멈춘다


def run_broker(
    port: int,
    cwd: str | None = None,
    exe_args: list[str] | None = None,
    idle_timeout: float = IDLE_TIMEOUT_SECONDS,
) -> None:
    """호출한 프로세스가 이 함수에서 블로킹된다 — 별도 프로세스로 spawn되는 것을 전제로 한다.

    exe_args는 테스트에서 실제 powershell.exe 대신 가짜 REPL 스크립트를 넣기 위한 것 —
    프로덕션 기본값은 powershell_daemon.POWERSHELL_EXE/_ARGS.
    """
    if exe_args is None:
        from .powershell_daemon import _ARGS, POWERSHELL_EXE  # noqa: PLC0415
        exe_args = [POWERSHELL_EXE, *_ARGS]

    proc = subprocess.Popen(
        exe_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=cwd or None, bufsize=0,  # 언버퍼드 — 파이썬 쪽에서 배치를 기다리며 지연시키지 않는다
    )

    # powershell.exe의 stdout을 읽는 유일한 스레드 — 연결 유무와 무관하게 계속 돈다.
    out_q: "queue.Queue[bytes | None]" = queue.Queue()

    def read_stdout() -> None:
        try:
            assert proc.stdout is not None
            while True:
                data = proc.stdout.read(_RECV_BUFSIZE)
                if not data:
                    break
                out_q.put(data)
        except OSError:
            pass
        finally:
            out_q.put(None)  # EOF 신호 — powershell.exe가 끝났다

    threading.Thread(target=read_stdout, daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", port))
    except OSError:
        # 이미 다른 브로커(또는 다른 무언가)가 이 포트를 쓰고 있다 — powershell.exe 자식만
        # 정리하고 조용히 종료한다(중복 브로커를 띄우려 한 쪽이 대신 기존 것에 붙는다).
        _terminate(proc)
        server.close()
        return
    server.listen(1)
    server.settimeout(idle_timeout)

    try:
        while True:
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                break  # 오래 재연결이 없었다 — 정리하고 종료한다
            _relay(conn, proc, out_q)
            if proc.poll() is not None:
                break  # powershell.exe 자체가 끝났다 — 더 받아줄 세션이 없다
    finally:
        server.close()
        _terminate(proc)


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.terminate()
    except OSError:
        pass


def _relay(conn: socket.socket, proc: subprocess.Popen, out_q: "queue.Queue[bytes | None]") -> None:
    """한 클라이언트 연결이 살아있는 동안 소켓↔powershell.exe를 중계한다.

    stdout 읽기는 여기서 하지 않는다(run_broker의 단일 리더 스레드가 out_q로 넘긴다) —
    이 함수는 out_q를 소비해 지금 연결로 전달하는 것과, 연결에서 받은 입력을 stdin에
    쓰는 것만 한다. 둘 다 poll_interval마다 종료 신호를 확인하므로, 어느 한쪽이 끝나면
    (연결 종료 또는 powershell.exe 종료) 반환하기 전에 둘 다 확실히 멈춘다 — 이게 없으면
    다음 연결의 _relay가 시작한 새 소비자와 이번 연결의 소비자가 out_q를 동시에 두고
    경쟁해 출력이 엉뚱한 연결로 샐 수 있다.
    """
    stop = threading.Event()

    def sock_to_stdin() -> None:
        try:
            while not stop.is_set():
                conn.settimeout(_POLL_INTERVAL)
                try:
                    data = conn.recv(_RECV_BUFSIZE)
                except socket.timeout:
                    continue
                if not data:
                    break
                assert proc.stdin is not None
                proc.stdin.write(data)
                proc.stdin.flush()
        except OSError:
            pass
        finally:
            stop.set()

    def queue_to_sock() -> None:
        try:
            while not stop.is_set():
                try:
                    data = out_q.get(timeout=_POLL_INTERVAL)
                except queue.Empty:
                    continue
                if data is None:
                    out_q.put(None)  # 다음 연결도 EOF를 보게 되돌려 놓는다
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            stop.set()

    t_in = threading.Thread(target=sock_to_stdin, daemon=True)
    t_out = threading.Thread(target=queue_to_sock, daemon=True)
    t_in.start()
    t_out.start()
    # 둘 다 poll_interval 안에 stop을 보고 스스로 끝난다 — 여기서 반환하기 전에
    # 두 스레드가 실제로 끝났음을 보장해야 다음 연결의 out_q 소비자와 겹치지 않는다.
    t_in.join()
    t_out.join()
    try:
        conn.close()
    except OSError:
        pass


def _main() -> None:
    parser = argparse.ArgumentParser(description="paas PowerShell 브로커(내부용)")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cwd", default=None)
    args = parser.parse_args()
    run_broker(args.port, cwd=args.cwd)


if __name__ == "__main__":
    _main()
