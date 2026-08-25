"""상주 PowerShell 데몬 — 세션 유지·종료코드 캡처, /exec 호출 간 세션 유지.

powershell.exe가 있는 환경(주로 Windows)에서만 실제 실행을 검증한다. 브로커
중계·재연결 로직(paas 재시작 생존)은 가짜 REPL로 비-Windows에서도 검증한다.
"""
import json
import shutil
import socket as _socket_module
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import get_settings
from app.main import create_app

ADMIN = {"x-api-key": "test-admin-key"}

_no_powershell = shutil.which("powershell.exe") is None
skip_no_ps = pytest.mark.skipif(_no_powershell, reason="powershell.exe 미존재 (비-Windows)")


@skip_no_ps
def test_session_state_persists_across_commands():
    from app.services.powershell_daemon import PowerShellDaemon

    d = PowerShellDaemon()
    try:
        d.run("$paas_test_var = 4242")
        res = d.run("Write-Output $paas_test_var")
        assert "4242" in res.output  # 같은 프로세스라 변수가 유지된다
    finally:
        d.stop()
    assert d.alive is False


@skip_no_ps
def test_returncode_captured():
    from app.services.powershell_daemon import PowerShellDaemon

    d = PowerShellDaemon()
    try:
        res = d.run("cmd /c exit 5")
        assert res.returncode == 5
    finally:
        d.stop()


@skip_no_ps
def test_exec_endpoint_shares_session(monkeypatch, fresh_settings):
    from app.config import get_settings
    from app.services import powershell_daemon

    monkeypatch.setattr(powershell_daemon, "_shared", None)
    monkeypatch.setenv("PAAS_PS_BROKER_PORT", str(_free_port()))
    get_settings.cache_clear()
    c = TestClient(create_app())
    try:
        r1 = c.post("/paas/api/v1/system/powershell/exec",
                    json={"command": "$paas_ep_var = 'hello-daemon'"}, headers=ADMIN)
        assert r1.status_code == 200, r1.text
        r2 = c.post("/paas/api/v1/system/powershell/exec",
                    json={"command": "Write-Output $paas_ep_var"}, headers=ADMIN)
        assert r2.status_code == 200, r2.text
        assert "hello-daemon" in r2.json()["output"]  # 호출 간 세션 유지
    finally:
        powershell_daemon.shutdown_shared()
        powershell_daemon.kill_broker(get_settings().ps_broker_port)  # 테스트가 띄운 브로커 정리


def test_exec_requires_command_field():
    """빈 command는 400 (powershell 없이도 검증되는 입력 검사)."""
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/system/powershell/exec", json={"command": "   "}, headers=ADMIN)
    assert r.status_code == 400


# --- run_detached_script — "paas가 stop돼도 동작해야 한다"의 실제 근거 ---
#
# 실제 Job Object 강제(Windows 전용)는 이 샌드박스에서 실행해 확인할 수 없다. 대신
# subprocess.Popen에 넘기는 인자 자체를 검증한다 — 이게 맞아야 실제 Windows에서도
# 살아남는다는 전제가 성립한다. 두 가지가 그 전제다:
#   1. CREATE_BREAKAWAY_FROM_JOB이 실제로 요청된다(그래야 nssm의 Job이 paas를 죽여도
#      같이 죽지 않는다) — Job이 breakaway를 불허하면 플래그 없이 재시도.
#   2. stdin/stdout/stderr를 paas에 물리지 않는다(파이프를 물리면 paas가 죽는 순간
#      그 파이프가 닫혀 자식도 따라 끝난다 — breakaway와 무관하게 죽는 경로).


def test_creation_flags_windows_requests_breakaway_and_detached(monkeypatch):
    from app.services import powershell_daemon as psd

    monkeypatch.setattr(psd.sys, "platform", "win32")
    flags = psd._creation_flags(detached=True, breakaway=True)
    assert flags & psd._CREATE_BREAKAWAY_FROM_JOB
    assert flags & psd._DETACHED_PROCESS
    assert flags & psd._CREATE_NEW_PROCESS_GROUP


def test_creation_flags_windows_fallback_omits_breakaway(monkeypatch):
    from app.services import powershell_daemon as psd

    monkeypatch.setattr(psd.sys, "platform", "win32")
    flags = psd._creation_flags(detached=True, breakaway=False)
    assert not (flags & psd._CREATE_BREAKAWAY_FROM_JOB)


def test_creation_flags_non_windows_is_always_zero(monkeypatch):
    """비-Windows에는 Job Object가 없어 이 보호 자체가 성립하지 않는다 — 항상 0."""
    from app.services import powershell_daemon as psd

    monkeypatch.setattr(psd.sys, "platform", "linux")
    assert psd._creation_flags(detached=True, breakaway=True) == 0
    assert psd._creation_flags(detached=False, breakaway=False) == 0


