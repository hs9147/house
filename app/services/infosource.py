"""정보 출처 스캔 — 웹사이트·API·MCP 서버를 읽어 "무엇을 조회할 수 있는가"를 정리한다.

스캔 방법은 공개된 연구와 오픈소스에서 가져왔다.

  - **구조 먼저, 그림은 거든다.** 웹 에이전트 연구의 결론이 같다: 접근성 트리·DOM 같은
    구조가 기본이고(WebArena 2023, Playwright MCP의 aria snapshot), 화면 캡처를 비전 모델에
    함께 주면 메뉴·표의 배치처럼 구조에 안 드러나는 것이 보완된다(SeeAct ICML 2024,
    WebVoyager 2024, Set-of-Mark 2023). 그래서 HTTP로 받은 HTML의 구조가 기본이고,
    Playwright가 설치돼 있으면 렌더링한 DOM·aria snapshot·캡처를 **더한다**(없어도 돈다).
  - **같은 출처 안에서, 깊이와 쪽수에 상한을 두고 돈다.** Crawl4AI·Firecrawl의 크롤 방식이다
    — sitemap.xml을 먼저 보고, robots.txt를 지키고, 결과를 마크다운으로 남긴다.
  - **API는 OpenAPI 문서를 찾아 읽는다**(잘 알려진 자리 몇 곳). 조회 가능한 정보 = GET 경로다.
  - **MCP는 tools/list가 곧 목록이다.** 읽기 도구와 쓰기 도구를 가른다(readOnlyHint, 이름).

LLM은 수집한 사실을 **정리**만 한다 — 메뉴 트리·페이지 성격·저장소 추천. 모델이 지어낸 주소는
버린다(실제로 받은 페이지·링크에 없는 URL). 모델이 없어도 결정론 결과가 남는다.

**내려받을 수 있는 파일**(같은 출처의 PDF·Office·한글·CSV)도 받아 저장소로 옮긴다. 저장 위치는
**유형마다** 따로 정한다(조회 정보 정리·문서·표·발표). 저장은 같은 주소를 늘 같은 자리에 쓰고,
내용이 같으면 건너뛰고 바뀌었으면 덮어쓴다 — 지난번에 썼는데 이번에 없는 파일은 휴지통으로
옮긴다. 파일 하나하나의 이력은 두지 않는다. 대신 **스캔 이력**(주소마다 결과·내용 해시)을 남겨
다음 스캔의 범위를 정한다: 지난번에 받은 화면은 상한 안에서 먼저 다시 보고, 없던 주소(404)는
한 번 쉬고, 바뀐 것만 새로 쓴다.

**요청 헤더(쿠키·토큰)는 요청을 보내는 그 순간에만 복호화한다.** 같은 출처로 가는 요청에만
붙이고(다른 호스트로 리다이렉트되면 떼어 낸다 — httpx는 Authorization만 떼고 Cookie는 그대로
넘긴다), 결과·감사·LLM 어디에도 값이 실리지 않는다.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.util
import json
import os
import re
import shutil
import ssl
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import InfoSource, InfoSourceScan
from ..security import decrypt_value, encrypt_value
from . import docsearch, jobs, llm, mcp_client, storage

KINDS = ("web", "api", "mcp")
USER_AGENT = "gpaas-infosource/1.0"

MAX_PAGES = 30          # 한 번 스캔에서 받는 페이지 수
MAX_DEPTH = 2           # 시작 페이지에서 링크를 따라 들어가는 깊이
MAX_BYTES = 2_000_000   # 페이지 하나의 상한 — 넘으면 앞부분만 읽는다
TIMEOUT = 15.0
MAX_SHOTS = 4           # 브라우저로 캡처할 페이지 수(비전 모델에 함께 보낸다)
TEXT_CAP = 4000         # 페이지 본문 발췌 상한(저장되는 문서에 실린다)
STALE_SCAN_SECONDS = 15 * 60
MAX_FILES = 30                # 한 번 스캔에서 받는 내려받기 파일 수
MAX_FILE_BYTES = 20_000_000   # 파일 하나의 상한 — 넘으면 받지 않는다(잘린 파일은 열리지 않는다)
MAX_HISTORY = 30              # 출처마다 남기는 스캔 이력

# 내려받아 저장소로 옮기는 파일 — 색인이 본문을 읽는 형식만(그림·압축·실행 파일은 받지 않는다).
FILE_TYPES = {".pdf": "documents", ".doc": "documents", ".docx": "documents", ".hwp": "documents",
              ".hwpx": "documents", ".txt": "documents", ".xls": "sheets", ".xlsx": "sheets",
              ".csv": "sheets", ".ppt": "slides", ".pptx": "slides"}
# 주소에 확장자가 없는 내려받기(download.do?id=…)는 content-type으로 안다.
_FILE_CTYPES = {
    "application/pdf": ".pdf", "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/x-hwp": ".hwp", "application/haansofthwp": ".hwp", "application/vnd.hancom.hwp": ".hwp",
    "application/vnd.hancom.hwpx": ".hwpx", "text/csv": ".csv",
}
# 저장 유형 — 유형마다 저장 위치를 따로 정한다. 값은 (화면 이름, 저장소 안 폴더).
CATEGORIES = {"pages": ("조회 정보 정리", ""), "documents": ("문서 파일", "문서"),
              "sheets": ("표 파일", "표"), "slides": ("발표 파일", "발표")}

# 따라가면 세션이 끊기거나 무언가를 바꾸는 링크 — 쿠키를 들고 도는 크롤러가 밟으면 안 된다.
_DANGEROUS_LINK = re.compile(r"log-?out|sign-?out|logoff|로그아웃|delete|remove|삭제", re.I)
_DOWNLOAD_SUFFIXES = (".pdf", ".zip", ".hwp", ".hwpx", ".doc", ".docx", ".xls", ".xlsx",
                      ".ppt", ".pptx", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".mp4", ".exe")
_NAV_HINT = re.compile(r"(^|[-_ ])(nav|navbar|menu|gnb|lnb|snb|sidebar|sitemap)([-_ ]|$)", re.I)
_NAV_ROLES = {"navigation", "menu", "menubar", "tree", "tablist"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "source", "track", "wbr"}
_OPENAPI_PATHS = ("/openapi.json", "/swagger.json", "/v3/api-docs", "/v2/api-docs",
                  "/swagger/v1/swagger.json", "/api-docs", "/api/openapi.json", "/docs/openapi.json")
# 쿼리에 이런 이름이 있으면 자격증명이 주소에 박혀 있다 — 주소는 화면·감사·LLM에 실린다.
_SECRET_PARAMS = ("key", "token", "secret", "password", "passwd", "pwd", "apikey", "access_token")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}
# 상위 도메인(.lge.com) 전체에 걸리는 SSO 쿠키 — EP 3.0은 CA SiteMinder다(SM*).
_SSO_COOKIE = re.compile(r"(^|;\s*)(SMSESSION|SMIDENTITY|SMSAVEDSESSION|SMCHALLENGE)\s*=", re.I)


class SourceError(ValueError):
    pass


class ScanBusy(SourceError):
    pass


# ---------------------------------------------------------------- 등록 값 검증

def check_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SourceError("주소는 http:// 또는 https://로 시작해야 합니다.")
    names = [p.split("=", 1)[0].lower() for p in parts.query.split("&") if p]
    leaked = [n for n in names if any(s in n for s in _SECRET_PARAMS)]
    if leaked:
        raise SourceError(
            f"주소의 쿼리에 자격증명으로 보이는 값이 있습니다({', '.join(leaked)}) — 주소는 화면과 "
            "작업 로그에 남습니다. 값은 요청 헤더 칸에 넣으세요.")
    return url


def parse_headers(text: str) -> dict[str, str]:
    """`이름: 값`을 한 줄에 하나씩. 값은 그대로 두고 이름만 검사한다."""
    found: dict[str, str] = {}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        name, sep, value = line.partition(":")
        name = name.strip()
        if not sep or not _HEADER_NAME.match(name):
            raise SourceError(f"헤더는 `이름: 값` 형식이어야 합니다: {name[:40]!r}")
        if name.lower() in _FORBIDDEN_HEADERS:
            raise SourceError(f"이 헤더는 지정할 수 없습니다: {name}")
        if name.lower() == "cookie" and (sso := _SSO_COOKIE.search(value)):
            raise SourceError(
                f"SSO 쿠키({sso.group(2)})는 넣을 수 없습니다 — 그 사용자로 사내 시스템 전부에 "
                "들어갈 수 있는 값입니다. 이 사이트의 세션 쿠키만 넣거나 브라우저 스캔을 쓰세요.")
        found[name] = value.strip()
    return found


def encrypt_headers(headers: dict[str, str]) -> str | None:
    return encrypt_value(json.dumps(headers, ensure_ascii=False)) if headers else None


def header_names(row: InfoSource) -> list[str]:
    return sorted(_headers(row))


def _headers(row: InfoSource) -> dict[str, str]:
    if not row.headers_encrypted:
        return {}
    try:
        data = json.loads(decrypt_value(row.headers_encrypted))
    except Exception:  # noqa: BLE001 — 키가 바뀌었으면 헤더 없이 돈다(값을 추측하지 않는다)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


# ---------------------------------------------------------------- HTTP 경계

def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _http_get(url: str, headers: dict[str, str], origin: str,
              limit: int = MAX_BYTES) -> tuple[int, str, str, bytes]:
    """테스트에서 monkeypatch하는 실제 HTTP 경계 — (상태, 최종 URL, content-type, 본문).

    리다이렉트를 직접 따른다: 헤더는 **등록한 출처로 가는 요청에만** 붙인다. 본문은 limit까지만
    읽는다(화면은 앞부분이면 충분하고, 파일은 부르는 쪽이 길이로 잘렸는지 본다).
    """
    current = url
    # 인증서는 OS 저장소로 검증한다 — httpx 기본(certifi)은 사내 루트 CA를 모른다. Windows에서
    # 표준 ssl의 기본 컨텍스트가 시스템 저장소(ROOT·CA)를 읽는다(pu-gps4.lge.com에서 확인).
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False,
                      verify=ssl.create_default_context()) as client:
        for _ in range(6):
            sent = {"user-agent": USER_AGENT}
            if _origin(current) == origin:
                sent.update(headers)
            with client.stream("GET", current, headers=sent) as res:
                if res.status_code in (301, 302, 303, 307, 308) and res.headers.get("location"):
                    current = urljoin(current, res.headers["location"])
                    continue
                body = b""
                for chunk in res.iter_bytes():
                    body += chunk
                    if len(body) >= limit:
                        break
                return res.status_code, current, res.headers.get("content-type", ""), body
    raise SourceError(f"리다이렉트가 너무 많습니다: {url}")


def _decode(body: bytes, content_type: str) -> str:
    """헤더의 charset → HTML의 meta charset → utf-8. 사내 사이트에 EUC-KR이 아직 많다."""
    match = re.search(r"charset=([\w-]+)", content_type, re.I) or re.search(
        rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    charset = match.group(1) if match else "utf-8"
    if isinstance(charset, bytes):
        charset = charset.decode("ascii", "ignore")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- HTML 읽기

class _Page(HTMLParser):
    """페이지 한 장에서 메뉴·제목·표 머리글·입력 칸·본문을 뽑는다(표준 라이브러리만)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[dict] = []
        self.headings: list[str] = []
        self.tables: list[list[str]] = []
        self.fields: list[str] = []
        self.has_password = False
        self._text: list[str] = []
        self._stack: list[tuple[str, bool]] = []
        self._skip = 0
        self._capture: dict | None = None   # 지금 모으는 글자의 자리(a·h·th·title·label)

    @property
    def in_nav(self) -> bool:
        return any(flag for _, flag in self._stack)

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
        nav = (tag == "nav" or a.get("role", "").lower() in _NAV_ROLES
               or bool(_NAV_HINT.search(f"{a.get('class', '')} {a.get('id', '')}")))
        if tag not in _VOID:
            self._stack.append((tag, nav))
        if tag == "a" and a.get("href"):
            self._capture = {"kind": "a", "href": a["href"], "text": [], "nav": self.in_nav}
        elif tag in ("h1", "h2", "h3", "title", "th", "label"):
            self._capture = {"kind": tag, "text": []}
            if tag == "th" and (not self.tables or self._new_table):
                self.tables.append([])
                self._new_table = False
        elif tag == "table":
            self._new_table = True
        elif tag in ("input", "select", "textarea"):
            kind = a.get("type", "text").lower()
            if kind == "password":
                self.has_password = True
            if kind not in ("hidden", "submit", "button", "password", "image", "reset"):
                label = a.get("aria-label") or a.get("placeholder") or a.get("title") or a.get("name")
                if label:
                    self.fields.append(label.strip()[:60])

    _new_table = False

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template") and self._skip:
            self._skip -= 1
        cap = self._capture
        if cap and (cap["kind"] == tag or (cap["kind"] == "a" and tag == "a")):
            text = " ".join("".join(cap["text"]).split())
            if cap["kind"] == "a":
                self.links.append({"href": cap["href"], "text": text[:80], "nav": cap["nav"]})
            elif tag == "title":
                self.title = text[:200]
            elif tag == "th" and self.tables and text:
                if len(self.tables[-1]) < 20:
                    self.tables[-1].append(text[:40])
            elif tag == "label" and text:
                self.fields.append(text[:60])
            elif text:
                self.headings.append(text[:120])
            self._capture = None
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._skip:
            return
        if self._capture is not None:
            self._capture["text"].append(data)
        if data.strip():
            self._text.append(data.strip())

    def text(self) -> str:
        return " ".join(" ".join(self._text).split())[:TEXT_CAP]


