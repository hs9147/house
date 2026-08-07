"""Gitea REST API 클라이언트.

조직(Organization) 생성 시 대응하는 Gitea Organization을, 조직 소속 프로젝트 생성 시
대응하는 리포를 플랫폼이 대신 만든다. 사용자는 Gitea URL/리포 주소를 직접 다루지
않는다 — git_url은 서버 내부에서만 사용되고 비관리자 API 응답에서는 마스킹된다
(api/projects.py `_serialize_project` 참고).
"""
from urllib.parse import urlsplit

import httpx

from ..config import get_settings


class GiteaError(RuntimeError):
    """Gitea API 호출 자체는 성공했지만(설정은 있음) 요청이 실패한 경우 — 502로 매핑."""


class GiteaNotConfigured(GiteaError):
    """PAAS_GITEA_URL/PAAS_GITEA_API_TOKEN 미설정 — 503으로 매핑."""


class GiteaNothingToMerge(GiteaError):
    """머지할 커밋이 없어 PR을 만들 수 없음 — 실패가 아니라 '이미 반영됨'이다."""


def _base_and_headers() -> tuple[str, dict[str, str]]:
    settings = get_settings()
    if not settings.gitea_url:
        raise GiteaNotConfigured("PAAS_GITEA_URL이 설정되지 않았습니다.")
    if not settings.gitea_api_token:
        raise GiteaNotConfigured("PAAS_GITEA_API_TOKEN이 설정되지 않았습니다.")
    return settings.gitea_url.rstrip("/"), {"Authorization": f"token {settings.gitea_api_token}"}


def ensure_org(name: str) -> None:
    """조직(Gitea Organization)이 없으면 생성한다. 이미 있으면 조용히 통과(멱등)."""
    base, headers = _base_and_headers()
    res = httpx.post(
        f"{base}/api/v1/orgs", headers=headers,
        json={"username": name, "visibility": "private"}, timeout=15,
    )
    if res.status_code in (201, 422):  # 422 = username already exists
        return
    raise GiteaError(f"Gitea 조직 생성 실패 (HTTP {res.status_code}): {res.text[:300]}")


def ensure_repo(org_name: str, repo_name: str, auto_init: bool = True) -> str:
    """조직 아래 리포가 없으면 생성하고, 이미 있으면 조회해서 clone URL을 반환한다.

    auto_init=False는 업로드 등록 경로용 — 플랫폼이 스테이징한 내용을 최초
    커밋으로 직접 push하므로 Gitea 쪽에서 빈 초기 커밋을 만들면 안 된다.
    """
    base, headers = _base_and_headers()
    res = httpx.post(
        f"{base}/api/v1/orgs/{org_name}/repos", headers=headers,
        json={"name": repo_name, "private": False, "auto_init": auto_init}, timeout=15,
    )
    if res.status_code == 201:
        return res.json()["clone_url"]
    if res.status_code == 409:  # 이미 존재 — 조회해서 재사용
        got = httpx.get(f"{base}/api/v1/repos/{org_name}/{repo_name}", headers=headers, timeout=15)
        if got.status_code == 200:
            return got.json()["clone_url"]
        raise GiteaError(f"Gitea 리포 조회 실패 (HTTP {got.status_code}): {got.text[:300]}")
    raise GiteaError(f"Gitea 리포 생성 실패 (HTTP {res.status_code}): {res.text[:300]}")


def list_orgs() -> list[dict]:
    """토큰이 접근 가능한 Gitea 조직 전체 목록(gitea_sync.sync_from_gitea 전용)."""
    base, headers = _base_and_headers()
    return _paginated(f"{base}/api/v1/orgs", headers)


def list_org_repos(org_name: str) -> list[dict]:
    """조직 아래 리포 전체 목록."""
    base, headers = _base_and_headers()
    return _paginated(f"{base}/api/v1/orgs/{org_name}/repos", headers)


def _paginated(url: str, headers: dict[str, str]) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        res = httpx.get(url, headers=headers, params={"page": page, "limit": 50}, timeout=15)
        if res.status_code != 200:
            raise GiteaError(f"Gitea 목록 조회 실패 (HTTP {res.status_code}): {res.text[:300]}")
        batch = res.json()
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 50:
            break
        page += 1
    return items