def test_run_detached_script_requests_breakaway_first(monkeypatch):
    from app.services import powershell_daemon as psd

    monkeypatch.setattr(psd.sys, "platform", "win32")
    calls = []

    class _FakeProc:
        pass

    def fake_popen(args, **kwargs):
        calls.append(kwargs)
        return _FakeProc()
    monkeypatch.setattr(psd.subprocess, "Popen", fake_popen)

    psd.run_detached_script("Write-Host hi", cwd="/tmp/x")

    assert len(calls) == 1
    assert calls[0]["creationflags"] & psd._CREATE_BREAKAWAY_FROM_JOB
    # stdin/stdout/stderr를 paas와 공유하지 않는다 — paas가 죽어도 이 프로세스의
    # 표준 입출력은 끊기지 않는다(파이프 EOF로 인한 자기 종료가 없다).
    assert "stdin" not in calls[0] and "stdout" not in calls[0] and "stderr" not in calls[0]


def test_run_detached_script_falls_back_when_breakaway_rejected(monkeypatch):
    """Job이 breakaway를 불허하면(OSError) 플래그 없이 한 번 더 시도한다 — 완전히
    실패 처리하지 않는다(스크립트 자체는 여전히 실행돼야 한다)."""
    from app.services import powershell_daemon as psd

    monkeypatch.setattr(psd.sys, "platform", "win32")
    calls = []

    class _FakeProc:
        pass

    def fake_popen(args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise OSError("Access is denied (breakaway not permitted by Job)")
        return _FakeProc()
    monkeypatch.setattr(psd.subprocess, "Popen", fake_popen)

    psd.run_detached_script("Write-Host hi")

    assert len(calls) == 2
    assert calls[0]["creationflags"] & psd._CREATE_BREAKAWAY_FROM_JOB
    assert not (calls[1]["creationflags"] & psd._CREATE_BREAKAWAY_FROM_JOB)


# --- /exec 상주 데몬도 이제 브로커를 거쳐 paas 재시작을 넘어 살아남는다 ---
#
# 예전에는 PowerShellDaemon이 stdin/stdout을 paas가 직접 PIPE로 물었다 — 그 파이프의
# 쓰기측을 paas가 들고 있어, paas가 죽으면(정상 종료든 강제 종료든) OS가 핸들을 닫고
# 데몬이 표준입력 EOF로 스스로 끝났다. breakaway는 강제 종료 캐스케이드만 막을 뿐 이
# 경로에는 무관했고, main.py의 atexit(shutdown_shared)도 정상 종료 때마다 데몬을
# 직접 정리했다.
#
# 이제 실제 powershell.exe는 독립 브로커 프로세스(ps_broker.py)가 소유한다. paas는
# BrokeredPowerShellDaemon(로컬 TCP 클라이언트)로 그 브로커에 붙을 뿐이다. paas가
# 죽어도 브로커는 살아남고, shutdown_shared()도 이제 "이 연결만 끊기"로 의미가
# 바뀌어 브로커·powershell.exe는 건드리지 않는다 — 다음(재시작된) paas가 같은 포트로
# 다시 붙으면 세션(cd·변수)이 그대로 이어진다. 아래는 real powershell.exe 없이도
# 이 관계 자체를 검증하는 테스트라 비-Windows에서도 실행된다(가짜 REPL로 브로커의
# 중계·재연결 로직을 그대로 돈다) — run_detached_script 검증과 같은 원리다.

# 마커 프로토콜(Write-Output "<marker> $LASTEXITCODE")만 이해하면 되는 최소 가짜 REPL.
# PS 프롬프트 에코는 흉내내지 않는다 — 브로커는 프로토콜을 모르는 순수 중계이고, 에코
# 필터링(_ECHO_RE)은 클라이언트 쪽 로직이라 실제 powershell.exe 없이는 검증 대상이 아니다.
_FAKE_SHELL_SRC = r"""
import re, sys
variables = {}
last_exit = 0
for raw in sys.stdin:
    line = raw.rstrip("\n")
    if line.strip() == "exit":
        break
    m = re.match(r"^cmd /c exit (-?\d+)$", line.strip())
    if m:
        last_exit = int(m.group(1))
        continue
    m = re.match(r"^\$(\w+)\s*=\s*(.*)$", line.strip())
    if m:
        name, val = m.group(1), m.group(2).strip()
        if val.startswith("'") and val.endswith("'"):
            val = val[1:-1]
        variables[name] = val
        continue
    m = re.match(r'^Write-Output "(.*)"$', line.strip())
    if m:
        text = m.group(1).replace("$LASTEXITCODE", str(last_exit))
        text = re.sub(r"\$(\w+)", lambda mo: variables.get(mo.group(1), ""), text)
        print(text)
        sys.stdout.flush()
        continue
    m = re.match(r"^Write-Output \$(\w+)$", line.strip())
    if m:
        print(variables.get(m.group(1), ""))
        sys.stdout.flush()
"""
_FAKE_SHELL_ARGS = [sys.executable, "-u", "-c", _FAKE_SHELL_SRC]


def _free_port() -> int:
    with _socket_module.socket(_socket_module.AF_INET, _socket_module.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_fake_broker(port: int, idle_timeout: float = 5) -> threading.Thread:
    from app.services import ps_broker

    t = threading.Thread(
        target=ps_broker.run_broker,
        kwargs=dict(port=port, exe_args=_FAKE_SHELL_ARGS, idle_timeout=idle_timeout),
        daemon=True,
    )
    t.start()
    time.sleep(0.3)  # 브로커가 리슨을 시작할 시간
    return t


def test_broker_relay_preserves_session_across_client_reconnect():
    """클라이언트 A가 세션에 변수를 심고 연결을 끊은 뒤(paas 재시작을 흉내낸다),
    새 클라이언트 B가 같은 포트에 다시 붙으면 그 변수를 그대로 본다."""
    from app.services.powershell_daemon import BrokeredPowerShellDaemon, kill_broker

    port = _free_port()
    t = _start_fake_broker(port)
    try:
        client_a = BrokeredPowerShellDaemon(port=port)
        client_a.run("$paas_test = 'hello'")
        client_a.stop()  # 연결만 끊는다 — powershell(가짜 셸)은 브로커가 계속 물고 있다

        client_b = BrokeredPowerShellDaemon(port=port)  # "재시작된 paas"의 새 클라이언트
        res = client_b.run('Write-Output "$paas_test"')
        assert "hello" in res.output
        client_b.stop()
    finally:
        kill_broker(port)
        t.join(timeout=3)


def test_connected_socket_has_no_lingering_recv_timeout():
    """회귀: create_connection(timeout=3)이 연결 후에도 소켓 기본 타임아웃으로 남으면,
    출력이 3초보다 느린 정상 명령이 스스로 EOF로 오해받아 세션이 끊긴다. 연결 후에는
    블로킹 모드로 되돌려야 한다(연결을 끊을 때는 shutdown()으로 즉시 깨운다)."""
    from app.services.powershell_daemon import BrokeredPowerShellDaemon, kill_broker

    port = _free_port()
    t = _start_fake_broker(port)
    try:
        client = BrokeredPowerShellDaemon(port=port)
        client.run('Write-Output "warm-up"')
        assert client._sock.gettimeout() is None
        client.stop()
    finally:
        kill_broker(port)
        t.join(timeout=3)


def test_shutdown_shared_disconnects_but_leaves_broker_running():
    """main.py의 atexit이 부르는 shutdown_shared()는 이제 이 프로세스의 연결만 끊는다 —
    브로커·세션은 죽지 않는다(paas가 죽어도 파워셀 데몬은 동작해야 한다는 요구사항)."""
    from app.services import powershell_daemon as psd

    port = _free_port()
    t = _start_fake_broker(port)
    try:
        d = psd.BrokeredPowerShellDaemon(port=port)
        d.run("$paas_survive = 'yes'")
        psd._shared = d
        try:
            psd.shutdown_shared()  # "paas 종료"를 흉내낸다
        finally:
            psd._shared = None

        # 새 클라이언트("재시작된 paas")가 같은 브로커에 다시 붙어 세션을 그대로 본다
        d2 = psd.BrokeredPowerShellDaemon(port=port)
        res = d2.run('Write-Output "$paas_survive"')
        assert "yes" in res.output
        d2.stop()
    finally:
        psd.kill_broker(port)
        t.join(timeout=3)


def test_client_spawns_broker_when_none_running(monkeypatch):
    """붙을 브로커가 아직 없으면(첫 실행, 또는 idle timeout으로 이미 정리됨) 새로
    띄우고 재시도해서 붙는다."""
    from app.services import powershell_daemon as psd

    port = _free_port()
    spawned = []

    def fake_spawn(p, cwd):
        spawned.append(p)
        _start_fake_broker(p)
    monkeypatch.setattr(psd, "_spawn_broker", fake_spawn)

    try:
        client = psd.BrokeredPowerShellDaemon(port=port)
        res = client.run('Write-Output "spawned-ok"')
        assert "spawned-ok" in res.output
        assert spawned == [port]
        client.stop()
    finally:
        psd.kill_broker(port)


def test_spawn_broker_requests_breakaway_and_no_shared_io(monkeypatch):
    """_spawn_broker도 run_detached_script와 같은 방식으로 뜬다 — 브로커가 paas의
    Job에서 벗어나고, stdin/stdout/stderr를 paas와 공유하지 않는다(둘을 통일하라는
    요구사항)."""
    from app.services import powershell_daemon as psd

    monkeypatch.setattr(psd.sys, "platform", "win32")
    calls = []

    class _FakeProc:
        pass

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return _FakeProc()
    monkeypatch.setattr(psd.subprocess, "Popen", fake_popen)

    psd._spawn_broker(47231, "/tmp/x")

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:3] == [psd.sys.executable, "-m", "app.services.ps_broker"]
    assert "--port" in args and "47231" in args
    assert kwargs["creationflags"] & psd._CREATE_BREAKAWAY_FROM_JOB
    assert "stdin" not in kwargs and "stdout" not in kwargs and "stderr" not in kwargs


# --- WebSocket 터미널 인증 ---

WS_URL = "/paas/api/v1/system/powershell/ws"


def test_websocket_terminal_refuses_anonymous_connections(fresh_settings):
    """이 엔드포인트는 관리자 셸을 그대로 내준다 — 인증 없이 붙을 수 있으면
    플랫폼에 닿는 누구나 서비스 계정 권한으로 명령을 실행할 수 있다(실제로 그랬다)."""
    c = TestClient(create_app())
    with pytest.raises(WebSocketDisconnect):  # close 1008
        with c.websocket_connect(WS_URL) as ws:
            _recv(ws)


def test_websocket_terminal_refuses_a_wrong_key(fresh_settings):
    c = TestClient(create_app())
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(
            # 서브프로토콜은 HTTP 헤더라 ASCII만 실린다 — 플랫폼 키는
            # secrets.token_urlsafe라 언제나 ASCII다.
            WS_URL, subprotocols=["paas-terminal", "paas-key.wrong-key"]
        ) as ws:
            _recv(ws)


def test_websocket_terminal_refuses_a_non_admin_key(fresh_settings):
    """발급 키는 앱 환경변수로도 나가는 값이다 — 그것으로 셸이 열리면 안 된다."""
    c = TestClient(create_app())
    issued = c.post("/paas/api/v1/keys", headers=ADMIN,
                    json={"name": "worker", "is_admin": False})
    assert issued.status_code == 201, issued.text
    raw = issued.json()["key"]
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(
            WS_URL, subprotocols=["paas-terminal", f"paas-key.{raw}"]
        ) as ws:
            _recv(ws)


ADMIN_SUBPROTOCOLS = ["paas-terminal", "paas-key.test-admin-key"]
skip_no_posix_pty = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX PTY 전용 (윈도우는 pywinpty 경로)")


_MARK = "MARK-끝"


def _recv(ws, timeout: float = 5.0) -> str:
    """타임아웃이 있는 receive_text.

    TestClient의 receive_text에는 타임아웃이 없어서, 구현이 깨지면 테스트가 **실패하지
    않고 매달린다** — CI에서는 "한참 뒤에 전체가 죽는" 형태로만 드러나 원인을 찾기 어렵다.
    데몬 스레드로 읽어 시간을 재고, 넘기면 그 자리에서 실패시킨다.
    """
    box: dict[str, object] = {}

    def read():
        try:
            box["value"] = ws.receive_text()
        except BaseException as e:  # noqa: BLE001 — 끊김도 그대로 올린다
            box["error"] = e

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    worker.join(timeout)
    if "error" in box:
        raise box["error"]
    if "value" not in box:
        raise AssertionError(f"{timeout}초 안에 터미널 출력이 오지 않았다")
    return str(box["value"])


def _drain_to_mark(ws, tries: int = 400) -> str:
    """표식 명령을 하나 흘려보내고 그 결과가 보일 때까지 모은다.

    찾는 문자열이 나올 때까지 기다리면, 기능이 깨졌을 때 테스트가 **실패하지 않고
    매달린다**(receive_text에는 타임아웃이 없다). 표식은 셸이 살아 있는 한 무슨 일이
    있어도 오므로, 기대한 것이 없으면 그 자리에서 실패한다.
    """
    ws.send_text(json.dumps({"type": "input", "data": f"echo {_MARK}\n"}))
    seen = ""
    for _ in range(tries):
        seen += _recv(ws)
        if seen.count(_MARK) >= 2:  # 입력 에코 + 실행 결과
            return seen
    raise AssertionError(f"표식을 못 봤다: {seen[-300:]!r}")


def _run(ws, script: str) -> str:
    ws.send_text(json.dumps({"type": "input", "data": script}))
    return _drain_to_mark(ws)


@skip_no_posix_pty
def test_websocket_terminal_accepts_an_admin_key(monkeypatch, fresh_settings):
    """키는 서브프로토콜로 받는다 — 쿼리스트링이면 IIS/ARR 접근 로그에 그대로 남는다."""
    monkeypatch.setenv("PAAS_PTY_SHELL", "/bin/sh")
    get_settings.cache_clear()
    c = TestClient(create_app())
    with c.websocket_connect(WS_URL, subprotocols=ADMIN_SUBPROTOCOLS) as ws:
        assert "hello-pty" in _run(ws, "echo hello-pty\n")
    # 셸을 연 주체가 감사 로그에 남는다
    actions = {r["action"] for r in c.get("/paas/api/v1/audit", headers=ADMIN).json()}
    assert "powershell.ws_open" in actions


@skip_no_posix_pty
def test_websocket_terminal_carries_an_interactive_prompt(monkeypatch, fresh_settings):
    """예전 줄 단위 구현이 못 하던 것 — 되묻는 명령이 그대로 멈춰 있었다."""
    monkeypatch.setenv("PAAS_PTY_SHELL", "/bin/sh")
    get_settings.cache_clear()
    c = TestClient(create_app())
    with c.websocket_connect(WS_URL, subprotocols=ADMIN_SUBPROTOCOLS) as ws:
        # read는 입력이 올 때까지 멈춰 있다 — 예전 줄 단위 구현이 여기서 걸려 있었다.
        ws.send_text(json.dumps({"type": "input", "data": "read x; echo got=$x\n"}))
        ws.send_text(json.dumps({"type": "input", "data": "응답값\n"}))
        assert "got=응답값" in _drain_to_mark(ws)


@skip_no_posix_pty
def test_websocket_terminal_applies_resize(monkeypatch, fresh_settings):
    """창 크기를 셸이 모르면 줄바꿈이 어긋난다 — resize가 실제로 전달돼야 한다."""
    monkeypatch.setenv("PAAS_PTY_SHELL", "/bin/sh")
    get_settings.cache_clear()
    c = TestClient(create_app())
    with c.websocket_connect(WS_URL, subprotocols=ADMIN_SUBPROTOCOLS) as ws:
        ws.send_text(json.dumps({"type": "resize", "cols": 81, "rows": 41}))
        assert "41 81" in _run(ws, "stty size\n")


@skip_no_posix_pty
def test_websocket_terminal_ignores_frames_outside_the_protocol(monkeypatch, fresh_settings):
    """규약에 없는 프레임을 셸에 흘려보내면 붙은 쪽이 의도치 않게 명령을 실행시킨다."""
    monkeypatch.setenv("PAAS_PTY_SHELL", "/bin/sh")
    get_settings.cache_clear()
    c = TestClient(create_app())
    with c.websocket_connect(WS_URL, subprotocols=ADMIN_SUBPROTOCOLS) as ws:
        ws.send_text("echo 날것으로-보낸-명령\n")            # JSON이 아니다
        assert "날것으로-보낸-명령" not in _drain_to_mark(ws)


def test_websocket_terminal_says_what_to_install_when_there_is_no_backend(
        monkeypatch, fresh_settings):
    """빈 화면만 남기지 않는다 — 무엇을 하면 되는지 터미널에 찍어 준다."""
    from app.services import pty_terminal

    def unavailable(*args, **kwargs):
        raise pty_terminal.PtyUnavailable(pty_terminal.INSTALL_HINT)

    monkeypatch.setattr(pty_terminal, "PtyTerminal", unavailable)
    c = TestClient(create_app())
    with c.websocket_connect(WS_URL, subprotocols=ADMIN_SUBPROTOCOLS) as ws:
        message = _recv(ws)
    assert "pip install pywinpty" in message
    assert "PAAS_PTY_BACKEND=winpty" in message  # Server 2016은 ConPTY가 없다


def test_unknown_pty_backend_is_rejected_by_name(fresh_settings):
    from app.services import pty_terminal

    assert pty_terminal.backend_code("") is None
    assert pty_terminal.backend_code("winpty") == 1
    assert pty_terminal.backend_code("conpty") == 0
    with pytest.raises(pty_terminal.PtyUnavailable, match="알 수 없는"):
        pty_terminal.backend_code("openssh")
