"""기능 모듈(설치 빌드옵션) — PAAS_FEATURES로 설치본마다 켤 모듈을 선택한다.

core(프로젝트·env·API 키·감사·모듈 레지스트리·/status)는 항상 켜져 있고,
아래 모듈은 선택이다. 비활성 모듈의 엔드포인트는 404로 감춰진다.
"""
from fastapi import HTTPException

from .config import get_settings

FEATURES: dict[str, str] = {
    "deploy": "배포 — deploy/rollback/stop/logs/deployments·웹훅·프리뷰",
    "workspace": "LLM 코드 워크스페이스 — 프로바이더·채팅·diff 승인·리뷰",
}

# Functions 이관 전 설정을 그대로 둔 기존 설치본도 중단 없이 업그레이드할 수 있게
# 이름만 허용하고 활성 기능에서는 제외한다.
DEPRECATED_FEATURES = {"mail", "payment"}


def enabled_features() -> set[str]:
    raw = get_settings().features
    names = {f.strip() for f in raw.split(",") if f.strip()}
    unknown = names - FEATURES.keys() - DEPRECATED_FEATURES
    if unknown:
        raise ValueError(f"unknown PAAS_FEATURES: {', '.join(sorted(unknown))}")
    return names & FEATURES.keys()


def is_enabled(name: str) -> bool:
    return name in enabled_features()


def require_feature(name: str):
    """비활성 모듈의 라우트를 404로 감추는 FastAPI 의존성."""

    def _check() -> None:
        if not is_enabled(name):
            raise HTTPException(status_code=404, detail="Not Found")

    return _check
