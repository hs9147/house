# GPAX 계정 로그인으로 MCP 연결 (OAuth 2.1 + PKCE)

개인 MCP 토큰을 설정 파일에 붙여 넣는 대신, MCP 클라이언트(Claude Code·VS Code·Cursor…)가 브라우저에
**GPAX 로그인 → 승인** 화면을 열어 연결한다. 개인 토큰은 그대로 계속 쓸 수 있다(대체가 아니라 추가).

## 흐름 — Gitea SSO와 같은 길

| 단계 | 경로 | Gitea SSO와 같은 점 / 다른 점 |
|---|---|---|
| 401 + 안내 | MCP 주소 → `WWW-Authenticate: Bearer resource_metadata="…"` | 새로 추가 |
| 보호 자원 메타데이터 | `/paas/.well-known/oauth-protected-resource/api/v1/mcp/hub` | 새로 추가(RFC 9728) |
| 발급자 메타데이터 | `/paas/oauth/.well-known/oauth-authorization-server` | Gitea용(`/paas/.well-known/openid-configuration`)과 **따로** — 그쪽은 백채널(localhost)을 가리킨다 |
| 클라이언트 등록 | `POST /paas/oauth/register` | Gitea는 설정 파일의 기밀 클라이언트, 여기는 스스로 등록하는 공개 클라이언트(RFC 7591) |
| 로그인 | `/paas/oauth/authorize` → 콘솔 로그인(`#/login?next=`) | **같다** — `paas_session` 쿠키 |
| 승인 | 같은 주소의 승인 화면(누가·어디로 돌아가는지) | 새로 추가 — 공개 클라이언트라 사람이 확인한다 |
| 토큰 | `POST /paas/oauth/token` | 같은 서명 키. PKCE S256 필수, 액세스 1시간 + 갱신(쓸 때마다 바뀜), 승인은 90일 |

액세스 토큰은 **MCP 전용**이다. 발급자(`…/paas/oauth`)가 달라 콘솔·배포 API(`require_api_key`)는 받지 않고, 승인한
MCP 주소(aud) 아래에서만 통한다. 「MCP 연결설정 → 승인된 앱」에서 끊으면 남은 액세스 토큰도 다음 요청부터 막힌다.

## 운영에서 열리려면 (인프라 — 앱 밖)

실측(2026-11-26, gpax.lge.com):

| 경로 | 지금 | 필요 |
|---|---|---|
| `/.well-known/oauth-authorization-server/paas/oauth` | 200 HTML(GPAX 포털) | 이 앱으로 넘기거나 404 |
| `/.well-known/openid-configuration/paas/oauth` | 200 HTML(GPAX 포털) | 이 앱으로 넘기거나 404 |
| `/.well-known/oauth-protected-resource/paas/…` | 200 HTML(GPAX 포털) | 이 앱으로 넘기거나 404 |
| `/paas/…` 아래 | 앱이 받음 | 그대로 |
| MCP 401 응답 | `WWW-Authenticate: Negotiate`, `NTLM`(IIS Windows 인증) | 앱이 보낸 `Bearer …`만 |

1. **도메인 뿌리의 `/.well-known/…/paas/…`** — MCP 클라이언트는 발급자 메타데이터를 도메인 뿌리에서 먼저 찾고,
   **200 HTML을 받으면 JSON으로 읽다 실패해 거기서 멈춘다**(404면 다음 후보 `/paas/oauth/.well-known/…`로 넘어간다).
   포털(뿌리 사이트)의 SPA 대체 규칙보다 앞에 다음 규칙을 둔다:

   ```xml
   <rule name="paas-oauth-well-known" stopProcessing="true">
     <match url="^\.well-known/(oauth-protected-resource|oauth-authorization-server|openid-configuration)/paas/(.*)$" />
     <action type="Rewrite" url="http://localhost:7000/.well-known/{R:1}/paas/{R:2}" />
   </rule>
   ```

   앱은 이 뿌리 경로들을 접두사 없이 받는다(`app/api/mcp_oauth.root_router`). 넘기는 대신 404만 내도 동작은 한다.

2. **`/paas`의 Windows 인증 끄기** — 앱이 401을 스스로 판정하므로 IIS의 `Negotiate`/`NTLM`은 쓰임이 없다. 둘이
   섞이면 클라이언트가 `WWW-Authenticate`의 첫 값만 보고 Bearer 안내를 놓칠 수 있다. `/paas` 애플리케이션은
   익명 인증만 켠다.

3. **HTTPS** — MCP 권한 규약은 발급자 주소를 HTTPS로 요구한다. 클라이언트에 따라 http를 거부할 수 있다(확인 필요).
   `PAAS_PLATFORM_PUBLIC_URL`을 https로 바꾸면 메타데이터·승인 화면 주소가 함께 바뀐다.

## 설정

| 환경변수 | 기본 | 뜻 |
|---|---|---|
| `PAAS_MCP_OAUTH_ENABLED` | `true` | 끄면 위 경로가 모두 404, 401에 Bearer 안내도 싣지 않는다 |
| `PAAS_OIDC_PROVIDER_ENABLED` | — | 필요(같은 서명 키·로그인을 쓴다) |
| `PAAS_PLATFORM_PUBLIC_URL` | — | 필요(메타데이터·aud가 이 주소로 만들어진다) |

## 확인 순서

```bash
curl -i -X POST http://gpax.lge.com/paas/api/v1/mcp/hub -H 'Content-Type: application/json' -d '{}'
#   → 401, WWW-Authenticate: Bearer resource_metadata="…/paas/.well-known/oauth-protected-resource/api/v1/mcp/hub"
curl http://gpax.lge.com/.well-known/oauth-authorization-server/paas/oauth   # → JSON(issuer=…/paas/oauth) 또는 404
claude mcp add --transport http --scope user paas http://gpax.lge.com/paas/api/v1/mcp/hub
#   → Claude Code에서 /mcp → paas → Authenticate → 브라우저에 로그인·승인
```
