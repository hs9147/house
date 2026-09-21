"""개발 배포 런타임 — dev 서버를 맨 프로세스로 띄운다(nssm·docker 없음).

**왜 별도 런타임인가.** development 프로필은 이미 dev 서버로 돈다 — build.py가 생성하는
start.cmd에 `PAAS_PROFILE=development`면 `vite --host --port --base` 또는 `npm run dev`로
가는 분기가 있다. 문제는 그것을 **Windows Service로 감싼다**는 것이었다: 배포마다
`nssm install` → `set` ×4 → `start` → 정지 시 `nssm remove` 사이클이 돌고, 서비스 등록에
관리자 권한이 필요하다. 정작 dev 서버는 HMR로 파일 변경을 스스로 반영하므로, 코드를
고칠 때마다 서비스를 다시 등록할 이유가 없다.

**무엇을 그대로 쓰는가.** 실행 방법은 start.cmd가 이미 안다 — 여기서 명령을 다시 만들지
않는다. 타입별 실행 규칙을 두 곳에 두면 갈라지고, 그러면 "release는 되는데 dev는 안 되는"
프로젝트가 생긴다. 이 런타임이 바꾸는 것은 **누가 그 스크립트를 감독하는가**뿐이다:
nssm 서비스 → 분리된 프로세스 + PID 파일.

**수명.** 띄운 동안만 산다. PID 파일이 있어 다음 paas 프로세스도 정지시킬 수 있고(Job
breakaway로 paas 재시작을 견딘다), **서버 재부팅·프로세스 크래시는 복구하지 않는다.**
그래서 status()가 PID와 포트를 실제로 확인한다 — 죽었는데 화면이 running이라고 말하면
그게 더 나쁘다. 운영(release)은 여전히 nssm/docker를 쓴다.

URL rewrite는 그대로 필요하다 — 프록시가 서브패스로 라우팅하고, start.cmd가 dev 서버에
같은 base를 넘긴다(PAAS_BASE_PATH). 이 런타임은 그 값을 환경변수로 전달할 뿐이다.
"""
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from ...config import get_settings
from ...models import BuildProfile
from .base import Endpoint, Runtime, RuntimeSpec
# 같은 호스트 이름(프록시 업스트림)과 로그 tail 읽기를 공유한다 — 두 런타임이 같은
# 머신의 같은 프로세스를 다루므로, 이름이 갈라지면 프록시가 ::1로 붙어 502가 난다
# (windows_service_runtime의 UPSTREAM_HOST 주석 참고).
from .windows_service_runtime import UPSTREAM_HOST, _read_log_tail

# dev 서버는 첫 기동에 의존성 그래프를 훑어(vite의 optimize) 시간이 걸린다. nssm 경로와
# 같은 창을 준다 — 여기서 짧게 끊으면 정상 기동을 실패로 보고한다.
_HEALTH_TIMEOUT = 90.0
_HEALTH_INTERVAL = 1.0
_STOP_TIMEOUT = 20.0


class DevProcessError(RuntimeError):
    pass


def _pid_path(unit_name: str) -> Path:
    """PID 파일 — build_log_dir에 둔다. 리포 안에 두면 사용자 커밋에 섞인다."""
    return get_settings().build_log_dir / f"{unit_name}.pid"


def _log_path(unit_name: str) -> Path:
    return get_settings().build_log_dir / f"{unit_name}.log"


