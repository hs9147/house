"""시스템·GPU 스냅샷. psutil / nvidia-ml-py는 선택 의존성 — 없으면 해당 항목을 생략한다."""
from typing import Any


def snapshot() -> dict[str, Any]:
    from .host import get_host_caps  # noqa: PLC0415

    caps = get_host_caps()
    data: dict[str, Any] = {
        "host_os": caps.os,
        "gpu_supported": caps.gpu_supported,
        "docker_hint": caps.docker_hint,
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
