"""1차(small) 리버스프록시 대안 — Apache httpd (mod_proxy + mod_rewrite).

배포 URL은 서브패스 기반이다: 프로젝트들은 기본적으로 base_domain 하나를 공유하고
/{조직}/{프로젝트}/ 경로로 구분된다(services/proxy/__init__.py의 path_prefix_for).
그래서 프로젝트별로 독립된 <VirtualHost>를 만들 수 없다 — 여러 파일에 같은
ServerName이 반복되면 Apache는 처음 로드된 VirtualHost만 매칭하고 나머지는
조용히 무시한다(에러도 안 내고 그냥 안 먹힌다 — 원인 파악이 더 어려워 Caddy보다
위험).

대신 Caddy의 "import 디렉터리 글롭"과 동일한 패턴을 쓴다: base_domain용 정적
VirtualHost 파일(_base.conf) 하나가 IncludeOptional handles/*.conf로 프로젝트별
조각을 끌어오고, 각 조각은 ProxyPass/redirect 지시어만 담는다(mod_proxy의
ProxyPass는 지정한 경로 접두사를 스스로 벗겨내므로 <Location> 래핑이 따로
필요없다). PAAS_APACHE_SITES_DIR을 Include하는 메인 httpd.conf 설정은 기존과
동일하게 한 번만 연결해두면 된다.

release 배포에 커스텀 도메인(project.domain)을 지정한 예외만 기존처럼 독립된
VirtualHost 파일을 그대로 쓴다.
"""
import subprocess

from ...config import get_settings
from ...models import BuildProfile
from ..runtime.base import Endpoint
from .base import PathRoute, RedirectSpec, ReverseProxy, site_name


def _prefixed(path_prefix: str, sub_path: str) -> str:
    """redirect/rewrite의 from_path/to_path는 프로젝트 자신의 경로 기준이므로,
    공유 사이트(VirtualHost 루트에 이어붙는 조각)에서는 조직/프로젝트 접두사를
    명시적으로 붙여야 한다."""
    return path_prefix.rstrip("/") + "/" + sub_path.lstrip("/")


def _directive(r: RedirectSpec) -> str:
    if r.kind == "redirect":
        return f"    Redirect {r.status_code} {r.from_path} {r.to_path}\n"
    return f"    RewriteRule ^{r.from_path}$ {r.to_path} [L]\n"


def _conf_file(project_name: str, profile: BuildProfile):
    settings = get_settings()
    return settings.apache_sites_dir / f"{site_name(project_name, profile)}.conf"


def _handles_dir():
    settings = get_settings()
    d = settings.apache_sites_dir / "handles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _handle_file(project_name: str, profile: BuildProfile):
    return _handles_dir() / f"{site_name(project_name, profile)}.conf"


def _is_shared(domain: str) -> bool:
    return domain == get_settings().base_domain


def _ensure_base_vhost() -> None:
    settings = get_settings()
    base_file = settings.apache_sites_dir / "_base.conf"
    if base_file.exists():
        return
    handles_dir = _handles_dir()
    base_file.write_text(
        "<VirtualHost *:80>\n"
        f"    ServerName {settings.base_domain}\n"
        "    ProxyPreserveHost On\n"
        "    RewriteEngine On\n"
        f"    IncludeOptional {handles_dir}/*.conf\n"
        "</VirtualHost>\n",
        encoding="utf-8",
    )


def _path_directives(routes: list[PathRoute]) -> str:
    """ProxyPass는 등록 순서대로 매칭하므로 비루트(prefix) 규칙을 먼저 둔다 — mod_proxy가
    접두사를 자동으로 벗겨내므로 handle_path/IIS rewrite와 동일한 규약이 된다."""
    lines = []
    for r in routes:
        if r.path_prefix in ("/", ""):
            continue
        prefix = "/" + r.path_prefix.strip("/") + "/"
        lines.append(f"    ProxyPass {prefix} http://{r.endpoint.host}:{r.endpoint.port}/\n")
        lines.append(f"    ProxyPassReverse {prefix} http://{r.endpoint.host}:{r.endpoint.port}/\n")
    return "".join(lines)


def _vhost_conf_paths(domain: str, routes: list[PathRoute], redirects: list[RedirectSpec]) -> str:
    root = next((r for r in routes if r.path_prefix in ("/", "")), routes[-1])
    rewrite_engine = "    RewriteEngine On\n" if any(r.kind == "rewrite" for r in redirects) else ""
    directives = "".join(_directive(r) for r in redirects)
    root_prefix = "/" + root.path_prefix.strip("/") + "/" if root.path_prefix not in ("/", "") else "/"
    return (
        "<VirtualHost *:80>\n"
        f"    ServerName {domain}\n"
        f"{rewrite_engine}"
        f"{directives}"
        "    ProxyPreserveHost On\n"
        f"{_path_directives(routes)}"
        f"    ProxyPass {root_prefix} http://{root.endpoint.host}:{root.endpoint.port}/\n"
        f"    ProxyPassReverse {root_prefix} http://{root.endpoint.host}:{root.endpoint.port}/\n"
        "</VirtualHost>\n"
    )


def _shared_fragment(routes: list[PathRoute], redirects: list[RedirectSpec]) -> str:
    """base VirtualHost에 IncludeOptional로 이어붙는 조각 — <VirtualHost> 래핑 없이
    ProxyPass/redirect 지시어만 담는다."""
    root = next((r for r in routes if r.path_prefix in ("/", "")), routes[-1])
    directives = "".join(
        _directive(RedirectSpec(
            from_path=_prefixed(root.path_prefix, r.from_path),
            to_path=_prefixed(root.path_prefix, r.to_path),
            kind=r.kind, status_code=r.status_code,
        ))
        for r in redirects
    )
    root_prefix = "/" + root.path_prefix.strip("/") + "/"
    return (
        f"{directives}"
        f"{_path_directives(routes)}"
        f"    ProxyPass {root_prefix} http://{root.endpoint.host}:{root.endpoint.port}/\n"
        f"    ProxyPassReverse {root_prefix} http://{root.endpoint.host}:{root.endpoint.port}/\n"
    )


class ApacheProxy(ReverseProxy):
    def configure(self, project_name, profile: BuildProfile, domain, path_prefix,
                  endpoint: Endpoint, redirects: list[RedirectSpec]) -> None:
        self.configure_paths(
            project_name, profile, domain, [PathRoute(path_prefix=path_prefix, endpoint=endpoint)],
            redirects,
        )

    def configure_paths(self, project_name, profile: BuildProfile, domain,
                         routes: list[PathRoute], redirects: list[RedirectSpec]) -> None:
        if _is_shared(domain):
            _ensure_base_vhost()
            _handle_file(project_name, profile).write_text(
                _shared_fragment(routes, redirects), encoding="utf-8",
            )
        else:
            _conf_file(project_name, profile).write_text(
                _vhost_conf_paths(domain, routes, redirects), encoding="utf-8",
            )
        self._reload()

    def remove(self, project_name, profile: BuildProfile) -> None:
        _conf_file(project_name, profile).unlink(missing_ok=True)
        _handle_file(project_name, profile).unlink(missing_ok=True)
        self._reload()

    def _reload(self) -> bool:
        """apachectl 미설치 환경(테스트 등)에서는 조용히 넘어간다 — Caddy reload와 동일 정책."""
        try:
            proc = subprocess.run(
                get_settings().apache_reload_cmd.split(), capture_output=True, timeout=15,
            )
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
