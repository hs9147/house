"""1차(small) 런타임 — Docker Engine.

블루-그린 최소형: 새 컨테이너를 새 포트로 기동 → 헬스체크 통과 → (호출측이 Caddy 전환)
→ 구 컨테이너 제거. 컨테이너 이름은 {unit}-a / {unit}-b 를 번갈아 사용한다.
"""
import socket
import time
import urllib.request

from ...config import get_settings
from ..build import PROFILES
from .base import Endpoint, Runtime, RuntimeSpec
from ...models import BuildProfile


def _docker_client():
    try:
        import docker  # noqa: PLC0415 — 선택 의존성
    except ImportError as e:
        raise RuntimeError("docker SDK가 설치되지 않았습니다 (pip install docker)") from e
    try:
        return docker.from_env()
    except Exception as e:
        err_msg = str(e)
        if "CreateFile" in err_msg or "WinError 2" in err_msg or "cannot find the file" in err_msg.lower():
            raise RuntimeError(
                f"[WinError 2] Docker 데몬 연결 실패 (Docker Desktop/Engine 미실행, 소켓: \\\\.\\pipe\\docker_engine): {e}"
            ) from e
        raise


def allocate_port() -> int:
    settings = get_settings()
    for port in range(settings.port_range_start, settings.port_range_end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free port in configured range")


def _mem_bytes_str(limit: str, factor: float) -> str:
    """'1g' 같은 표기에 프로필 배율 적용 → docker가 이해하는 문자열."""
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    unit = limit[-1].lower()
    if unit in units:
        value = float(limit[:-1]) * units[unit]
    else:
        value = float(limit)
    return str(int(value * factor))


class DockerRuntime(Runtime):
    def start(self, spec: RuntimeSpec) -> Endpoint:
        if spec.gpu:
            from ..host import get_host_caps, gpu_allowed  # noqa: PLC0415

            if not gpu_allowed():
                caps = get_host_caps()
                raise RuntimeError(
                    f"이 호스트 OS({caps.os})는 GPU 컨테이너를 지원하지 않습니다. "
                    f"{caps.gpu_note} (강제하려면 PAAS_FORCE_GPU=true)"
                )
        client = _docker_client()
        factor = PROFILES[spec.profile].resource_factor
        host_port = allocate_port()
        old = self._find(client, spec.unit_name)
        slot = "b" if (old and old.name.endswith("-a")) else "a"

        nano_cpus = int(spec.cpu_limit * factor * 1e9)
        kwargs = dict(
            image=spec.image_tag,
            name=f"{spec.unit_name}-{slot}",
            detach=True,
            environment=spec.env,
            # 바인드 주소를 명시하지 않으면 Docker가 0.0.0.0(모든 인터페이스)에 publish해
            # 리버스 프록시를 거치지 않고 host_port로 외부에서 바로 붙을 수 있다 — 반드시
            # 127.0.0.1로 제한해 프록시(단일 외부 포트)만 접근하도록 강제한다.
            ports={f"{spec.internal_port}/tcp": ("127.0.0.1", host_port)},
            mem_limit=_mem_bytes_str(spec.memory_limit, factor),
            nano_cpus=nano_cpus,
            restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
            labels={"paas.project": spec.project_name, "paas.profile": spec.profile.value},
        )
        if spec.gpu:
            import docker  # noqa: PLC0415

            kwargs["device_requests"] = [
                docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
            ]
        container = client.containers.run(**kwargs)

        if not self._wait_healthy(host_port, spec.health_check_path):
            log_tail = container.logs(tail=50).decode(errors="replace")
            container.remove(force=True)
            raise RuntimeError(f"health check failed on :{host_port}\n{log_tail}")

        if old is not None:
            old.remove(force=True)
        return Endpoint(host="127.0.0.1", port=host_port)

    def stop(self, project_name: str, profile: BuildProfile) -> None:
        client = _docker_client()
        spec = RuntimeSpec(project_name, "", 0, profile, "")
        c = self._find(client, spec.unit_name)
        if c is not None:
            c.remove(force=True)

    def status(self, project_name: str, profile: BuildProfile) -> str:
        client = _docker_client()
        spec = RuntimeSpec(project_name, "", 0, profile, "")
        c = self._find(client, spec.unit_name)
        return c.status if c is not None else "stopped"

    def logs(self, project_name: str, profile: BuildProfile, tail: int = 200) -> str:
        client = _docker_client()
        spec = RuntimeSpec(project_name, "", 0, profile, "")
        c = self._find(client, spec.unit_name)
        if c is None:
            return ""
        return c.logs(tail=tail).decode(errors="replace")

    @staticmethod
    def _find(client, unit_name: str):
        for slot in ("a", "b"):
            found = client.containers.list(all=True, filters={"name": f"{unit_name}-{slot}"})
            if found:
                return found[0]
        return None

    @staticmethod
    def _wait_healthy(port: int, path: str, timeout: float = 60.0) -> bool:
        url = f"http://127.0.0.1:{port}{path}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as res:
                    if res.status < 500:
                        return True
            except Exception:
                pass
            time.sleep(2)
        return False
