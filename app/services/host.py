"""서버 운영환경(OS) 감지·기능 매트릭스 — macOS / Linux / Windows.

PAAS_HOST_OS=auto(기본)면 platform.system()으로 감지하고, 컨테이너 안에서 실행하는 등
감지가 틀릴 때는 명시 값으로 오버라이드한다.
"""
import platform as _platform
from dataclasses import dataclass
from typing import Literal

from ..config import get_settings

HostOS = Literal["linux", "macos", "windows"]


@dataclass(frozen=True)
class HostCaps:
    os: HostOS
    gpu_supported: bool
    gpu_note: str
    docker_hint: str


_CAPS: dict[HostOS, HostCaps] = {
    "linux": HostCaps(
        os="linux",
        gpu_supported=True,
        gpu_note="NVIDIA Container Toolkit으로 GPU 컨테이너 지원",
        docker_hint="Docker Engine (Apache 2.0, 무료) — 운영 권장",
    ),
    "macos": HostCaps(
        os="macos",
        gpu_supported=False,
        gpu_note="macOS는 NVIDIA GPU 컨테이너 미지원 — LLM 프로젝트는 CPU(Ollama) 또는 원격 GPU 서버 사용",
        docker_hint="Colima(무료) 권장. Docker Desktop은 기업 규모에 따라 유료",
    ),
    "windows": HostCaps(
        os="windows",
        gpu_supported=True,
        gpu_note="WSL2 백엔드 경유 GPU 지원 — 가능하면 WSL2 안에서 Linux 모드로 운영 권장",
        docker_hint="Docker Desktop + WSL2 (기업 규모에 따라 유료)",
    ),
}


def detect_host_os() -> HostOS:
    system = _platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Windows":
        return "windows"
    return "linux"


def get_host_caps() -> HostCaps:
    configured = get_settings().host_os
    os_name: HostOS = detect_host_os() if configured == "auto" else configured  # type: ignore[assignment]
    return _CAPS[os_name]


def gpu_allowed() -> bool:
    """이 호스트에서 GPU 컨테이너를 배정해도 되는가. PAAS_FORCE_GPU=true로 강제 가능."""
    return get_host_caps().gpu_supported or get_settings().force_gpu
