"""1차(small) 기본 리버스프록시 — Caddy.

배포 URL은 서브패스 기반이다: 프로젝트들은 기본적으로 base_domain 하나를 공유하고
/{조직}/{프로젝트}/ 경로로 구분된다(services/proxy/__init__.py의 path_prefix_for).
그래서 프로젝트마다 독립된 최상위 사이트 파일을 만들 수 없다 — 여러 파일에 똑같은
{base_domain} 사이트 주소가 반복되면 Caddy가 "ambiguous site definition"으로
기동을 거부한다.

대신 2단 구조를 쓴다: base_domain용 정적 사이트 파일(_base.caddy) 하나가
handles/*.caddy를 import하고, 프로젝트별 배포는 handles/ 아래에 자기 경로만 담은
작은 조각 파일 하나를 쓰고 지운다(기존과 동일하게 파일 하나 write/delete로 끝난다).
메인 Caddyfile은 지금처럼 "import <caddy_sites_dir>/*.caddy" 한 줄이면 되고,
_base.caddy도 그 디렉터리에 있으니 자동으로 걸린다 — 운영자가 추가로 바꿀 것은 없다.

release 배포에 커스텀 도메인(project.domain)을 지정한 예외만 기존처럼 독립된
최상위 사이트 파일을 그대로 쓴다(domain_for가 base_domain이 아닌 그 도메인을
반환하므로 이 파일에서 "공유 사이트인지"를 domain == base_domain으로 판별한다).
"""
import subprocess

import httpx

from ...config import get_settings
from ...models import BuildProfile
from ..runtime.base import Endpoint
from .base import PathRoute, RedirectSpec, ReverseProxy, site_name


def _redirect_lines(redirects: list[RedirectSpec], indent: str = "    ") -> str:
    lines = []
    for r in redirects:
        if r.kind == "redirect":
            lines.append(f"{indent}redir {r.from_path} {r.to_path} {r.status_code}")
        else:
            lines.append(f"{indent}rewrite {r.from_path} {r.to_path}")
    return "".join(f"{line}\n" for line in lines)


def _site_file(project_name: str, profile: BuildProfile):
    settings = get_settings()
    return settings.caddy_sites_dir / f"{site_name(project_name, profile)}.caddy"


def _handles_dir():
    settings = get_settings()
    d = settings.caddy_sites_dir / "handles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snippet_file(project_name: str, profile: BuildProfile):
    return _handles_dir() / f"{site_name(project_name, profile)}.caddy"


def _is_shared(domain: str) -> bool:
    return domain == get_settings().base_domain


def _ensure_base_site() -> None:
    settings = get_settings()
    base_file = settings.caddy_sites_dir / "_base.caddy"
    if base_file.exists():
        return
    handles_dir = _handles_dir()
    base_file.write_text(
        f"{settings.base_domain} {{\n    import {handles_dir}/*.caddy\n    log\n}}\n",
        encoding="utf-8",
    )


def _path_block(route: PathRoute, redirects: list[RedirectSpec]) -> str:
    """route.path_prefix가 "/"나 ""면(커스텀 도메인 예외 — 이 파일 자체가 이미
    도메인을 통째로 가짐) 래핑 없이 reverse_proxy만 쓴다. 그 외(공유 사이트의
    /조직/프로젝트/ 경로, composite의 /api/ 등)는 handle_path로 감싼다 — redirect도
    그 안에 넣어 handle_path가 이미 벗겨낸 경로 기준으로 동작하게 한다(사용자가
    적는 from_path/to_path는 프로젝트 자신의 경로 기준 그대로 유지된다)."""
    if route.path_prefix in ("/", ""):
        return f"{_redirect_lines(redirects)}    reverse_proxy {route.endpoint.host}:{route.endpoint.port}\n"
    prefix = route.path_prefix.rstrip("/")
    return (
        f"    handle_path {prefix}/* {{\n"
        f"{_redirect_lines(redirects, indent='        ')}"
        f"        reverse_proxy {route.endpoint.host}:{route.endpoint.port}\n"
        f"    }}\n"
    )


def _routes_body(routes: list[PathRoute], redirects: list[RedirectSpec]) -> str:
    # handle_path는 첫 매칭 블록만 실행하므로 "/"(캐치올)는 반드시 마지막에 둔다 —
    # 공유 사이트에서는 어느 라우트도 리터럴 "/"가 아니므로(항상 /조직/프로젝트/...)
    # 안정 정렬이 호출자가 넘긴 순서(구체적 라우트 먼저)를 그대로 보존한다.
    ordered = sorted(routes, key=lambda r: r.path_prefix in ("/", ""))
    blocks = []
    for i, r in enumerate(ordered):
        blocks.append(_path_block(r, redirects if i == len(ordered) - 1 else []))
    return "".join(blocks)


class CaddyProxy(ReverseProxy):
    def configure(self, project_name, profile, domain, path_prefix, endpoint: Endpoint,
                  redirects: list[RedirectSpec]) -> None:
        body = _routes_body([PathRoute(path_prefix=path_prefix, endpoint=endpoint)], redirects)
        if _is_shared(domain):
            _ensure_base_site()
            _snippet_file(project_name, profile).write_text(body, encoding="utf-8")
        else:
            _site_file(project_name, profile).write_text(
                f"{domain} {{\n{body}    log\n}}\n", encoding="utf-8",
            )
        reload_caddy()

    def configure_paths(self, project_name, profile, domain, routes: list[PathRoute],
                         redirects: list[RedirectSpec]) -> None:
        body = _routes_body(routes, redirects)
        if _is_shared(domain):
            _ensure_base_site()
            _snippet_file(project_name, profile).write_text(body, encoding="utf-8")
        else:
            _site_file(project_name, profile).write_text(
                f"{domain} {{\n{body}    log\n}}\n", encoding="utf-8",
            )
        reload_caddy()

    def remove(self, project_name, profile) -> None:
        # 공유/전용 어느 쪽으로 만들어졌는지 remove()는 알 수 없으므로 둘 다 시도한다.
        _site_file(project_name, profile).unlink(missing_ok=True)
        _snippet_file(project_name, profile).unlink(missing_ok=True)
        reload_caddy()


def reload_caddy() -> bool:
    """caddy CLI 우선, 실패 시 admin API. Caddy 미기동 환경(테스트 등)에서는 조용히 넘어간다."""
    try:
        proc = subprocess.run(["caddy", "reload"], capture_output=True, timeout=15)
        if proc.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        settings = get_settings()
        httpx.post(f"{settings.caddy_admin_url}/load", timeout=5)
        return True
    except Exception:
        return False
