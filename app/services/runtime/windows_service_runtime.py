"""1차(small) 대안 런타임 — Windows Service (Docker 없이 네이티브 프로세스로 실행).

IIS/Apache 뒤에 배치하는 Windows 환경 등 Docker Engine을 쓸 수 없는 구성을 위한
런타임이다. 컨테이너 이미지 대신 체크아웃된 리포 루트의 start.cmd(배포 시 build.py의
write_start_script가 자동 생성 — PORT 환경변수로 리슨 포트를 전달받아 그 포트에서
서비스를 띄운다)를 nssm(Non-Sucking Service Manager, public domain)으로 Windows Service에 등록해 실행한다.

Docker와 달리 네이티브 프로세스라 플랫폼이 바인드 주소를 강제할 방법이 없다 — PORT와
함께 HOST=localhost(UPSTREAM_HOST)도 넘겨주므로, 앱이 이를 지켜 바인드하면 프록시(단일
외부 포트)만 접근 가능해진다. 앱이 HOST를 무시하고 0.0.0.0에 바인드할 수도 있으므로, 운영 환경에서는
Windows 방화벽으로 외부에서 port_range(PAAS_PORT_RANGE_START~END) 인바운드를 반드시
차단해야 한다(3.6절 문서 참고).

DockerRuntime과 동일한 블루-그린 패턴: 서비스 이름은 {unit}-a / {unit}-b를 번갈아
쓰고, 새 슬롯이 헬스체크를 통과한 뒤에만 이전 슬롯을 제거한다.
"""
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request

from ...config import get_settings
from ...models import BuildProfile
from .base import Endpoint, Runtime, RuntimeSpec


class WindowsServiceError(RuntimeError):
    pass


# sc/nssm 호출에 반드시 타임아웃을 건다. 이 함수들은 요청 경로에서 불린다 —
# /server-config는 프로젝트·프로필마다 status()를 부르고 서비스 목록까지 조회한다.
# 타임아웃 없이 하나라도 멈추면 그 요청이 Starlette 스레드풀 슬롯을 영원히 잡고,
# 슬롯이 마르면 같은 풀을 쓰는 동기 엔드포인트가 전부 대기한다 — PowerShell 콘솔
# (POST /system/powershell/exec)이 "명령어 실행 중"에서 멈추던 경로가 이것이다.
# 프록시가 이 런타임의 앱에 붙을 때 쓰는 호스트 이름. 127.0.0.1이 아니라 localhost다 —
# 127.0.0.1은 "사용자 자기 PC"를 가리켜야 하는 자리(git 클라이언트 OAuth 콜백 등)에
# 남겨 두고, ARR의 응답 헤더 역방향 재작성이 그 콜백과 이름이 겹치는 것도 피한다.
#
# 앱이 듣는 주소(HOST)도 같은 이름으로 준다. 한쪽만 바꾸면 Windows에서 localhost가
# ::1로 먼저 풀려 어긋난다 — 앱은 127.0.0.1(IPv4)에만 듣는데 프록시는 ::1로 붙어
# 502가 난다. 같은 머신의 같은 리졸버를 양쪽이 쓰므로, 이름을 맞추면 어긋나지 않는다.
UPSTREAM_HOST = "localhost"

_QUERY_TIMEOUT = 10.0   # sc query — 단건 조회
_LIST_TIMEOUT = 15.0    # sc query state= all — 전체 목록이라 출력이 크다
_MANAGE_TIMEOUT = 30.0  # nssm install/set/start/stop/remove — 서비스 기동·정지를 기다린다