def _normalize(base: str, href: str) -> str | None:
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    parts = urlsplit(urljoin(base, href))
    if parts.scheme not in ("http", "https"):
        return None
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _read_page(url: str, html: str) -> dict:
    parser = _Page()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — 깨진 HTML도 읽은 데까지는 쓴다
        pass
    links, seen = [], set()
    for link in parser.links:
        target = _normalize(url, link["href"])
        if not target or target in seen:
            continue
        seen.add(target)
        links.append({"url": target, "text": link["text"], "nav": link["nav"]})
    return {
        "url": url,
        "title": parser.title,
        "headings": parser.headings[:30],
        "tables": [t for t in parser.tables if t][:10],
        "fields": list(dict.fromkeys(parser.fields))[:30],
        "requires_login": parser.has_password,
        "links": links[:200],
        "text": parser.text(),
    }


# ---------------------------------------------------------------- 웹 크롤

def _robots(origin: str, headers: dict) -> tuple[RobotFileParser, list[str]]:
    robots = RobotFileParser()
    sitemaps: list[str] = []
    try:
        status, _, _, body = _http_get(f"{origin}/robots.txt", headers, origin)
    except (httpx.HTTPError, SourceError):
        status, body = 0, b""
    lines = _decode(body, "").splitlines() if status == 200 else []
    robots.parse(lines)  # 없으면 전부 허용
    sitemaps = [ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("sitemap:")]
    return robots, sitemaps


def _sitemap_urls(origin: str, extra: list[str], headers: dict) -> list[str]:
    found: list[str] = []
    for url in [*extra, f"{origin}/sitemap.xml"][:3]:
        try:
            status, _, _, body = _http_get(url, headers, origin)
        except (httpx.HTTPError, SourceError):
            continue
        if status != 200:
            continue
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", _decode(body, ""))[:MAX_PAGES * 2]:
            if _origin(loc) == origin and loc not in found:
                found.append(loc)
    return found


def _file_ext(url: str, ctype: str = "") -> str:
    """받을 파일의 확장자 — 주소의 확장자, 없으면 content-type. 받지 않는 형식이면 ""."""
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in FILE_TYPES:
        return suffix
    return _FILE_CTYPES.get(ctype.split(";")[0].strip().lower(), "")


def _crawl(start: str, headers: dict, known: dict | None = None) -> dict:
    """같은 출처 안을 돈다. known은 지난 스캔의 주소별 결과(scan_history) — 범위를 정하는 데 쓴다."""
    origin = _origin(start)
    known = known or {}
    robots, sitemap_hint = _robots(origin, headers)
    sitemap = _sitemap_urls(origin, sitemap_hint, headers)
    # 지난번에 받은 화면 — 링크 순서가 조금 바뀌어도 상한 밖으로 밀려나 "사라진" 것처럼 보이지 않게
    # 메뉴 다음 차례로 다시 본다.
    revisit = [(u, min(int(k.get("depth") or 1), MAX_DEPTH)) for u, k in known.items()
               if k.get("kind") == "page" and k.get("status") == "ok" and _origin(u) == origin]
    queue: list[tuple[str, int]] = [(start, 0)]
    seen: set[str] = set()
    pages: list[dict] = []
    skipped: list[dict] = []
    downloads: dict[str, dict] = {}
    while queue and len(pages) < MAX_PAGES:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not robots.can_fetch(USER_AGENT, url):
            skipped.append({"url": url, "reason": "robots.txt가 막음"})
            continue
        # 지난번에 없던 주소는 한 번 쉰다 — 쪽수 상한을 살아 있는 화면에 쓴다. 쉰 다음 스캔에서는
        # 다시 본다(이력에 "쉼"으로 남아 404가 아니게 된다).
        if url != start and known.get(url, {}).get("status") in ("HTTP 404", "HTTP 410"):
            skipped.append({"url": url, "reason": "지난 스캔에서 없던 주소 — 이번에는 건너뜀",
                            "status": "rested"})
            continue
        try:
            status, final, ctype, body = _http_get(url, headers, origin)
        except (httpx.HTTPError, SourceError) as e:
            skipped.append({"url": url, "reason": f"요청 실패: {e}"[:200]})
            continue
        if status >= 400:
            skipped.append({"url": url, "reason": f"HTTP {status}"})
            continue
        if "html" not in ctype.lower():
            if _file_ext(final, ctype) and _origin(final) == origin:
                # 확장자 없이 파일을 주는 주소 — 화면이 아니라 내려받기다. 잘렸으면 다시 받는다.
                downloads.setdefault(final, {"url": final, "text": "", "page": "",
                                             "body": body if len(body) < MAX_BYTES else None,
                                             "ctype": ctype})
                continue
            skipped.append({"url": url, "reason": f"HTML 아님({ctype.split(';')[0] or '?'})"})
            continue
        page = _read_page(final, _decode(body, ctype))
        # 로그인 화면으로 돌려보내졌으면 그 페이지는 로그인 페이지다 — 헤더(쿠키)가 필요하다.
        if final != url and re.search(r"login|signin|sso|auth", final, re.I):
            page["requires_login"] = True
        page["depth"] = depth
        pages.append(page)
        if _origin(final) != origin:
            continue
        nav, rest = [], []
        for link in page["links"]:
            target = link["url"]
            if (_origin(target) != origin or target in seen
                    or _DANGEROUS_LINK.search(f"{target} {link['text']}")):
                continue
            if _file_ext(target):
                # 내려받기는 깊이와 상관없이 모은다 — 화면 쪽수 상한에 들지 않는다.
                downloads.setdefault(target, {"url": target, "text": link["text"], "page": final})
                continue
            if urlsplit(target).path.lower().endswith(_DOWNLOAD_SUFFIXES):
                continue
            (nav if link["nav"] else rest).append((target, depth + 1))
        if depth >= MAX_DEPTH:
            continue
        # 메뉴 링크가 먼저다 — 상한 안에서 사이트의 뼈대를 먼저 본다. 그다음이 지난번에 받은 화면.
        queue += nav
        if depth == 0:
            queue += [r for r in revisit if r[0] not in seen]
        queue += rest
        if depth == 0:
            queue.extend((u, 1) for u in sitemap if u not in seen)
    return {"origin": origin, "pages": pages, "skipped": skipped[:50],
            "downloads": list(downloads.values()),
            "sitemap": len(sitemap), "limits": {"pages": MAX_PAGES, "depth": MAX_DEPTH}}


