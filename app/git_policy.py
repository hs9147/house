"""기업용 거버넌스 — 프로젝트 git_url을 사내 Git 서버로 한정한다 (선택, PAAS_GIT_INTERNAL_ONLY).

15절 보안 점검에서 "internal LLM 프로바이더가 라벨일 뿐 강제되지 않던" 문제를 코드로
막았던 것과 동일한 원칙: git_url도 방치하면 프로젝트를 github.com 등 외부 호스트로
등록해 소스가 사외로 나가는 경로가 남는다. 이 검증은 그 구멍을 닫는다.
"""
from urllib.parse import urlparse

from fastapi import HTTPException

from .config import get_settings


def _extract_host(git_url: str) -> str:
    if "://" in git_url:
        return urlparse(git_url).netloc
    # SCP-like SSH: git@git.example.com:org/repo.git
    if "@" in git_url and ":" in git_url:
        return git_url.split("@", 1)[1].split(":", 1)[0]
    return git_url


def enforce_internal_git_url(git_url: str) -> None:
    settings = get_settings()
    if not settings.git_internal_only:
        return
    if not settings.gitea_url:
        raise HTTPException(
            status_code=503,
            detail="PAAS_GIT_INTERNAL_ONLY가 켜져 있지만 PAAS_GITEA_URL이 설정되지 않았습니다.",
        )
    allowed_host = urlparse(settings.gitea_url).netloc
    actual_host = _extract_host(git_url)
    if actual_host != allowed_host:
        raise HTTPException(
            status_code=422,
            detail=(
                f"기업용 설정(PAAS_GIT_INTERNAL_ONLY)에서는 프로젝트 git_url이 사내 Git 서버"
                f"({allowed_host})에 있어야 합니다: {git_url}"
            ),
        )