def _read_log_tail(log_path, n: int = 40) -> str:
    """서비스 stdout/stderr 로그의 마지막 n줄 — 헬스체크 실패 원인(트레이스백, 의존성
    설치 로그 등)을 에러에 실어 보여주기 위한 것(teardown 후에도 파일은 남는다)."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"(로그 없음: {log_path})"
    return "\n".join(lines[-n:]) if lines else "(로그 비어 있음 — 프로세스가 아무 출력도 하지 않음)"


def allocate_port() -> int:
    """비어 있는 포트를 찾는다 — 앱이 실제로 바인드할 이름(UPSTREAM_HOST)으로 확인한다.

    127.0.0.1로 확인하면 안 된다. 앱에는 HOST=localhost를 넘기고 Windows에서 그건 ::1로
    먼저 풀리는데, ::1에서 이미 쓰이는 포트가 127.0.0.1에서는 비어 보인다 — 그러면 이미
    다른 배포가 쓰는 포트를 다시 내주고 두 번째 기동이 조용히 실패한다.
    """
    settings = get_settings()
    for port in range(settings.port_range_start, settings.port_range_end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((UPSTREAM_HOST, port)) != 0:
                return port
    raise RuntimeError("no free port in configured range")


def _sc_binary() -> str:
    import os  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    if shutil.which("sc"):
        return "sc"
    default_sc = r"C:\Windows\System32\sc.exe"
    if os.path.exists(default_sc):
        return default_sc
    return "sc"


def _nssm_binary() -> str:
    import os  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    configured = get_settings().nssm_path
    if os.path.exists(configured):
        return configured
    found = shutil.which(configured)
    if found:
        return found
    candidates = [
        r"C:\tools\nssm-2.24\win64\nssm.exe",
        r"C:\tools\nssm-2.24\win32\nssm.exe",
        r"C:\tools\nssm\nssm.exe",
        r"C:\Program Files\nssm\win64\nssm.exe",
        r"C:\Program Files\nssm\nssm.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return configured


SERVICE_PREFIX = "paas-"


def list_registered_services() -> list[tuple[str, str]]:
    """플랫폼이 등록한 Windows Service의 (이름, 상태) 목록.

    콘솔에서 "지금 실제로 뭐가 등록돼 있나"를 보기 위한 것 — status()는 예상 이름을
    조회할 뿐이라, 배포가 중간에 끊겨 남은 슬롯이나 프로젝트를 지운 뒤 남은 서비스는
    화면에 드러나지 않는다. 그 둘이 배포를 막는 원인이라 눈에 보여야 한다.

    sc가 없거나(비Windows) 실패하면 빈 목록이다 — 조회가 안 되는 것은 오류가 아니라
    "볼 것이 없음"으로 다룬다(서버구성 화면 전체가 이것 때문에 실패하면 안 된다).
    """
    try:
        proc = subprocess.run(
            [_sc_binary(), "query", "state=", "all"],
            capture_output=True, text=True, timeout=_LIST_TIMEOUT,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []

    services: list[tuple[str, str]] = []
    name: str | None = None
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("SERVICE_NAME:"):
            candidate = stripped.split(":", 1)[1].strip()
            name = candidate if candidate.startswith(SERVICE_PREFIX) else None
        elif name and stripped.upper().startswith("STATE"):
            upper = stripped.upper()
            state = "running" if "RUNNING" in upper else "stopped" if "STOPPED" in upper else "unknown"
            services.append((name, state))
            name = None
    return services


class WindowsServiceRuntime(Runtime):
    def start(self, spec: RuntimeSpec) -> Endpoint:
        settings = get_settings()
        workdir = settings.work_dir / spec.project_name
        start_script = workdir / "start.cmd"
        if not start_script.exists():
            raise WindowsServiceError(
                "windows_service 런타임은 리포 루트에 start.cmd가 필요합니다 "
                f"(배포 시 자동 생성됨 — PORT/HOST 환경변수로 리슨 포트·바인드 주소 전달): {start_script}"
            )

        # 포트는 플랫폼 대장이 정한다(services/ports.py). 값이 없을 때만 직접 찾는다.
        host_port = spec.host_port or allocate_port()
        old_slot = self._current_slot(spec.unit_name)
        slot = "b" if old_slot == "a" else "a"
        name = f"{spec.unit_name}-{slot}"

        # 우리가 지금 덮어쓸 슬롯에 서비스가 남아 있으면 먼저 치운다. 이전 배포가
        # 중간에 끊기면(헬스체크 통과 후 구 슬롯 정리 직전에 플랫폼이 내려가거나,
        # _teardown의 nssm remove가 조용히 실패하면) 두 슬롯이 모두 남는다. 그 상태로
        # 두면 아래 install이 "이미 있음"으로 실패하고, 사람이 손으로 지우기 전까지
        # 이후 **모든** 배포가 같은 자리에서 막힌다 — 한 번의 사고가 영구 고장이 된다.
        if self._exists(name):
            self._teardown(name)

        log_path = settings.build_log_dir / f"{name}.log"
        env_pairs = "\n".join(
            f"{k}={v}" for k, v in {
                **spec.env,
                "PORT": str(host_port),
                "HOST": UPSTREAM_HOST,
                # start.cmd가 dev 서버로 띄울지 판단하고, dev 서버에 줄 base를 얻는다.
                "PAAS_PROFILE": spec.profile.value,
                "PAAS_BASE_PATH": spec.base_path,
            }.items()
        )

        # .cmd는 CreateProcess로 직접 실행되지 않으므로 cmd.exe /c로 감싼다 —
        # start.cmd를 nssm Application에 그대로 넣으면 서비스가 뜨지 않아(포트 미개방)
        # 헬스체크가 실패한다. AppDirectory가 workdir이므로 파일명만 넘겨도 된다.
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        self._nssm("install", name, comspec, "/c", str(start_script))
        self._nssm("set", name, "AppDirectory", str(workdir))
        self._nssm("set", name, "AppEnvironmentExtra", env_pairs)
        self._nssm("set", name, "AppStdout", str(log_path))
        self._nssm("set", name, "AppStderr", str(log_path))
        self._nssm("start", name)

        if not self._wait_healthy(host_port, spec.health_check_path):
            tail = _read_log_tail(log_path)
            self._teardown(name)
            probed = self._health_url(host_port, spec.health_check_path)
            raise WindowsServiceError(
                f"health check failed — {probed} 로 헬스체크 시간 내 응답이 없습니다. "
                f"앱이 HOST={UPSTREAM_HOST} PORT={host_port}를 지켜 리슨하는지 확인하세요"
                f"(HOST를 무시하고 다른 주소에 바인드하면 여기서 못 찾습니다). "
                f"아래 서비스 로그에 기동 오류가 그대로 남습니다.\n"
                f"--- {log_path.name} ---\n{tail}"
            )

        if old_slot is not None:
            self._teardown(f"{spec.unit_name}-{old_slot}")
        return Endpoint(host=UPSTREAM_HOST, port=host_port)

    def stop(self, project_name: str, profile: BuildProfile) -> None:
        spec = RuntimeSpec(project_name, "", 0, profile, "")
        for slot in ("a", "b"):
            name = f"{spec.unit_name}-{slot}"
            if self._exists(name):
                self._teardown(name)

    def status(self, project_name: str, profile: BuildProfile) -> str:
        spec = RuntimeSpec(project_name, "", 0, profile, "")
        slot = self._current_slot(spec.unit_name)
        if slot is None:
            return "stopped"
        return self._query_state(f"{spec.unit_name}-{slot}")

    def logs(self, project_name: str, profile: BuildProfile, tail: int = 200) -> str:
        spec = RuntimeSpec(project_name, "", 0, profile, "")
        slot = self._current_slot(spec.unit_name)
        if slot is None:
            return ""
        log_path = get_settings().build_log_dir / f"{spec.unit_name}-{slot}.log"
        if not log_path.exists():
            return ""
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    def _current_slot(self, unit_name: str) -> str | None:
        for slot in ("a", "b"):
            if self._exists(f"{unit_name}-{slot}"):
                return slot
        return None

    def _exists(self, name: str) -> bool:
        try:
            proc = subprocess.run(
                [_sc_binary(), "query", name],
                capture_output=True, text=True, timeout=_QUERY_TIMEOUT,
            )
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _query_state(self, name: str) -> str:
        try:
            proc = subprocess.run(
                [_sc_binary(), "query", name],
                capture_output=True, text=True, timeout=_QUERY_TIMEOUT,
            )
            if proc.returncode != 0:
                return "stopped"
            out = proc.stdout.upper()
            if "RUNNING" in out:
                return "running"
            if "STOPPED" in out:
                return "stopped"
            return "unknown"
        except subprocess.TimeoutExpired:
            return "unknown"
        except FileNotFoundError:
            return "stopped"

    def _teardown(self, name: str) -> None:
        nssm = _nssm_binary()
        try:
            subprocess.run(
                [nssm, "stop", name], capture_output=True, text=True, timeout=_MANAGE_TIMEOUT,
            )
            subprocess.run(
                [nssm, "remove", name, "confirm"],
                capture_output=True, text=True, timeout=_MANAGE_TIMEOUT,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # 정리는 베스트 에포트다 — 남으면 다음 배포가 설치 전에 다시 치운다(start 참고).
            pass

    def _nssm(self, *args: str) -> None:
        nssm = _nssm_binary()
        settings = get_settings()
        try:
            proc = subprocess.run(
                [nssm, *args], capture_output=True, text=True, timeout=_MANAGE_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            # 배포 경로다 — 조용히 넘어가면 반쯤 등록된 서비스가 남는다.
            raise WindowsServiceError(
                f"nssm {args[0]}이(가) {_MANAGE_TIMEOUT:.0f}초 내에 끝나지 않았습니다 "
                f"(서비스: {args[1] if len(args) > 1 else '?'})."
            ) from e
        except FileNotFoundError as e:
            raise WindowsServiceError(
                f"[WinError 2] nssm 실행 파일을 찾을 수 없습니다 (설정: PAAS_NSSM_PATH={settings.nssm_path}, 시도 경로: {nssm}): {e}"
            ) from e
        if proc.returncode != 0:
            raise WindowsServiceError(
                f"nssm {args[0]} 실패 (nssm 미설치 시 PAAS_NSSM_PATH={settings.nssm_path} 확인): "
                f"{(proc.stderr or proc.stdout).strip()[:500]}"
            )

    @staticmethod
    def _health_url(port: int, path: str) -> str:
        return f"http://{UPSTREAM_HOST}:{port}{path}"

    @staticmethod
    def _wait_healthy(port: int, path: str, timeout: float = 60.0) -> bool:
        """포트가 HTTP로 응답하기 시작했는지만 본다(기동 확인).

        호스트는 앱에 넘긴 HOST와 같은 이름을 쓴다 — 127.0.0.1로 박아 두면, localhost가
        ::1로 먼저 풀리는 Windows에서 앱이 ::1에 듣고 있을 때 영영 실패한다.

        4xx도 "떠 있음"이다. urlopen은 4xx에서 HTTPError를 던지는데 그걸 통째로 삼키면
        404가 죽은 것으로 계산된다 — dev 서버는 base가 붙은 경로만 받으므로 "/"에서
        404를 내고, 그 배포가 전부 실패했다. 5xx만 아직 준비되지 않은 것으로 본다.
        """
        url = WindowsServiceRuntime._health_url(port, path)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as res:
                    if res.status < 500:
                        return True
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return True
            except Exception:
                pass
            time.sleep(2)
        return False