# ---------------------------------------------------------------- 내려받기 파일

def _file_dir(source_id: int) -> Path:
    """받은 파일을 저장 전까지 두는 자리 — 저장은 사람이 정하는 다음 단계라 그 사이에 둔다."""
    root = Path(get_settings().storage_root or "./data/storage").resolve()
    return root / ".infosource" / "files" / str(source_id)


def _file_name(url: str, text: str, ext: str) -> str:
    """저장소에 둘 이름 — 주소의 파일 이름, 없으면 링크 글자. 같은 주소는 늘 같은 이름이다."""
    base = Path(unquote(urlsplit(url).path)).name
    stem = Path(base).stem if Path(base).suffix.lower() == ext else (text or Path(base).stem)
    return _folder_name(stem or "file")[:80] + ext


def _download(downloads: list[dict], headers: dict, origin: str) -> tuple[list[dict], list[dict]]:
    """모은 내려받기 링크를 받는다 — (받은 것[data 포함], 받지 않은 것)."""
    got, skipped = [], []
    for d in downloads[MAX_FILES:]:
        skipped.append({"url": d["url"], "reason": f"파일 수 상한({MAX_FILES}개)을 넘음"})
    for d in downloads[:MAX_FILES]:
        body, ctype = d.get("body"), d.get("ctype", "")
        if body is None:
            try:
                status, final, ctype, body = _http_get(d["url"], headers, origin, limit=MAX_FILE_BYTES)
            except (httpx.HTTPError, SourceError) as e:
                skipped.append({"url": d["url"], "reason": f"요청 실패: {e}"[:200]})
                continue
            if status >= 400:
                skipped.append({"url": d["url"], "reason": f"HTTP {status}"})
                continue
            if "html" in ctype.lower():
                # 파일 대신 화면이 왔다 — 대개 로그인 화면이다.
                skipped.append({"url": d["url"], "reason": "파일 대신 화면이 옴(로그인이 필요할 수 있음)"})
                continue
        if len(body) >= MAX_FILE_BYTES:
            skipped.append({"url": d["url"], "reason": f"파일이 너무 큼({MAX_FILE_BYTES // 1_000_000}MB 이상)"})
            continue
        ext = d.get("ext") or _file_ext(d["url"], ctype)
        if not ext or not body:
            skipped.append({"url": d["url"], "reason": "받지 않는 형식이거나 빈 파일"})
            continue
        got.append({"url": d["url"], "name": d.get("name") or _file_name(d["url"], d.get("text", ""), ext),
                    "text": d.get("text", ""), "page": d.get("page", ""), "ext": ext, "data": body})
    return got, skipped


