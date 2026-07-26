"""사내 Gitea에 대한 git 명령 인증 주입.

조직 소속/업로드로 생성된 프로젝트 리포는 Gitea에 private:true로 생성되므로
플랫폼이 clone/fetch/push할 때도 인증이 필요하다. git_url 자체에는 토큰을
심지 않고(DB·로그 노출 방지) git 프로세스 인자로만 주입한다.
"""
from urllib.parse import urlsplit

from ..config import get_settings


def auth_args(git_url: str) -> list[str]:
    """git_url 호스트가 PAAS_GITEA_URL과 일치할 때만 Authorization 헤더 인자를 반환한다.

    반환값은 git 서브커맨드 앞에 그대로 이어붙인다: ["git", *auth_args(url), "clone", ...]
    """
    settings = get_settings()
    if not settings.gitea_url or not settings.gitea_api_token:
        return []
    gitea_host = urlsplit(settings.gitea_url).netloc
    if not gitea_host or urlsplit(git_url).netloc != gitea_host:
        return []
    return ["-c", f"http.extraHeader=Authorization: token {settings.gitea_api_token}"]
