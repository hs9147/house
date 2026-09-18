"""시스템·GPU 스냅샷. psutil / nvidia-ml-py는 선택 의존성 — 없으면 해당 항목을 생략한다."""
from typing import Any


def snapshot() -> dict[str, Any]:
    """시스템 상태 — **지금 이 설치본의 실제 구성**을 말한다.

    예전에는 host.py의 OS 능력 매트릭스를 그대로 내보냈다. 그래서 Windows면 런타임·프록시
    설정과 무관하게 늘 "Docker Desktop + WSL2 (기업 규모에 따라 유료) · GPU 지원"이 떴다.
    windows_service 런타임은 정의상 Docker를 쓰지 않는데(nssm으로 네이티브 프로세스를
    등록한다) Docker 라이선스 안내가 뜨고, GPU가 없는 서버에서 "GPU 지원"이라고 하면서
    바로 아래에 "GPU 없음"을 함께 보여 주는 자기모순이 있었다.

    OS 능력은 여전히 상한이다(macOS는 GPU 컨테이너를 못 쓴다). 다만 상한과 실제 구성은
    다른 것이므로, 화면에는 실제 구성을 보낸다.
    """
    from ..config import get_settings  # noqa: PLC0415
    from .deployer import GPU_CAPABLE_RUNTIMES, runtime_name  # noqa: PLC0415
    from .host import get_host_caps, gpu_allowed  # noqa: PLC0415

    settings = get_settings()
    caps = get_host_caps()
    runtime = runtime_name()
    data: dict[str, Any] = {
        "host_os": caps.os,
        # 무엇으로 돌고 무엇으로 프록시하는지 — 이게 사람이 확인하려던 값이다.
        "runtime_backend": runtime,
        "proxy_backend": settings.proxy_backend,
        # GPU는 OS만으로 정해지지 않는다: 런타임이 배정할 수 있어야 하고(GPU_CAPABLE_RUNTIMES),
        # 그 위에 OS 능력·PAAS_FORCE_GPU가 걸린다. 실제 장치 유무는 아래 gpus가 말한다.
        "gpu_supported": runtime in GPU_CAPABLE_RUNTIMES and gpu_allowed(),
        # Docker 라이선스 안내는 Docker 런타임일 때만 의미가 있다.
        "docker_hint": caps.docker_hint if runtime == "docker" else "",
    }
    try:
        import psutil  # noqa: PLC0415

        vm = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        data["cpu_percent"] = psutil.cpu_percent(interval=0.2)
        data["memory"] = {"total": vm.total, "used": vm.used, "percent": vm.percent}
        data["disk"] = {"total": disk.total, "used": disk.used, "percent": disk.percent}
    except ImportError:
        data["system"] = "psutil not installed"

    data["gpus"] = _gpu_snapshot()
    return data


def _gpu_snapshot() -> list[dict[str, Any]]:
    try:
        import pynvml  # noqa: PLC0415
    except ImportError:
        return []
    try:
        pynvml.nvmlInit()
        gpus = []
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            name = pynvml.nvmlDeviceGetName(h)
            gpus.append({
                "index": i,
                "name": name.decode() if isinstance(name, bytes) else name,
                "vram_total": mem.total,
                "vram_used": mem.used,
                "util_percent": util.gpu,
            })
        pynvml.nvmlShutdown()
        return gpus
    except Exception:
        return []


def free_vram_bytes() -> int | None:
    """LLM 배포 전 VRAM 사전 검사용. GPU가 없으면 None."""
    gpus = _gpu_snapshot()
    if not gpus:
        return None
    return max(g["vram_total"] - g["vram_used"] for g in gpus)