def _stage(source_id: int, items: list[dict]) -> tuple[list[dict], list[str]]:
    """받은 파일을 내용 해시 이름으로 둔다. **같은 내용은 한 벌만** — 주소가 달라도 하나로 친다.

    이번 스캔에 없는 파일은 지운다(받아 둔 자리에 지난 스캔의 찌꺼기가 쌓이지 않게).
    """
    out_dir = _file_dir(source_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    files, notes, by_sha = [], [], {}
    for item in items:
        data = item.pop("data")
        sha = hashlib.sha256(data).hexdigest()
        if sha in by_sha:
            notes.append(f"내용이 같은 파일을 하나로 합쳤습니다: {item['url']} = {by_sha[sha]}")
            continue
        by_sha[sha] = item["url"]
        (out_dir / f"{sha}{item['ext']}").write_bytes(data)
        files.append({**item, "sha": sha, "size": len(data),
                      "category": FILE_TYPES.get(item["ext"], "documents")})
    keep = {f"{f['sha']}{f['ext']}" for f in files}
    for old in out_dir.iterdir():
        if old.name not in keep:
            old.unlink(missing_ok=True)
    return files, notes


def staged_file(row: InfoSource, f: dict) -> Path:
    return _file_dir(row.id) / f"{f['sha']}{f['ext']}"


# ---------------------------------------------------------------- 브라우저(선택)

def browser_available() -> bool:
    """Playwright가 설치돼 있는가. 브라우저 바이너리까지는 실제로 띄울 때 안다."""
    return importlib.util.find_spec("playwright") is not None


def _shot_dir(source_id: int) -> Path:
    root = Path(get_settings().storage_root or "./data/storage").resolve()
    # 점으로 시작하는 폴더 — 색인·목록에서 빠진다(docsearch.skip_dir).
    return root / ".infosource" / str(source_id)


def _browse(source_id: int, urls: list[str], headers: dict, origin: str) -> dict:
    """렌더링한 DOM·aria snapshot·캡처. 실패하면 이유만 남기고 HTTP 결과로 간다."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 — 선택 의존성

    out_dir = _shot_dir(source_id)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900},
                                          user_agent=USER_AGENT)

            def attach(route, request):
                # 같은 출처에만 헤더를 붙인다 — extra_http_headers는 모든 호스트에 붙는다.
                if _origin(request.url) == origin and headers:
                    route.continue_(headers={**request.headers, **headers})
                else:
                    route.continue_()

            context.route("**/*", attach)
            page = context.new_page()
            for i, url in enumerate(urls[:MAX_SHOTS]):
                try:
                    page.goto(url, wait_until="networkidle", timeout=int(TIMEOUT * 1000))
                    shot = out_dir / f"{i}.jpg"
                    page.screenshot(path=str(shot), type="jpeg", quality=60)
                    try:
                        aria = page.locator("body").aria_snapshot()
                    except Exception:  # noqa: BLE001 — 옛 Playwright에는 없다
                        aria = ""
                    rendered.append({"url": url, "shot": i, "aria": aria[:6000],
                                     "html": page.content()})
                except Exception as e:  # noqa: BLE001
                    rendered.append({"url": url, "error": str(e)[:200]})
        finally:
            browser.close()
    return {"pages": rendered}


# ---------------------------------------------------------------- API·MCP

def _json_of(url: str, headers: dict, origin: str):
    try:
        status, _, ctype, body = _http_get(url, headers, origin)
    except (httpx.HTTPError, SourceError):
        return None
    if status != 200:
        return None
    try:
        return json.loads(_decode(body, ctype))
    except ValueError:
        return None


def _shape(value, depth: int = 0):
    """JSON 값의 모양 — 키와 타입만(값은 싣지 않는다)."""
    if isinstance(value, dict):
        if depth >= 2:
            return "object"
        return {k: _shape(v, depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def _ref(spec: dict, schema: dict) -> dict:
    ref = schema.get("$ref", "") if isinstance(schema, dict) else ""
    if ref.startswith("#/"):
        node = spec
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return node if isinstance(node, dict) else {}
    return schema if isinstance(schema, dict) else {}


def _fields_of(spec: dict, schema: dict) -> list[str]:
    schema = _ref(spec, schema)
    if schema.get("type") == "array":
        schema = _ref(spec, schema.get("items") or {})
    return list((schema.get("properties") or {}).keys())[:20]


def _scan_api(url: str, headers: dict) -> dict:
    origin = _origin(url)
    spec, found_at = None, ""
    for candidate in [url, *(origin + p for p in _OPENAPI_PATHS)]:
        data = _json_of(candidate, headers, origin)
        if isinstance(data, dict) and ("openapi" in data or "swagger" in data) and "paths" in data:
            spec, found_at = data, candidate
            break
    if spec is None:
        sample = _json_of(url, headers, origin)
        return {"openapi": "", "title": "", "endpoints": [],
                "sample_shape": _shape(sample) if sample is not None else None}
    endpoints = []
    for path, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete") or not isinstance(op, dict):
                continue
            ok = (op.get("responses") or {}).get("200") or (op.get("responses") or {}).get("201") or {}
            schema = ((ok.get("content") or {}).get("application/json") or {}).get("schema") \
                or ok.get("schema") or {}
            endpoints.append({
                "method": method.upper(),
                "path": path,
                "summary": str(op.get("summary") or op.get("description") or "")[:200],
                "params": [str(p.get("name")) for p in op.get("parameters") or []
                           if isinstance(p, dict) and p.get("name")][:20],
                "fields": _fields_of(spec, schema),
                "read_only": method.lower() == "get",
            })
    info = spec.get("info") or {}
    return {"openapi": found_at, "title": str(info.get("title") or ""),
            "description": str(info.get("description") or "")[:1000],
            "endpoints": endpoints[:300]}


_READ_TOOL = re.compile(r"^(get|list|search|read|find|query|describe|fetch|lookup|show|count)", re.I)


def _mcp_rpc(url: str, headers: dict, method: str, params: dict) -> dict:
    sent = {"content-type": "application/json", "accept": "application/json, text/event-stream",
            **headers}
    data = mcp_client._post_rpc(url, sent, {"jsonrpc": "2.0", "id": 1, "method": method,
                                           "params": params})
    if "error" in data:
        raise SourceError(str(data["error"].get("message", "MCP 서버 오류")))
    return data.get("result") or {}


def _scan_mcp(url: str, headers: dict) -> dict:
    tools = _mcp_rpc(url, headers, "tools/list", {}).get("tools", [])
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        hint = (t.get("annotations") or {}).get("readOnlyHint")
        name = str(t.get("name") or "")
        out.append({
            "name": name,
            "description": str(t.get("description") or "")[:300],
            "params": list(((t.get("inputSchema") or {}).get("properties") or {}).keys())[:20],
            "read_only": bool(hint) if hint is not None else bool(_READ_TOOL.match(name)),
        })
    return {"tools": out}


# ---------------------------------------------------------------- LLM 정리

PROMPT = """당신은 사내 정보 출처를 스캔한 결과를 정리한다. 아래 사실만 근거로 쓰고, 사실에 없는
주소를 만들지 마라. 출력은 JSON 객체 하나다(설명·코드펜스 없이).

{{
  "summary": "이 출처가 무엇이고 무엇을 조회할 수 있는지 2~4문장",
  "menu": [{{"label": "메뉴 이름", "url": "사실에 있는 주소", "children": [같은 모양]}}],
  "pages": [{{"url": "사실에 있는 주소", "kind": "list|detail|form|dashboard|doc|login|other",
              "info": "이 화면에서 조회할 수 있는 정보", "filters": ["조회 조건"]}}],
  "keywords": ["이 출처의 정보를 대표하는 낱말 5~10개"],
  "stores": {{"유형": {{"mode": "existing|new", "name": "저장소 이름", "reason": "이유 한 문장"}}}}
}}

저장소 추천 규칙: 저장 유형마다 따로 고른다 — 유형이 다르면 맞는 저장소도 다를 수 있다(예: 화면
정리는 업무 시스템 안내 저장소, 내려받은 규정 PDF는 규정 저장소). 기존 저장소 중 주제가 같은 것이
있으면 existing과 그 이름을, 없으면 new와 새 이름(소문자·숫자·하이픈, 40자 이내)을 쓴다.
menu·pages는 웹사이트일 때만 채운다.

출처 종류: {kind}
출처 이름: {name}
기존 저장소: {stores}
저장 유형: {categories}

수집한 사실:
{facts}
"""

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _parse(text: str) -> dict:
    cleaned = _FENCE_RE.sub("", (text or "").strip())
    for candidate in (cleaned, cleaned[cleaned.find("{"):cleaned.rfind("}") + 1]):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _facts(kind: str, scan: dict) -> str:
    if kind == "web":
        pages = [{
            "url": p["url"], "title": p["title"], "headings": p["headings"][:10],
            "menu_links": [{"url": link["url"], "text": link["text"]}
                           for link in p["links"] if link["nav"]][:40],
            "tables": p["tables"][:5], "fields": p["fields"][:15],
            "requires_login": p["requires_login"], "text": p["text"][:500],
            **({"aria": p["aria"][:2000]} if p.get("aria") else {}),
        } for p in scan.get("pages", [])]
        files = [{"name": f["name"], "link_text": f["text"], "type": f["category"]}
                 for f in scan.get("files") or []]
        facts = {"pages": pages, **({"files": files} if files else {})}
        if scan.get("menu_dom"):
            # 화면에 그려진 메뉴 트리(브라우저 스캔) — 메뉴 구성의 1차 근거다.
            facts = {"menu_tree": scan["menu_dom"], **facts}
        return json.dumps(facts, ensure_ascii=False)[:60000]
    return json.dumps(scan, ensure_ascii=False)[:60000]


def categories_of(scan: dict) -> list[str]:
    """이 스캔에서 저장할 유형 — 조회 정보 정리는 늘, 파일 유형은 받은 것이 있을 때만."""
    present = {f.get("category") for f in scan.get("files") or []}
    return ["pages", *(c for c in CATEGORIES if c != "pages" and c in present)]


def _ask_llm(db: Session, row: InfoSource, scan: dict, stores: list[str]) -> tuple[dict, list[str]]:
    """정리 한 번. 실패해도 스캔은 실패가 아니다 — 이유를 notes에 남기고 결정론 결과로 간다."""
    provider = llm.default_provider(db)
    if provider is None:
        return {}, ["기본 LLM 프로바이더가 없어 정리 없이 수집한 구조만 남겼습니다."]
    text = PROMPT.format(kind=row.kind, name=row.name, stores=", ".join(stores) or "(없음)",
                         categories=", ".join(f"{c}({CATEGORIES[c][0]})" for c in categories_of(scan)),
                         facts=_facts(row.kind, scan))
    images = []
    for p in scan.get("pages", []):
        shot = p.get("shot")
        if shot is None:
            continue
        path = _shot_dir(row.id) / f"{shot}.jpg"
        if path.is_file():
            images.append({"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()}})
    notes: list[str] = []
    attempts = [[{"type": "text", "text": text}, *images]] if images else []
    attempts.append(text)
    for content in attempts:
        try:
            return _parse(llm.chat_completion(provider, [{"role": "user", "content": content}], db)), notes
        except Exception as e:  # noqa: BLE001 — 비전을 못 받는 모델이면 글로만 다시 묻는다
            notes.append(f"LLM 정리 실패{'(캡처 포함)' if content is not text else ''}: {str(e)[:200]}")
    return {}, notes


# ---------------------------------------------------------------- 결과 조립

def _menu_fallback(pages: list[dict]) -> list[dict]:
    """LLM 없이 — 시작 페이지의 메뉴 링크가 1단, 그 페이지에만 있는 메뉴 링크가 2단."""
    if not pages:
        return []
    by_url = {p["url"]: p for p in pages}
    top = [link for link in pages[0]["links"] if link["nav"]][:30]
    top_urls = {link["url"] for link in top}
    menu = []
    for link in top:
        child_page = by_url.get(link["url"])
        children = [] if not child_page else [
            {"label": c["text"] or c["url"], "url": c["url"], "children": []}
            for c in child_page["links"] if c["nav"] and c["url"] not in top_urls
        ][:15]
        menu.append({"label": link["text"] or link["url"], "url": link["url"], "children": children})
    return menu


def _page_kind(p: dict) -> str:
    if p["requires_login"]:
        return "login"
    if p["tables"]:
        return "list"
    if p["fields"]:
        return "form"
    return "doc"


def _clean_menu(items, known: set[str], dropped: list[str], depth: int = 0) -> list[dict]:
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or depth > 3:
            continue
        url = str(item.get("url") or "")
        if url and url not in known:
            dropped.append(url)
            url = ""
        out.append({"label": str(item.get("label") or url or "(이름 없음)")[:80], "url": url,
                    "children": _clean_menu(item.get("children"), known, dropped, depth + 1)})
    return out[:60]


def _menu_urls(items: list[dict]) -> set[str]:
    return {u for m in items for u in ({m["url"]} | _menu_urls(m["children"])) if u}


def _web_result(scan: dict, data: dict) -> tuple[dict, list[str]]:
    pages = scan["pages"]
    crawled = {p["url"] for p in pages}
    dom_menu = scan.get("menu_dom") or []
    linked = crawled | {link["url"] for p in pages for link in p["links"]} | _menu_urls(dom_menu)
    dropped: list[str] = []
    # 브라우저 스캔은 화면에 그려진 메뉴 트리를 그대로 가져온다 — 링크로 짐작한 것보다 낫다.
    menu = _clean_menu(data.get("menu"), linked, dropped) or dom_menu or _menu_fallback(pages)
    by_url = {str(p.get("url")): p for p in data.get("pages") or [] if isinstance(p, dict)}
    dropped += [u for u in by_url if u not in crawled]
    out_pages = []
    for p in pages:
        hint = by_url.get(p["url"], {})
        out_pages.append({
            "url": p["url"], "title": p["title"],
            "kind": str(hint.get("kind") or _page_kind(p)),
            "info": str(hint.get("info") or " · ".join(p["headings"][:5]))[:500],
            "filters": [str(f)[:60] for f in hint.get("filters") or p["fields"]][:15],
            "tables": p["tables"], "headings": p["headings"], "fields": p["fields"],
            "requires_login": p["requires_login"], "text": p["text"], "depth": p.get("depth", 0),
            **({"shot": p["shot"]} if p.get("shot") is not None else {}),
        })
    notes = [f"LLM이 낸 주소 중 받지 않은 것을 버렸습니다: {', '.join(sorted(set(dropped))[:5])}"
             ] if dropped else []
    if pages and all(p["requires_login"] for p in pages):
        notes.append("모든 페이지가 로그인 화면입니다 — 요청 헤더에 세션 쿠키를 넣고 다시 스캔하세요.")
    return {"menu": menu, "pages": out_pages}, notes


def _keywords(row: InfoSource, scan: dict, data: dict) -> list[str]:
    words = [str(w).strip() for w in data.get("keywords") or [] if str(w).strip()]
    if not words:
        titles = [p.get("title", "") for p in scan.get("pages", [])[:5]]
        titles += [scan.get("title", "")] + [e.get("summary", "") for e in scan.get("endpoints", [])[:5]]
        words = [w for t in titles for w in re.split(r"[\s|·:\-_/]+", t) if len(w) >= 2]
    return list(dict.fromkeys(words))[:10]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40].strip("-")
    return slug


def _evidence(keywords: list[str], stores: list[storage.Store]) -> list[dict]:
    """기존 저장소마다 키워드가 걸리는 문서 수 — 추천의 근거로 화면에 그대로 보인다."""
    out = []
    for s in stores:
        total, matched = 0, []
        for word in keywords:
            try:
                hits = len(docsearch.search(s.name, word, limit=50)["hits"])
            except Exception:  # noqa: BLE001 — 색인이 아직 없는 저장소
                hits = 0
            if hits:
                matched.append(word)
                total += hits
        out.append({"store": s.name, "hits": total, "keywords": matched, "read_only": s.read_only})
    return sorted(out, key=lambda e: -e["hits"])


def default_folder(name: str) -> str:
    """새 저장소 폴더 제안 — 기존 문서 폴더의 형제 자리, 없으면 내부 저장소 옆."""
    roots = [s.root for s in storage.visible_stores()]
    base = roots[0].parent if roots else Path(get_settings().storage_root or "./data/storage").resolve().parent / "doc-sources"
    return str(base / name)


def _proposal(row: InfoSource, pick: dict, evidence: list[dict], planned: dict[str, str]) -> dict:
    """유형 하나의 저장 위치. planned는 이 제안에서 이미 새로 만들기로 한 저장소 {이름: 폴더} —
    유형 여럿이 같은 새 저장소를 고르면 하나를 함께 쓴다."""
    writable = [e for e in evidence if not e["read_only"]]
    names = {e["store"] for e in writable}
    mode, name = str(pick.get("mode") or ""), str(pick.get("name") or "")
    reason = str(pick.get("reason") or "")[:300]
    if mode == "existing" and name in names:
        return {"mode": "existing", "store": name, "reason": reason or "주제가 같은 저장소입니다.",
                "evidence": evidence}
    best = writable[0] if writable else None
    if not reason and best and best["hits"] >= 5:
        return {"mode": "existing", "store": best["store"], "evidence": evidence,
                "reason": f"키워드가 이 저장소 문서 {best['hits']}건에 걸립니다."}
    candidate = name if mode == "new" and storage._NAME_RE.match(name) else ""
    candidate = candidate or slugify(row.name) or slugify(urlsplit(row.url).hostname or "") or f"source-{row.id}"
    if candidate not in planned:
        taken = {s.name for s in storage.stores()}
        base, n = candidate, 2
        while candidate in taken:
            candidate = f"{base[:37]}-{n}"
            n += 1
        planned.setdefault(candidate, default_folder(candidate))
    return {"mode": "new", "store": candidate, "path": planned[candidate],
            "reason": reason or "주제가 맞는 기존 저장소가 없습니다.", "evidence": evidence}


def _file_words(files: list[dict]) -> list[str]:
    words = [w for f in files for t in (Path(f["name"]).stem, f.get("text") or "")
             for w in re.split(r"[\s|·:\-_/().\[\]]+", t) if len(w) >= 2 and not w.isdigit()]
    return list(dict.fromkeys(words))[:10]


def _targets(row: InfoSource, data: dict, result: dict, visible: list[storage.Store]) -> dict:
    """저장 유형마다 위치를 정한다 — 조회 정보 정리는 출처의 키워드로, 파일 유형은 그 파일들의
    이름·링크 글자로 기존 저장소를 견준다. 파일 유형에 근거가 없으면 정리와 같은 자리로 간다
    (같은 출처의 것은 한곳에 모여 있는 편이 찾기 쉽다).

    지난번에 저장한 유형은 그 자리를 그대로 제안한다 — 다른 곳에 쓰면 같은 파일이 두 벌이 된다."""
    picks = data.get("stores") if isinstance(data.get("stores"), dict) else {}
    planned: dict[str, str] = {}
    writable = {s.name for s in visible if not s.read_only}
    previous = {f["category"]: f["store"] for f in row.saved_files or [] if f["store"] in writable}
    if row.saved_files is None and row.target_store in writable:   # 저장 목록을 남기기 전에 저장한 출처
        previous["pages"] = row.target_store
    out: dict[str, dict] = {}
    for category in categories_of(result):
        files = [f for f in result.get("files") or [] if f["category"] == category]
        pick = picks.get(category) if isinstance(picks.get(category), dict) else {}
        evidence = _evidence(_file_words(files) if files else result["keywords"], visible)
        if category in previous:
            out[category] = {"mode": "existing", "store": previous[category], "evidence": evidence,
                             "reason": "지난번에 저장한 저장소입니다 — 같은 자리에 덮어씁니다."}
            continue
        if category == "pages":
            out[category] = _proposal(row, pick, evidence, planned)
            continue
        best = next((e for e in evidence if not e["read_only"]), None)
        if not pick and not (best and best["hits"] >= 5):
            same = {k: v for k, v in out["pages"].items() if k != "reason"}
            out[category] = {**same, "evidence": evidence,
                             "reason": "따로 맞는 저장소가 없어 조회 정보 정리와 같은 자리에 둡니다."}
            continue
        out[category] = _proposal(row, pick, evidence, planned)
    return out


def scan(db: Session, row: InfoSource) -> None:
    """스캔 한 번 — 결과·제안을 행에 쓴다. 작업 큐에서 돈다(start_scan)."""
    headers = _headers(row)
    notes: list[str] = []
    if row.kind == "web":
        result = _crawl(row.url, headers, known_urls(db, row, "server"))
        got, missed = _download(result.pop("downloads"), headers, result["origin"])
        result["files"], stage_notes = _stage(row.id, got)
        result["skipped"] = (result["skipped"] + missed)[:80]
        notes += stage_notes
        if not result["pages"]:
            raise SourceError("받은 페이지가 없습니다: "
                              + "; ".join(f"{s['url']} — {s['reason']}" for s in result["skipped"][:3]))
        if browser_available():
            try:
                targets = [result["pages"][0]["url"]] + [
                    link["url"] for link in result["pages"][0]["links"] if link["nav"]
                    and _origin(link["url"]) == result["origin"]
                    and not _DANGEROUS_LINK.search(f"{link['url']} {link['text']}")]
                rendered = _browse(row.id, list(dict.fromkeys(targets)), headers, result["origin"])
                by_url = {p["url"]: p for p in result["pages"]}
                for r in rendered["pages"]:
                    if r.get("error"):
                        notes.append(f"브라우저로 열지 못함: {r['url']} — {r['error']}")
                        continue
                    seen_page = _read_page(r["url"], r.pop("html"))
                    page = by_url.get(r["url"])
                    if page is None:
                        seen_page["depth"] = 1
                        result["pages"].append(seen_page)
                        page = seen_page
                    elif len(seen_page["links"]) > len(page["links"]):
                        # 스크립트가 그리는 메뉴(SPA) — 렌더링한 쪽이 더 많이 안다.
                        page.update({k: seen_page[k] for k in ("links", "headings", "tables", "fields")})
                    page["shot"], page["aria"] = r["shot"], r["aria"]
                result["browser"] = True
            except Exception as e:  # noqa: BLE001 — 브라우저가 없어도 HTTP 결과는 쓴다
                notes.append(f"브라우저 스캔을 건너뛰었습니다: {str(e)[:200]}")
                result["browser"] = False
        else:
            result["browser"] = False
    elif row.kind == "api":
        result = _scan_api(row.url, headers)
        if not result["endpoints"] and result.get("sample_shape") is None:
            raise SourceError("OpenAPI 문서도, JSON 응답도 찾지 못했습니다.")
    else:
        result = _scan_mcp(row.url, headers)
    _finish(db, row, result, notes)


# ---------------------------------------------------------------- 스캔 이력

def known_urls(db: Session, row: InfoSource, via: str) -> dict:
    """같은 방법(server·browser)으로 한 지난 스캔의 주소별 결과 — {url: {kind, status, sha, depth}}.

    방법끼리는 섞지 않는다: 브라우저 스캔은 로그인한 화면을 보고 서버는 못 본다 — 섞으면 서버가
    못 여는 화면을 범위에 넣고, 방법을 바꿀 때마다 전부 "새로"·"없어짐"으로 보인다.
    """
    last = db.execute(
        select(InfoSourceScan).where(InfoSourceScan.source_id == row.id, InfoSourceScan.via == via,
                                     InfoSourceScan.status == "done")
        .order_by(InfoSourceScan.id.desc()).limit(1)
    ).scalar_one_or_none()
    return dict(last.urls or {}) if last else {}


def _page_sha(p: dict) -> str:
    # 화면이 바뀌었는지는 저장되는 내용으로 본다(정리 문구는 LLM이 매번 조금씩 달리 쓴다).
    keys = ("title", "headings", "tables", "fields", "text")
    return hashlib.sha256(json.dumps([p.get(k) for k in keys], ensure_ascii=False).encode()).hexdigest()


def _compare(result: dict, before: dict) -> tuple[dict, dict]:
    """이번 결과에 바뀜 표시(new·changed·same)를 달고, 이력에 남길 주소표와 집계를 만든다."""
    urls: dict[str, dict] = {}
    counts = {"new": 0, "changed": 0, "same": 0, "gone": 0}
    for kind, items in (("page", result.get("pages") or []), ("file", result.get("files") or [])):
        for item in items:
            sha = item.get("sha") or _page_sha(item)
            prev = before.get(item["url"], {})
            if prev.get("kind") != kind or prev.get("status") != "ok":
                change = "new"
            else:
                change = "same" if prev.get("sha") == sha else "changed"
            item["change"] = change
            counts[change] += 1
            urls[item["url"]] = {"kind": kind, "status": "ok", "sha": sha,
                                 **({"depth": item.get("depth", 0)} if kind == "page" else {})}
    for s in result.get("skipped") or []:
        urls.setdefault(s["url"], {"kind": "skip", "status": s.get("status") or s["reason"][:40]})
    gone = [u for u, k in before.items() if k.get("status") == "ok" and u not in urls]
    counts["gone"] = len(gone)
    result["gone"] = gone[:50]
    return urls, counts


def _record(db: Session, row: InfoSource, *, via: str, started, status: str, urls=None,
            counts=None, pages: int = 0, files: int = 0, error: str | None = None) -> None:
    db.add(InfoSourceScan(source_id=row.id, via=via, status=status, started_at=started,
                          finished_at=datetime.now(timezone.utc), pages=pages, files=files,
                          changes=counts, urls=urls, error=error))
    db.flush()
    old = db.execute(
        select(InfoSourceScan).where(InfoSourceScan.source_id == row.id)
        .order_by(InfoSourceScan.id.desc()).offset(MAX_HISTORY)
    ).scalars().all()
    for o in old:
        db.delete(o)


def history(db: Session, row: InfoSource) -> list[dict]:
    rows = db.execute(
        select(InfoSourceScan).where(InfoSourceScan.source_id == row.id)
        .order_by(InfoSourceScan.id.desc())
    ).scalars().all()
    return [{"id": r.id, "via": r.via, "status": r.status, "started_at": r.started_at,
             "finished_at": r.finished_at, "pages": r.pages, "files": r.files,
             "changes": r.changes, "error": r.error} for r in rows]


def _finish(db: Session, row: InfoSource, result: dict, notes: list[str]) -> None:
    """수집한 것을 정리·제안해 행에 쓴다 — 서버 스캔과 브라우저 스캔이 같은 길로 끝난다."""
    started = row.scanned_at   # 스캔 중에는 시작 시각이다(start_scan)
    visible = storage.visible_stores()
    data, llm_notes = _ask_llm(db, row, result, [s.name for s in visible])
    notes += llm_notes
    if row.kind == "web":
        shaped, web_notes = _web_result(result, data)
        result.update(shaped)
        notes += web_notes
    via = result.get("via") or "server"
    before = known_urls(db, row, via)
    urls, counts = _compare(result, before)
    if before:
        notes.append(f"지난 스캔과 비교 — 새로 {counts['new']} · 바뀜 {counts['changed']} · "
                     f"그대로 {counts['same']} · 없어짐 {counts['gone']}")
    result["summary"] = str(data.get("summary") or "")[:2000]
    result["keywords"] = _keywords(row, result, data)
    result["notes"] = notes
    row.scan = result
    row.proposal = {"targets": _targets(row, data, result, visible)}
    row.status, row.error = "scanned", None
    row.scanned_at = datetime.now(timezone.utc)
    _record(db, row, via=via, started=started, status="done", urls=urls,
            counts=counts, pages=len(result.get("pages") or []), files=len(result.get("files") or []))
    db.commit()


def _fail(db: Session, source_id: int, error: Exception, via: str) -> None:
    db.rollback()
    row = db.get(InfoSource, source_id)
    started = row.scanned_at
    row.status, row.error = "failed", str(error)[:2000]
    _record(db, row, via=via, started=started, status="failed", error=row.error)
    db.commit()


def _submit(source_id: int) -> None:
    """테스트에서 인라인으로 바꾸는 큐 경계."""
    jobs.submit(_scan_job, source_id)


def _scan_job(source_id: int) -> None:
    from ..db import SessionLocal  # noqa: PLC0415

    with SessionLocal() as session:
        row = session.get(InfoSource, source_id)
        if row is None:
            return
        try:
            scan(session, row)
        except Exception as e:  # noqa: BLE001 — 실패 이유는 화면에 그대로 보인다
            _fail(session, source_id, e, "server")


def _refuse_if_running(row: InfoSource) -> None:
    if row.status == "scanning" and row.scanned_at:
        started = row.scanned_at if row.scanned_at.tzinfo else row.scanned_at.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - started).total_seconds() < STALE_SCAN_SECONDS:
            raise ScanBusy("이미 스캔 중입니다.")


def start_scan(db: Session, row: InfoSource) -> None:
    _refuse_if_running(row)
    row.status, row.error = "scanning", None
    row.scanned_at = datetime.now(timezone.utc)   # 스캔 중에는 시작 시각이다
    db.commit()
    _submit(row.id)


# ---------------------------------------------------------------- 브라우저 스캔(북마크릿)
#
# SSO(EP·SiteMinder) 뒤의 사이트는 서버가 로그인할 수 없다(다중 인증). 사용자가 로그인한 탭에서
# 북마크릿이 화면을 읽어 **JSON 파일로 내려받고**, 그 파일을 콘솔이 올린다. 세션은 브라우저
# 밖으로 나가지 않는다. 파일로 오가는 이유: 운영 콘솔은 http라 https 페이지가 직접 부를 수 없다
# (혼합 콘텐츠). 올라온 것은 **남이 만든 입력**으로 다룬다 — 출처가 같은지 보고, 모양·길이를 자른다.

BROWSER_AGENT = "gpax-scan/1"
# 북마크릿이 받은 파일까지 싣는다(scanAgent.js: 파일 20개, 하나 10MB, 합 50MB) — base64로 4/3배가 된다.
MAX_UPLOAD_BYTES = 80_000_000
MAX_BROWSER_FILES = 20


def _s(value, cap: int) -> str:
    return str(value or "").strip()[:cap]


def _strs(values, count: int, cap: int) -> list[str]:
    return [_s(v, cap) for v in (values if isinstance(values, list) else [])[:count] if _s(v, cap)]


def _same(url, origin: str) -> str:
    url = _s(url, 1024)
    return url if url and _origin(url) == origin else ""


def _menu_in(items, origin: str, budget: list[int], depth: int = 0) -> list[dict]:
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or depth > 4 or budget[0] <= 0:
            continue
        budget[0] -= 1
        label = _s(item.get("label"), 80)
        children = _menu_in(item.get("children"), origin, budget, depth + 1)
        if label or children:
            out.append({"label": label or "(이름 없음)", "url": _same(item.get("url"), origin),
                        "children": children})
    return out


def from_browser(row: InfoSource, payload) -> dict:
    """북마크릿 결과 → _crawl과 같은 모양. 다른 사이트의 결과·모르는 파일은 받지 않는다."""
    if row.kind != "web":
        raise SourceError("브라우저 스캔은 웹사이트 출처에만 씁니다.")
    if not isinstance(payload, dict) or payload.get("agent") != BROWSER_AGENT:
        raise SourceError("브라우저 스캔 결과 파일이 아닙니다(콘솔의 북마크릿으로 만든 파일을 올리세요).")
    # 북마크는 출처마다 하나다 — 같은 사이트에 출처가 여럿이어도 다른 출처의 파일은 받지 않는다.
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    if source.get("id") != row.id:
        raise SourceError(f"다른 출처의 북마크로 만든 파일입니다: {_s(source.get('name'), 64) or '알 수 없음'} "
                          f"(이 출처는 {row.name}) — 이 화면의 북마크로 다시 스캔하세요.")
    origin = _origin(row.url)
    if _origin(_s(payload.get("origin"), 300)) != origin:
        raise SourceError(f"다른 사이트에서 만든 파일입니다: {_s(payload.get('origin'), 100)} "
                          f"(이 출처는 {origin})")
    pages, seen = [], set()
    for p in (payload.get("pages") if isinstance(payload.get("pages"), list) else [])[:MAX_PAGES * 2]:
        url = _same(p.get("url"), origin) if isinstance(p, dict) else ""
        if not url or url in seen:
            continue
        seen.add(url)
        links = [{"url": u, "text": _s(link.get("text"), 120), "nav": bool(link.get("nav"))}
                 for link in (p.get("links") if isinstance(p.get("links"), list) else [])[:200]
                 if isinstance(link, dict) and (u := _same(link.get("url"), origin))]
        tables = p.get("tables") if isinstance(p.get("tables"), list) else []
        pages.append({
            "url": url, "title": _s(p.get("title"), 200), "headings": _strs(p.get("headings"), 30, 200),
            "tables": [t for t in (_strs(t, 40, 80) for t in tables[:10]) if t],
            "fields": _strs(p.get("fields"), 30, 80), "requires_login": bool(p.get("requires_login")),
            "links": links, "text": _s(p.get("text"), TEXT_CAP), "depth": 0,
        })
    if not pages:
        raise SourceError("파일에 이 출처의 화면이 없습니다.")
    downloads, skipped = _browser_files(payload.get("files"), origin)
    return {"origin": origin, "pages": pages, "skipped": skipped, "sitemap": 0, "browser": False,
            "via": "browser", "menu_dom": _menu_in(payload.get("menu"), origin, [400]),
            "downloads": downloads}


def _browser_files(items, origin: str) -> tuple[list[dict], list[dict]]:
    """북마크릿이 받아 실은 파일 — 같은 출처의 주소, 받는 형식, 크기 상한만 받는다."""
    downloads, skipped, seen = [], [], set()
    for f in (items if isinstance(items, list) else [])[:MAX_BROWSER_FILES]:
        if not isinstance(f, dict):
            continue
        url = _same(f.get("url"), origin)
        if not url or url in seen:
            continue
        seen.add(url)
        given = _s(f.get("name"), 200)
        ext = _file_ext(url, _s(f.get("type"), 200)) or (
            Path(given).suffix.lower() if Path(given).suffix.lower() in FILE_TYPES else "")
        try:
            body = base64.b64decode(_s(f.get("data"), MAX_FILE_BYTES * 2), validate=True)
        except (binascii.Error, ValueError):
            skipped.append({"url": url, "reason": "파일 내용이 깨져 있음"})
            continue
        if not ext:
            skipped.append({"url": url, "reason": "받지 않는 형식"})
            continue
        name = (_folder_name(Path(given).stem)[:80] + ext if Path(given).suffix.lower() == ext
                else _file_name(url, _s(f.get("text"), 120), ext))
        downloads.append({"url": url, "name": name, "text": _s(f.get("text"), 120),
                          "page": _same(f.get("page"), origin), "body": body, "ctype": "", "ext": ext})
    return downloads, skipped


def _submit_result(source_id: int, result: dict) -> None:
    """테스트에서 인라인으로 바꾸는 큐 경계(브라우저 스캔)."""
    jobs.submit(_result_job, source_id, result)


def _result_job(source_id: int, result: dict) -> None:
    from ..db import SessionLocal  # noqa: PLC0415

    with SessionLocal() as session:
        row = session.get(InfoSource, source_id)
        if row is None:
            return
        try:
            _finish(session, row, result, [
                "사용자 브라우저에서 읽은 결과입니다(로그인한 세션, 화면 캡처 없음).",
                *result.pop("stage_notes", [])])
        except Exception as e:  # noqa: BLE001
            _fail(session, source_id, e, "browser")


def accept_browser_result(db: Session, row: InfoSource, payload) -> dict:
    """검사는 지금(틀리면 바로 400), 정리(LLM)는 큐에서 — 정리는 수십 초 걸린다."""
    _refuse_if_running(row)
    result = from_browser(row, payload)
    # 받은 파일은 지금 둔다 — 큐로 넘길 결과에 수십 MB의 본문을 싣지 않는다.
    got, missed = _download(result.pop("downloads"), {}, result["origin"])
    result["files"], result["stage_notes"] = _stage(row.id, got)
    result["skipped"] += missed
    row.status, row.error = "scanning", None
    row.scanned_at = datetime.now(timezone.utc)
    db.commit()
    _submit_result(row.id, result)
    return {"pages": len(result["pages"]), "menu": len(result["menu_dom"]), "files": len(result["files"])}


# ---------------------------------------------------------------- 저장(admin 결정)

def _env_path() -> Path:
    # 설정이 읽는 **그 파일** — 다른 자리를 고치면 고쳤는데 반영되지 않는다.
    # env_file=".env"는 작업 폴더 기준이다(서비스는 설치 폴더에서 뜬다).
    from ..config import Settings  # noqa: PLC0415

    return Path(str(Settings.model_config.get("env_file") or ".env")).resolve()


def create_store(name: str, folder: str) -> storage.Store:
    """새 저장소 — 폴더를 만들고 .env의 PAAS_DOC_ROOTS에 더한 뒤 설정을 다시 읽는다.

    재시작하지 않는다: 저장소 목록은 부를 때마다 설정에서 만들고(storage.stores), 설정은
    캐시만 비우면 다시 읽힌다. 주기 색인도 다음 차례에 새 저장소를 집는다.
    """
    if not storage._NAME_RE.match(name or ""):
        raise SourceError("저장소 이름은 소문자·숫자·하이픈(40자 이내)이어야 합니다.")
    if "PAAS_DOC_ROOTS" in os.environ:
        raise SourceError(
            "PAAS_DOC_ROOTS가 서비스 환경변수로 설정돼 있어 .env를 고쳐도 반영되지 않습니다 — "
            "환경변수에서 빼고 .env로 옮기거나, 기존 저장소를 고르세요.")
    path = Path((folder or "").strip().strip('"'))
    if not path.is_absolute():
        raise SourceError("폴더는 절대 경로여야 합니다(예: D:\\docs\\sources\\hr-portal).")
    path = path.resolve()
    current = storage.stores()
    if any(s.name == name for s in current):
        raise SourceError(f"이미 있는 저장소 이름입니다: {name}")
    for s in current:
        if path == s.root or path.is_relative_to(s.root) or s.root.is_relative_to(path):
            raise SourceError(f"기존 저장소({s.name}: {s.root})와 폴더가 겹칩니다 — 같은 문서가 "
                              "두 번 색인됩니다.")
    path.mkdir(parents=True, exist_ok=True)

    env = _env_path()
    before = env.read_text(encoding="utf-8") if env.exists() else ""
    value = get_settings().doc_roots.strip().strip(",")
    entry = f"{name}={path}"
    line = f"PAAS_DOC_ROOTS={value + ',' if value else ''}{entry}"
    pattern = re.compile(r"^\s*PAAS_DOC_ROOTS\s*=.*$", re.M)
    after = pattern.sub(lambda _: line, before, count=1) if pattern.search(before) \
        else before + ("" if not before or before.endswith("\n") else "\n") + line + "\n"
    if env.exists():
        shutil.copyfile(env, env.with_name(".env.bak"))
    env.write_text(after, encoding="utf-8")
    get_settings.cache_clear()
    try:
        made = storage.store(name)
    except storage.StorageError as e:
        made, error = None, str(e)
    else:
        error = "새 설정에서 저장소가 보이지 않습니다."
    if made is None:
        env.write_text(before, encoding="utf-8")   # 되돌린다 — 반쯤 바뀐 설정으로 남기지 않는다
        get_settings.cache_clear()
        raise SourceError(f".env를 고쳤지만 반영되지 않아 되돌렸습니다: {error}")
    return made


def _folder_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "-", text).strip("-.")[:60] or "source"


def _menu_md(items: list[dict], depth: int = 0) -> list[str]:
    lines = []
    for item in items:
        label = item["label"]
        lines.append("  " * depth + (f"- [{label}]({item['url']})" if item.get("url") else f"- {label}"))
        lines += _menu_md(item.get("children") or [], depth + 1)
    return lines


def _tag(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:6]


def _page_name(p: dict) -> str:
    # 같은 화면은 늘 같은 이름 — 스캔마다 순번을 붙이면 순서만 바뀌어도 전부 다른 파일이 된다.
    return f"pages/{_folder_name(p.get('title') or 'page')[:40]}-{_tag(p['url'])}.md"


def file_paths(row: InfoSource) -> list[tuple[dict, str]]:
    """받은 파일마다 저장소 안 자리 — {출처 폴더}/{유형 폴더}/{파일 이름}. 이름이 겹치면 주소로 가른다."""
    folder, used, out = _folder_name(row.name), set(), []
    for f in (row.scan or {}).get("files") or []:
        rel = f"{folder}/{CATEGORIES[f['category']][1]}/{f['name']}"
        if rel.lower() in used:
            stem, ext = rel.rsplit(".", 1)
            rel = f"{stem}-{_tag(f['url'])}.{ext}"
        used.add(rel.lower())
        out.append((f, rel))
    return out


def render(row: InfoSource, places: dict[str, str] | None = None) -> dict[str, str]:
    """저장할 문서들 — {상대경로: 마크다운}. 색인·온톨로지가 제목·표를 그대로 읽는다.

    places는 유형별로 고른 저장소 이름 — 내려받은 파일이 정리와 같은 저장소면 링크로, 다른
    저장소면 그 이름으로 적는다."""
    scan_data = row.scan or {}
    places = places or {}
    folder = _folder_name(row.name)
    head = [f"# {row.name}", "", f"- 출처: {row.url}", f"- 종류: {row.kind}",
            f"- 스캔: {row.scanned_at:%Y-%m-%d %H:%M}" if row.scanned_at else "- 스캔: -", ""]
    if scan_data.get("summary"):
        head += ["## 요약", "", scan_data["summary"], ""]
    if scan_data.get("keywords"):
        head += ["## 키워드", "", ", ".join(scan_data["keywords"]), ""]
    files: dict[str, str] = {}
    if row.kind == "web":
        head += ["## 메뉴 구성", "", *(_menu_md(scan_data.get("menu") or []) or ["(메뉴를 찾지 못함)"]), ""]
        head += ["## 조회 가능한 정보", "", "| 화면 | 종류 | 정보 | 조회 조건 |", "|---|---|---|---|"]
        for p in scan_data.get("pages") or []:
            title = (p.get("title") or p["url"]).replace("|", "/")
            name = _page_name(p)
            head.append(f"| [{title}]({name}) | {p.get('kind', '')} | "
                        f"{(p.get('info') or '').replace('|', '/')} | "
                        f"{', '.join(p.get('filters') or []).replace('|', '/')} |")
            body = [f"# {p.get('title') or p['url']}", "", f"- 주소: {p['url']}",
                    f"- 종류: {p.get('kind', '')}", f"- 출처: {row.name}", ""]
            if p.get("info"):
                body += ["## 조회 가능한 정보", "", p["info"], ""]
            if p.get("filters"):
                body += ["## 조회 조건", "", *(f"- {f}" for f in p["filters"]), ""]
            for table in p.get("tables") or []:
                body += ["## 표", "", "| " + " | ".join(c.replace("|", "/") for c in table) + " |",
                         "|" + "---|" * len(table), ""]
            if p.get("headings"):
                body += ["## 제목", "", *(f"- {h}" for h in p["headings"]), ""]
            if p.get("text"):
                body += ["## 본문 발췌", "", p["text"], ""]
            files[f"{folder}/{name}"] = "\n".join(body)
        placed = file_paths(row)
        if placed:
            head += ["", "## 내려받은 파일", "", "| 파일 | 종류 | 링크 글자 | 위치 |", "|---|---|---|---|"]
            for f, rel in placed:
                store = places.get(f["category"])
                where = (f"[{rel.split('/', 1)[1]}]({rel.split('/', 1)[1]})" if store == places.get("pages")
                         else f"저장소 {store}: {rel}" if store else f"저장 안 함 — {f['url']}")
                head.append(f"| {f['name'].replace('|', '/')} | {CATEGORIES[f['category']][0]} | "
                            f"{(f.get('text') or '').replace('|', '/')} | {where} |")
    elif row.kind == "api":
        if scan_data.get("openapi"):
            head += [f"- OpenAPI: {scan_data['openapi']}", ""]
        endpoints = scan_data.get("endpoints") or []
        head += ["## 조회 가능한 정보", "", "| 방식 | 경로 | 설명 | 조건 | 응답 필드 |", "|---|---|---|---|---|"]
        head += [f"| {e['method']} | `{e['path']}` | {e['summary'].replace('|', '/')} | "
                 f"{', '.join(e['params'])} | {', '.join(e['fields'])} |"
                 for e in endpoints if e["read_only"]]
        writes = [e for e in endpoints if not e["read_only"]]
        if writes:
            head += ["", "## 변경 API(조회 아님)", "", *(f"- {e['method']} `{e['path']}` {e['summary']}" for e in writes)]
        if scan_data.get("sample_shape") is not None:
            head += ["", "## 응답 모양", "", "```json",
                     json.dumps(scan_data["sample_shape"], ensure_ascii=False, indent=2), "```"]
    else:
        tools = scan_data.get("tools") or []
        head += ["## 조회 도구", "", "| 도구 | 설명 | 인자 |", "|---|---|---|"]
        head += [f"| `{t['name']}` | {t['description'].replace('|', '/')} | {', '.join(t['params'])} |"
                 for t in tools if t["read_only"]]
        writes = [t for t in tools if not t["read_only"]]
        if writes:
            head += ["", "## 변경 도구", "", *(f"- `{t['name']}` {t['description']}" for t in writes)]
    files[f"{folder}/index.md"] = "\n".join(head) + "\n"
    return files


def _legacy_saved(row: InfoSource) -> list[dict]:
    """저장 목록을 남기기 전에 저장한 출처 — 그때는 화면 문서에 순번(01-…)을 붙였다. 이름 규칙이
    바뀌었으니 그 문서들을 지난번 저장으로 쳐야 새 이름과 두 벌이 되지 않는다."""
    store = storage.store(row.target_store) if row.target_store else None
    if store is None:
        return []
    folder = _folder_name(row.name)
    return [{"category": "pages", "store": store.name, "path": f"{folder}/pages/{p.name}"}
            for p in sorted((store.root / folder / "pages").glob("[0-9][0-9]-*.md"))]


def _sha_of(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def save(db: Session, row: InfoSource, targets: dict) -> dict:
    """유형마다 고른 저장소에 쓴다. targets = {유형: {mode: existing|new|skip, store, path}}.

    같은 자리의 내용이 같으면 쓰지 않고(색인도 그대로), 바뀌었으면 덮어쓴다. 지난번에 이 출처가
    썼는데 이번에 없는 파일은 휴지통으로 옮긴다 — 저장 안 함으로 둔 유형은 건드리지 않는다.
    """
    if row.status not in ("scanned", "saved") or not row.scan:
        raise SourceError("스캔이 끝난 출처만 저장할 수 있습니다.")
    categories = categories_of(row.scan)
    unknown = set(targets) - set(categories)
    if unknown:
        raise SourceError(f"이 스캔에 없는 저장 유형입니다: {', '.join(sorted(unknown))}")
    chosen: dict[str, dict] = {}
    for category in categories:
        t = targets.get(category) or {"mode": "skip"}
        mode, name = str(t.get("mode") or ""), str(t.get("store") or "")
        if mode == "skip":
            continue
        if mode not in ("existing", "new"):
            raise SourceError("mode는 existing·new·skip 중 하나입니다.")
        chosen[category] = {"mode": mode, "store": name, "path": str(t.get("path") or "")}
    if not chosen:
        raise SourceError("저장할 유형을 하나 이상 고르세요.")

    # 쓰기 전에 다 확인한다 — 새 저장소를 만든 뒤에 다른 유형에서 막히면 빈 저장소만 남는다.
    plan: list[tuple[str, str, bytes]] = []   # (유형, 상대경로, 내용)
    for f, rel in file_paths(row):
        if f["category"] in chosen:
            staged = staged_file(row, f)
            if not staged.is_file():
                raise SourceError("받아 둔 파일이 없습니다 — 다시 스캔한 뒤 저장하세요.")
            plan.append((f["category"], rel, staged.read_bytes()))
    stores: dict[str, storage.Store] = {}
    for c, t in chosen.items():
        if t["mode"] == "existing" and t["store"] not in stores:
            found = storage.store(t["store"])
            if found is None or found.hidden:
                raise SourceError(f"저장소를 찾을 수 없습니다: {t['store']}")
            if found.read_only:
                raise SourceError(f"잠긴 저장소에는 쓸 수 없습니다: {found.name}")
            stores[found.name] = found
    created = []
    for c, t in chosen.items():
        if t["mode"] == "new" and t["store"] not in stores:
            stores[t["store"]] = create_store(t["store"], t["path"])
            created.append(t["store"])
    places = {c: t["store"] for c, t in chosen.items()}
    if "pages" in chosen:
        plan += [("pages", rel, text.encode("utf-8")) for rel, text in render(row, places).items()]

    written, same, saved = [], [], []
    for category, rel, data in plan:
        store = stores[places[category]]
        sha = hashlib.sha256(data).hexdigest()
        if _sha_of(storage.resolve(store.root, rel)) == sha:
            same.append(rel)
        else:
            written.append(storage.write_file(store, rel, data))
        saved.append({"category": category, "store": store.name, "path": rel, "sha": sha})
    keep = {(s["store"], s["path"]) for s in saved}
    removed, untouched = [], []
    for old in row.saved_files if row.saved_files is not None else _legacy_saved(row):
        # 저장 안 함으로 둔 유형만 그대로 둔다 — 이번 스캔에 그 유형이 아예 없으면 사이트에서 없어진 것이다.
        if old["category"] in categories and old["category"] not in chosen:
            untouched.append(old)
            continue
        if (old["store"], old["path"]) in keep:
            continue
        store = storage.store(old["store"])
        if store is None or store.read_only:
            continue
        try:
            storage.delete_file(store, old["path"])
            removed.append(f"{old['store']}:{old['path']}")
        except (FileNotFoundError, storage.StorageError):
            pass   # 사람이 이미 옮기거나 지웠다
    row.saved_files = saved + untouched
    row.status = "saved"
    row.target_store = places.get("pages") or next(iter(places.values()))
    row.saved_at = datetime.now(timezone.utc)
    db.commit()
    return {"stores": [{"store": s.name, "root": str(s.root), "created": s.name in created}
                       for s in stores.values()],
            "created": bool(created), "files": written, "same": same, "removed": removed}


def shot_path(row: InfoSource, n: int) -> Path | None:
    path = _shot_dir(row.id) / f"{n}.jpg"
    return path if path.is_file() else None


def forget(row: InfoSource) -> None:
    shutil.rmtree(_shot_dir(row.id), ignore_errors=True)
    shutil.rmtree(_file_dir(row.id), ignore_errors=True)