def repo_slug(git_url: str) -> tuple[str, str] | None:
    """git_url에서 (owner, repo)를 뽑는다. 사내 Gitea 리포가 아니면 None.

    Gitea REST API(PR·머지)를 쓸 수 있는지 판별하는 관문 — git_auth.auth_args와 동일하게
    호스트가 PAAS_GITEA_URL과 일치할 때만 유효하다.
    """
    settings = get_settings()
    if not settings.gitea_url:
        return None
    if urlsplit(git_url).netloc != urlsplit(settings.gitea_url).netloc:
        return None
    path = urlsplit(git_url).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def ensure_pull_request(
    owner: str, repo: str, head: str, base: str, title: str, body: str = ""
) -> dict:
    """head→base PR을 만들고, 이미 열려 있으면 그 PR을 반환한다(멱등)."""
    api, headers = _base_and_headers()
    res = httpx.post(
        f"{api}/api/v1/repos/{owner}/{repo}/pulls", headers=headers,
        json={"head": head, "base": base, "title": title, "body": body}, timeout=15,
    )
    if res.status_code == 201:
        return res.json()
    if res.status_code == 409:
        # 409는 두 가지다: 같은 head→base PR이 이미 열려 있거나, 두 브랜치 사이에
        # 커밋이 없거나. 열린 PR이 없으면 남는 해석은 후자다 — 이미 반영된 상태다.
        existing = _find_open_pull(api, headers, owner, repo, head, base)
        if existing is not None:
            return existing
        raise GiteaNothingToMerge(
            f"'{head}'에 기본 브랜치로 반영할 커밋이 없습니다: {res.text[:200]}")
    raise GiteaError(f"Gitea PR 생성 실패 (HTTP {res.status_code}): {res.text[:300]}")


def _find_open_pull(
    api: str, headers: dict[str, str], owner: str, repo: str, head: str, base: str
) -> dict | None:
    res = httpx.get(
        f"{api}/api/v1/repos/{owner}/{repo}/pulls", headers=headers,
        params={"state": "open", "limit": 50}, timeout=15,
    )
    if res.status_code != 200:
        return None
    for pr in res.json():
        head_ref = (pr.get("head") or {}).get("ref")
        base_ref = (pr.get("base") or {}).get("ref")
        if head_ref == head and base_ref == base:
            return pr
    return None


def merge_pull_request(owner: str, repo: str, index: int, title: str = "") -> bool:
    """PR을 머지한다. 충돌 등으로 머지 불가면 False(호출부가 PR을 열어둔 채 보고)."""
    api, headers = _base_and_headers()
    res = httpx.post(
        f"{api}/api/v1/repos/{owner}/{repo}/pulls/{index}/merge", headers=headers,
        json={"Do": "merge", "MergeTitleField": title}, timeout=30,
    )
    if res.status_code in (200, 204):
        return True
    if res.status_code in (405, 409):  # 머지 불가(충돌·미승인 등)
        return False
    raise GiteaError(f"Gitea PR 머지 실패 (HTTP {res.status_code}): {res.text[:300]}")


def ensure_webhook(org_name: str, repo_name: str) -> None:
    """리포에 플랫폼 push 웹훅이 없으면 등록한다(멱등) — 수동 설정 없이 자동 배포가 되도록.

    PAAS_PLATFORM_PUBLIC_URL이 비어 있으면 플랫폼 자신의 주소를 알 수 없으므로
    조용히 건너뛴다(infra/gitea/README.md의 수동 절차로 대체 가능).
    """
    settings = get_settings()
    if not settings.platform_public_url:
        return
    base, headers = _base_and_headers()
    hook_url = f"{settings.platform_public_url.rstrip('/')}/paas/webhooks/git"

    existing = httpx.get(f"{base}/api/v1/repos/{org_name}/{repo_name}/hooks", headers=headers, timeout=15)
    if existing.status_code == 200 and any(
        h.get("config", {}).get("url") == hook_url for h in existing.json()
    ):
        return  # 이미 등록됨

    res = httpx.post(
        f"{base}/api/v1/repos/{org_name}/{repo_name}/hooks", headers=headers,
        json={
            "type": "gitea",
            "config": {"url": hook_url, "content_type": "json", "secret": settings.webhook_secret},
            "events": ["push"],
            "active": True,
        },
        timeout=15,
    )
    if res.status_code not in (200, 201):
        raise GiteaError(f"Gitea 웹훅 등록 실패 (HTTP {res.status_code}): {res.text[:300]}")