def _read_pid(unit_name: str) -> int | None:
    try:
        return int(_pid_path(unit_name).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """이 PID가 아직 살아 있는지. 죽은 PID 파일이 남아 있는 것이 정상 상황이다."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True, text=True, errors="replace", timeout=10,
    )
    # tasklist는 못 찾아도 종료 코드 0이다 — 출력에 PID가 있는지로 본다.
    return str(pid) in (out.stdout or "")


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((UPSTREAM_HOST, port)) == 0


class DevProcessRuntime(Runtime):
    def start(self, spec: RuntimeSpec) -> Endpoint:
        settings = get_settings()
        workdir = settings.work_dir / spec.project_name
        if spec.component:
            workdir = workdir / spec.component
        start_script = workdir / "start.cmd"
        if not start_script.exists():
            raise DevProcessError(
                "개발 배포는 리포 루트에 start.cmd가 필요합니다 "
                f"(배포 시 자동 생성됨 — build.write_start_script): {start_script}"
            )
        if spec.host_port is None:
            raise DevProcessError(
                "개발 배포는 플랫폼이 배정한 호스트 포트가 필요합니다(services/ports.py)."
            )
        port = spec.host_port

        # 같은 유닛이 이미 떠 있으면 먼저 내린다. 블루-그린을 하지 않는 이유: dev 서버는
        # 무중단 교체가 목적이 아니고(보는 사람이 새로 고치면 된다), 두 개를 동시에 띄우면
        # 포트가 둘 필요해지는데 그 값이 프록시 설정과 어긋난다.
        self._stop_unit(spec.unit_name)

        log_path = _log_path(spec.unit_name)
        env = {
            **os.environ,
            **spec.env,
            "PORT": str(port),
            "HOST": UPSTREAM_HOST,
            # start.cmd가 dev 서버로 띄울지 판단하고, dev 서버에 줄 base를 얻는다.
            "PAAS_PROFILE": spec.profile.value,
            "PAAS_BASE_PATH": spec.base_path,
        }
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [comspec, "/c", str(start_script)],
                cwd=str(workdir), env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=_creation_flags(),
            )
        _pid_path(spec.unit_name).write_text(str(proc.pid), encoding="utf-8")

        if not self._wait_healthy(port, spec.health_check_path):
            tail = _read_log_tail(log_path)
            self._stop_unit(spec.unit_name)
            raise DevProcessError(
                f"health check failed — http://{UPSTREAM_HOST}:{port}{spec.health_check_path} 로 "
                f"{int(_HEALTH_TIMEOUT)}초 내 응답이 없습니다. dev 서버가 HOST={UPSTREAM_HOST} "
                f"PORT={port}를 지켜 리슨하는지 확인하세요.\n"
                f"--- {log_path.name} ---\n{tail}"
            )
        return Endpoint(host=UPSTREAM_HOST, port=port)

    def stop(self, project_name: str, profile: BuildProfile) -> None:
        self._stop_unit(RuntimeSpec(project_name, "", 0, profile, "").unit_name)

    def status(self, project_name: str, profile: BuildProfile) -> str:
        """PID와 포트를 실제로 확인한다 — 이 런타임은 크래시를 복구하지 않으므로
        '떠 있다고 기록돼 있다'와 '떠 있다'가 갈라진다."""
        unit = RuntimeSpec(project_name, "", 0, profile, "").unit_name
        pid = _read_pid(unit)
        if pid is None or not _pid_alive(pid):
            return "stopped"
        return "running"

    def logs(self, project_name: str, profile: BuildProfile, tail: int = 200) -> str:
        unit = RuntimeSpec(project_name, "", 0, profile, "").unit_name
        return _read_log_tail(_log_path(unit), tail)

    # --- 내부 ---

    def _wait_healthy(self, port: int, health_path: str) -> bool:
        url = f"http://{UPSTREAM_HOST}:{port}{health_path if health_path.startswith('/') else '/' + health_path}"
        deadline = time.monotonic() + _HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(_HEALTH_INTERVAL)
            # 포트가 열렸는지 먼저 본다 — dev 서버는 라우팅을 늦게 붙여 HTTP가 404를
            # 줄 수도 있는데, 그건 "떴다"는 뜻이다.
            if not _port_open(port):
                continue
            try:
                with urllib.request.urlopen(url, timeout=5) as res:
                    if res.status < 500:
                        return True
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return True
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            else:
                continue
        return _port_open(port)

    def _stop_unit(self, unit_name: str) -> None:
        """PID 트리를 끝낸다. cmd.exe가 node를 자식으로 띄우므로 트리째 죽여야 한다 —
        부모만 죽이면 dev 서버가 포트를 물고 남아 다음 배포가 그 포트에서 막힌다."""
        pid = _read_pid(unit_name)
        _pid_path(unit_name).unlink(missing_ok=True)
        if pid is None or not _pid_alive(pid):
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, errors="replace",
                           timeout=_STOP_TIMEOUT)
        else:
            try:
                os.killpg(os.getpgid(pid), 15)
            except OSError:
                pass


def _creation_flags() -> int:
    """창을 숨기고 Job에서 떼어낸다.

    **DETACHED_PROCESS를 쓰지 않는다** — 콘솔이 없으면 cmd.exe가 시작하지 못할 수 있고,
    같은 함정을 powershell_daemon.run_detached_script에서 이미 겪었다(Popen은 성공하고
    아무 일도 일어나지 않는다). breakaway는 nssm이 paas를 Job에 묶어 둔 설치본에서 dev
    서버가 paas 재시작에 따라 죽지 않게 한다 — 띄운 동안은 살아야 한다.
    """
    if os.name != "nt":
        return 0
    create_new_process_group = 0x00000200
    create_no_window = 0x08000000
    create_breakaway_from_job = 0x01000000
    return create_new_process_group | create_no_window | create_breakaway_from_job
