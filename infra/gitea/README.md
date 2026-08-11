# 사내 Git 서버 (Gitea)

GitHub을 대신하는 self-host Git 서버. MIT 라이선스로 상용·사내 사용에 제약이 없다
(근거: [설계 검토 문서 10.2절](../../../docs/cloud-platform-paas-design-review.md)).
소스가 사외 SaaS로 나가지 않아야 하는 기업용 요건에 맞춰 여기 구성한다.

플랫폼(FastAPI 컨트롤 플레인) 코드는 이미 GitHub과 Gitea 웹훅 서명을 둘 다
인식하므로(`X-Hub-Signature-256` / `X-Gitea-Signature`, `app/security.py`), 애플리케이션
변경 없이 아래 배포만으로 연동된다.

## 0. 전용 서버에 Gitea 설치하기 (1차, Docker Compose 기준)

플랫폼과 물리적으로 분리된 **Gitea 전용 서버**를 새로 준비하는 경우의 처음부터 끝까지
순서다. 플랫폼과 같은 서버에 얹는다면 [deployment-guide.md 3.1절](../../../docs/deployment-guide.md)에서
Docker·Caddy가 이미 설치됐을 테니 아래 1)·2)는 건너뛰고 3)부터 이어간다
(2차/K8s로 배포한다면 클러스터가 이미 있다고 가정하므로 이 절 전체가 불필요 — 아래
"2차 배포" 참고).

**요구 사양**: Gitea는 가볍다 — 1 vCPU / 1~2GB RAM / 저장소 20GB+(리포 규모에 따라 조정)면
소규모 팀 기준 충분하다. OS는 Ubuntu 22.04 LTS+ 가정(다른 배포판은 패키지 관리자만 바꾸면 된다).
**Windows 서버라면 아래 ["Windows에서 설치"](#windows에서-설치) 절로 건너뛴다.**

**1) 방화벽 — 필요한 포트만 연다**

```bash
# 실행 위치: 새로 준비한 서버 셸 (sudo 권한)
sudo ufw allow 22/tcp      # 관리자 SSH 접속
sudo ufw allow 80,443/tcp  # Caddy(HTTP→HTTPS 자동 리다이렉트, TLS 자동 발급)
sudo ufw allow 2222/tcp    # git SSH clone/push — 안 쓸 거면 생략 (아래 "SSH를 못 쓰는 경우")
sudo ufw enable
```

**2) Docker Engine + Caddy 설치**

```bash
# Docker Engine (docker compose v2 플러그인 포함)
curl -fsSL https://get.docker.com | sh
docker compose version   # 설치 확인

# Caddy — 공식 APT 저장소 등록 후 설치 (도메인·TLS 자동 발급용)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

**3) DNS**

`git.example.com → 서버 IP` A 레코드 하나만 있으면 된다(레코드 전파에는 보통 수 분~수십 분
걸릴 수 있다 — 다음 단계로 넘어가기 전에 `dig git.example.com`으로 확인해도 좋다).

이후 절차([어느 걸 쓸지](#어느-걸-쓸지) → [1차 배포](#1차-배포-docker-compose))는
플랫폼과 같은 서버든 전용 서버든 동일하다.

## Windows에서 설치

Gitea 전용 서버가 Windows라면 두 가지 방법이 있고, **A(WSL2)를 권장**한다 — 위 Linux
절차를 거의 그대로 쓸 수 있고 Docker Desktop 라이선스 없이도 가능하다(플랫폼 자체를
Windows에서 돌릴 때와 동일한 권장 — [deployment-guide.md 3.6절](../../../docs/deployment-guide.md)).

### A. WSL2 안에서 실행 (권장)

```powershell
# 실행 위치: PowerShell (관리자)
wsl --install -d Ubuntu     # 최초 1회, 재부팅 필요
```

이후 WSL Ubuntu 터미널에서 위 "0. 전용 서버에 Gitea 설치하기"의 1)~3)을 그대로 따른다.
방화벽은 Windows 방화벽에서 열어야 한다(WSL은 호스트 방화벽을 통과한다):

```powershell
New-NetFirewallRule -DisplayName "Gitea HTTP/HTTPS" -Direction Inbound -LocalPort 80,443 -Protocol TCP -Action Allow
New-NetFirewallRule -DisplayName "Gitea SSH" -Direction Inbound -LocalPort 2222 -Protocol TCP -Action Allow
```

Docker는 WSL Ubuntu 안에 Docker Engine을 직접 설치(`curl -fsSL https://get.docker.com | sh`,
무료)하거나 Docker Desktop(WSL Integration, 기업 규모에 따라 유료)을 쓴다.

### B. 네이티브 Windows — Docker 없이 (Gitea 바이너리 + NSSM)

Docker 자체를 쓰지 않으려면 Gitea 공식 Windows 바이너리를 nssm(플랫폼의
`windows_service` 런타임과 동일한 도구)으로 Windows Service에 등록해 직접 실행한다.

```powershell
# 실행 위치: PowerShell (관리자)
New-NetFirewallRule -DisplayName "Gitea HTTP/HTTPS" -Direction Inbound -LocalPort 80,443 -Protocol TCP -Action Allow
New-NetFirewallRule -DisplayName "Gitea SSH" -Direction Inbound -LocalPort 2222 -Protocol TCP -Action Allow

$version = "1.22.0"   # https://dl.gitea.com/gitea/ 에서 최신 버전 확인 후 교체
mkdir C:\gitea; cd C:\gitea
Invoke-WebRequest "https://dl.gitea.com/gitea/$version/gitea-$version-windows-4.0-amd64.exe" -OutFile gitea.exe

choco install nssm -y   # 또는 https://nssm.cc 에서 수동 다운로드

nssm install gitea C:\gitea\gitea.exe web
nssm set gitea AppDirectory C:\gitea
nssm set gitea AppEnvironmentExtra "GITEA__server__DOMAIN=git.example.com`nGITEA__server__ROOT_URL=https://git.example.com/`nGITEA__server__SSH_LISTEN_PORT=2222`nGITEA__server__SSH_PORT=2222`nGITEA__service__DISABLE_REGISTRATION=true`nGITEA__database__DB_TYPE=sqlite3"
nssm start gitea
```

> 설정 항목이 늘어나면 위 `AppEnvironmentExtra` 한 줄은 관리하기 어렵다(백틱 `n`으로
> 구분되는 데다 값 하나 고치려면 전체를 다시 써야 한다). **`C:\gitea\custom\conf\app.ini`에
> 직접 적는 편이 낫다** — [`app.ini.example`](app.ini.example)에 이 플랫폼과 붙이는 데
> 필요한 항목이 주석과 함께 모여 있다. 파일을 고친 뒤 `nssm restart gitea`.

Caddy(리버스프록시, TLS 자동 발급)도 같은 방식으로 설치·등록한다:

```powershell
winget install CaddyServer.Caddy
# Caddyfile에 아래 한 줄 작성 후:
#   git.example.com {
#       reverse_proxy 127.0.0.1:3000
#   }
nssm install caddy "C:\Program Files\Caddy\caddy.exe" run --config C:\caddy\Caddyfile
nssm start caddy
```

DB를 SQLite에서 Postgres로 바꾸거나 백업하는 절차는 이후 [최초 설정](#최초-설정-공통)·
[백업](#백업) 절과 동일하다(경로만 Windows 표기로 바꾼다 — 예: `C:\gitea\data\gitea\gitea.db`).

## 어느 걸 쓸지

| | 1차(중소규모) | 2차(기업용) |
| --- | --- | --- |
| 파일 | `docker-compose.yml` + `Caddyfile.example` | `k8s/*.yaml` |
| 대상 | 플랫폼과 같은 서버 또는 전용 서버(위 0절) | 플랫폼 K8s 클러스터(6.2절 ingress·cert-manager 재사용) |
| DB | 내장 SQLite | 내장 SQLite → 팀 규모가 크면 Postgres로 교체(주석 참고) |

> **설정값은 [`app.ini.example`](app.ini.example)에 한 번에 모아 뒀다.** 이 문서 곳곳에
> `GITEA__섹션__KEY=값` 형태로 나오는 것들과 같은 내용이고, 이름이 1:1로 대응한다
> (`[server] ROOT_URL` ↔ `GITEA__server__ROOT_URL`). 컨테이너면 compose의 환경변수로,
> 네이티브 설치(nssm 등)면 app.ini에 직접 적는 편이 다루기 쉽다 — 어느 쪽이든 결과는 같다.

## 1차 배포 (Docker Compose)

전용 서버라면 이 `chofam` 리포를 그 서버에도 clone하거나(가장 간단) `infra/gitea/`
디렉터리만 `scp`로 옮겨도 된다 — 이 폴더는 플랫폼 코드와 독립적으로 동작한다.

```bash
# 실행 위치: Gitea를 둘 서버 — infra/gitea 디렉터리
GITEA_DOMAIN=git.example.com docker compose -f docker-compose.yml up -d
```

메인 Caddyfile에 `Caddyfile.example` 내용을 `import`하거나 그대로 복사한다
(전용 서버라면 `/etc/caddy/Caddyfile`에 직접 추가).

## 2차 배포 (Kubernetes)

```bash
kubectl apply -f k8s/namespace.yaml -f k8s/pvc.yaml -f k8s/deployment.yaml \
  -f k8s/service.yaml -f k8s/ingress.yaml
```

`deployment.yaml`·`ingress.yaml`의 `git.example.com`을 실제 도메인으로 바꾸고,
DB를 Postgres로 교체하려면 deployment.yaml 주석의 `GITEA__database__*` 블록을 활성화한 뒤
비밀번호는 플랫폼의 시크릿 관행과 동일하게 `envFrom: secretRef`로 주입할 것
(평문 env 금지 — [15절](../../../docs/cloud-platform-paas-design-review.md) 참고).

## 서브패스(단일 포트)로 노출하는 경우

외부에 도메인을 추가로 못 받아서 Gitea를 플랫폼과 **같은 도메인의 서브패스**로
내보내야 할 때(예: `https://paas.example.com/gitea/`, 위 기본 절차의 별도 도메인
`git.example.com` 대신)는 아래 두 가지를 반드시 함께 맞춰야 한다 — Gitea 공식
문서도 서브패스 배포는 "권장하지 않지만" 이 두 조건을 맞추면 동작한다고 명시한다
([Gitea Reverse Proxies 문서](https://docs.gitea.com/administration/reverse-proxies/)).

1. **`ROOT_URL`에 서브패스까지 포함** — Gitea 자신이 로그인 리다이렉트·정적 자산·
   OAuth2 콜백 URL을 이 값 기준으로 만든다. 도메인만 넣고 서브패스를 빼면(기존
   기본 절차처럼 `https://paas.example.com/`만 넣으면) 로그인 후 리다이렉트가
   서브패스를 잃어버려 깨진다 — "gitea 자체 로그인 인증 이슈"의 실제 원인이 대부분 이것이다.
   ```
   GITEA__server__ROOT_URL=https://paas.example.com/gitea/
   ```
2. **리버스 프록시가 `/gitea` 접두어를 벗기지 않고 그대로 전달** — Gitea는 요청
   경로에 `/gitea`가 이미 붙어 있는 채로 와야 한다(1의 ROOT_URL과 대응). Caddy는
   `handle_path`(접두어를 벗김)가 아니라 `handle`(벗기지 않고 그대로 전달)을 써야 한다:
   ```caddy
   paas.example.com {
       handle /gitea/* {
           reverse_proxy 127.0.0.1:3000
       }
       # ... 플랫폼 자신의 handle 블록들(콘솔·/apps/... 등)은 그대로 유지
   }
   ```
   IIS/ARR(`PAAS_PROXY_BACKEND=iis`)를 쓴다면 URL Rewrite 규칙에서도 경로를
   재작성(rewrite)만 하고 **벗겨내지(strip) 않아야** 동일한 효과를 낸다 —
   [deployment-guide.md 3.6절](../../../docs/deployment-guide.md)의 ARR 설정 참고.

   네이티브 Windows(위 B절)라면 nssm에 ROOT_URL만 서브패스 버전으로 바꿔 넣는다:
   ```powershell
   nssm set gitea AppEnvironmentExtra "GITEA__server__DOMAIN=paas.example.com`nGITEA__server__ROOT_URL=https://paas.example.com/gitea/`nGITEA__server__SSH_LISTEN_PORT=2222`nGITEA__server__SSH_PORT=2222`nGITEA__service__DISABLE_REGISTRATION=true`nGITEA__database__DB_TYPE=sqlite3"
   nssm restart gitea
   ```

## SSH를 못 쓰는 경우 (git은 http로만)

git SSH 포트(22/2222)를 열 수 없는 환경이라도 **플랫폼 동작에는 아무 영향이 없다.**
플랫폼은 clone/fetch/push를 전부 http로 하고 인증은 토큰을 헤더로 넣어 처리한다
(`services/git_auth.py`가 `http.extraHeader`로 주입 — git_url 자체엔 토큰을 심지 않는다).
조직·업로드로 만드는 리포의 주소도 Gitea API가 주는 **http clone_url**을 쓴다(ssh_url이 아니다).

사람이 직접 쓰는 경로만 정리해 주면 된다:

```
GITEA__server__DISABLE_SSH=true
```

이걸 켜야 Gitea UI가 SSH clone 주소를 **안 보여준다.** 안 끄면 리포 화면에 SSH 주소가
계속 함께 뜨고, 사용자가 그걸 복사해 쓰다 연결이 안 돼서 헤맨다.

- docker-compose: `environment:`에 위 줄을 추가하고 `ports:`의 `2222:22` 줄을 지운다.
- 네이티브 Windows(nssm): `AppEnvironmentExtra`에 `GITEA__server__DISABLE_SSH=true`를
  이어붙이고 `nssm restart gitea`. 방화벽의 2222 규칙도 지운다.
- K8s: `k8s/service.yaml`의 ssh 포트 항목과 `deployment.yaml`의 `GITEA__server__SSH_PORT`를 지운다.

### 개발자 PC에서 clone/push (VS Code 포함)

**http git에는 "로그인 세션"이 없다.** 브라우저로 Gitea(또는 SSO)에 로그인해 둬도 git은
그 세션을 쓰지 않는다 — push할 때마다 자격 증명을 다시 보낸다. 그래서 매번 물어보는
것처럼 보인다. 저장은 git credential helper가 담당한다.

**SSO로 만들어진 계정은 비밀번호가 아예 없다.** OIDC 자동 등록(`ENABLE_AUTO_REGISTRATION`)
으로 생긴 계정에는 로컬 비밀번호가 설정돼 있지 않다. 그래서 git이 물어볼 때 평소 쓰는
**SSO 비밀번호를 넣으면 반드시 실패한다** — 이게 "로그인이 유지되지 않는다"로 보이는
가장 흔한 원인이다.

#### 권장 — 브라우저 SSO로 인증 (토큰 수동 발급 없음)

Git Credential Manager(GCM)는 Gitea를 자동 인식해 **브라우저 OAuth 로그인**으로 처리한다.
`git push` 시 브라우저가 열리고, 이미 SSO 세션이 있으면 클릭 한 번으로 끝난다 — 사용자가
토큰을 만들 필요도, 어디에 붙여넣을지 고민할 필요도 없다. 발급된 토큰은 GCM이 OS 자격
증명 저장소에 넣어 두므로 다음 push부터는 아무것도 묻지 않는다.

서버 쪽에 등록할 것이 없다. Gitea 1.21+는 GCM용 OAuth 애플리케이션을 **기본으로 미리
등록해 둔다**(`[oauth2] DEFAULT_APPLICATIONS`의 `git-credential-manager`, 이 리포의
compose는 1.22를 쓴다). `[oauth2] ENABLED`도 기본값이 true여야 한다 — 끄면 GCM이
OAuth를 못 찾고 비밀번호를 묻는 방식으로 되돌아간다.

개발자 PC에서 한 번만:
```bash
git config --global credential.helper manager     # 구버전 Git이면 manager-core
# Windows: Git for Windows에 GCM이 함께 설치돼 있다
# Linux/macOS: 별도 설치 필요 (https://github.com/git-ecosystem/git-credential-manager)
```
이후 평소처럼 clone/push하면 브라우저가 뜬다:
```bash
git clone https://git.example.com/org/repo.git
```

> 콘솔의 **프로젝트**·**에이전트 기획** 화면에 있는 `VS Code` 버튼을 쓰면 주소를 직접
> 복사하지 않아도 된다 — VS Code가 열리며 clone 위치를 묻는다.

**Gitea를 서브패스로 뒀다면 자동 인식이 안 된다.** GCM이 쓰는 Gitea 기본 엔드포인트는
도메인 루트 기준(`/login/oauth/authorize`)이라, `https://host/gitea/`처럼 하위 경로에
있으면 엉뚱한 주소를 부른다([GCM #1650](https://github.com/git-ecosystem/git-credential-manager/issues/1650)).
이 경우 엔드포인트를 직접 지정한다(클라이언트 시크릿은 없다 — 공개 클라이언트다):
```bash
git config --global credential.https://host.example.com.oauthClientId e90ee53c-94e2-48ac-9358-a874fb9e0662
git config --global credential.https://host.example.com.oauthAuthorizeEndpoint /gitea/login/oauth/authorize
git config --global credential.https://host.example.com.oauthTokenEndpoint    /gitea/login/oauth/access_token
git config --global credential.https://host.example.com.oauthRedirectUri      http://127.0.0.1
```
> 토큰 엔드포인트는 `/login/oauth/token`이 **아니라** `/login/oauth/access_token`이다.
> 여기를 틀리면 증상이 헷갈린다 — authorize는 맞는 주소라 **로그인·승인까지 정상으로
> 보이고**, 그 뒤 GCM이 토큰을 받으러 갈 때 404가 나면서 `git push`가 응답을 기다리며
> 멈춘다. 아래 "브라우저 승인까지 됐는데 push가 멈춘다" 참고.
>
> `oauthRedirectUri`는 `127.0.0.1`을 쓴다(`localhost` 아님). Gitea는 루프백 주소에 한해
> 포트를 가리지 않는데, 그 처리가 `127.0.0.1`에만 적용된다 — GCM은 매번 임의 포트로
> 대기하므로 이게 맞아야 콜백이 돌아온다.

#### 승인했는데 `127.0.0.1`로 넘어가지 않는다 (code·state까지만 가고 끊김)

이 흐름에는 `code`·`state`를 나르는 구간이 **두 개** 있다. 어디서 끊겼는지는
**브라우저가 멈춘 주소**에 그대로 드러나므로 그것부터 본다.

```
① 플랫폼(IdP) ──code,state──▶ Gitea 콜백        (Gitea에 로그인하는 단계)
② Gitea(IdP)  ──code,state──▶ 127.0.0.1 (개발자 PC)  (git 자격 증명을 받는 단계)
```

| 멈춘 주소 | 끊긴 구간 | 원인 |
| --- | --- | --- |
| `…/gitea/user/oauth2/paas/callback?code=…&state=…` | ① 끝 | Gitea가 그 code를 플랫폼에 교환하러 갔다가 실패 — **백채널** |
| 공개 도메인인데 `?code=…&state=…`가 붙어 있음 | ② 중간 | 리버스 프록시가 응답의 `Location`을 고쳐 씀 — **ARR** |
| Gitea 화면에 오류(`unregistered redirect uri` 등) | ② 시작 전 | redirect URI 불일치 (`localhost` 사용 등) |
| `http://127.0.0.1:<포트>/…`까지 갔는데 연결 거부 | ② 도착 후 | 브라우저와 GCM이 서로 다른 머신 |

**① 백채널** — 플랫폼 콘솔 로그에 두 줄이 한 쌍으로 찍힌다.
```
[paas] OIDC authorize → code 발급: client=gitea user=... redirect_uri=...
[paas] OIDC token 교환 성공: client=gitea user=...
```
앞줄만 있고 뒷줄이 없으면 **Gitea가 플랫폼 토큰 엔드포인트에 아예 닿지 못한 것**이다.
Gitea가 도는 머신에서 직접 찔러 본다(405/400이면 닿는 것, 연결 거부·타임아웃이면 아니다):
```powershell
curl.exe -s -o NUL -w "%{http_code}`n" -X POST http://localhost:7000/paas/oauth2/token
```
닿지 않으면 `PAAS_OIDC_PROVIDER_BACKCHANNEL_URL`이 **Gitea 머신 기준** 주소가 아니다 —
Gitea가 컨테이너면 그 안의 `localhost`는 컨테이너 자신이다(`host.docker.internal` 등).
`교환 실패` 줄이 찍혔다면 사유가 그 줄에 함께 나온다.

**② ARR의 `Location` 재작성** — "127.0.0.1로 안 넘어간다"의 가장 흔한 원인이다.
Gitea는 `302 Location: http://127.0.0.1:<포트>/?code=…&state=…`를 **응답으로만** 내려보내는데,
ARR의 *Reverse rewrite host in response headers*가 켜져 있으면 백엔드 호스트와 같은 이름을
공개 도메인으로 바꿔치기한다. **백엔드를 `127.0.0.1:3000`으로 잡아 두면 GCM의 콜백 호스트와
이름이 겹쳐서**, 개발자 PC로 가야 할 리다이렉트가 서버로 되돌아온다 — code·state는 그대로
붙어 있으니 "전달은 됐는데 목적지가 다르다"로 보인다.

확인 — 프록시를 통과한 것과 Gitea를 직접 부른 것의 `Location`을 비교한다:
```powershell
$q = "client_id=e90ee53c-94e2-48ac-9358-a874fb9e0662&redirect_uri=http://127.0.0.1:5000/&response_type=code&state=t"
curl.exe -s -D - -o NUL "https://paas.example.com/gitea/login/oauth/authorize?$q"   # 프록시 통과
curl.exe -s -D - -o NUL "http://localhost:3000/login/oauth/authorize?$q"            # Gitea 직접
```
직접 호출의 `Location`에는 `127.0.0.1`이 남아 있는데 프록시 쪽만 공개 도메인으로 바뀌었다면
확정이다. 둘 중 하나로 푼다:

1. **백엔드 주소를 `127.0.0.1` 대신 `localhost`(또는 머신 이름)로 바꾼다** — 이름이 안 겹치니
   콜백은 건드려지지 않는다. 영향 범위가 가장 좁다.
2. 역방향 재작성을 끈다 — Gitea는 이미 `ROOT_URL` 기준의 공개 주소를 스스로 만들어 내보내므로
   이 기능이 할 일이 없다(서버 단위 설정이다):
   ```powershell
   %windir%\system32\inetsrv\appcmd.exe set config -section:system.webServer/proxy `
     /reverseRewriteHostInResponseHeaders:"False" /commit:apphost
   ```
URL Rewrite의 **아웃바운드 규칙**에서 `Location`을 손대고 있다면 거기에도 루프백 제외 조건을
넣는다(`{RESPONSE_Location}`이 `^http://127\.0\.0\.1`이면 건너뛰기). Caddy는 `Location`을
고치지 않으므로 이 문제가 없다.

**③ redirect URI 불일치** — 위 [redirect URI 정하기](#redirect-uri-정하기)의 2번. `localhost`가
아니라 `127.0.0.1`이어야 한다.

**④ 브라우저와 GCM이 다른 머신** — `127.0.0.1`은 **브라우저가 도는 PC**를 가리킨다. SSH 원격
개발·WSL·컨테이너 안에서 `git push`를 하면 GCM은 그쪽에서 대기하는데 브라우저는 내 PC에서
열려, 리다이렉트가 엉뚱한 곳의 루프백으로 간다. 같은 머신에서 하거나
[액세스 토큰 방식](#대안--액세스-토큰-gcm을-못-쓰는-환경)을 쓴다.

#### 브라우저 승인까지 됐는데 push가 멈춘다

로그인·승인이 끝났는데 VS Code/터미널이 계속 대기한다면, 인가 코드를 토큰으로
바꾸는 마지막 단계에서 막힌 것이다. 승인 화면이 떴다는 건 authorize 주소는 맞다는
뜻이므로 **토큰 엔드포인트와 콜백**부터 본다.

1. **토큰 엔드포인트 오타** — 위에서 말한 `access_token`. 지금 값부터 확인한다:
   ```bash
   git config --global --get-regexp 'credential\..*oauth'
   ```
2. **직접 눌러 본다** — 404면 경로가 틀린 것이다(405 Method Not Allowed면 경로는 맞다):
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST https://host.example.com/gitea/login/oauth/access_token
   ```
3. **GCM 로그로 확인** — 어느 URL에서 멈추는지 그대로 찍힌다:
   ```bash
   GCM_TRACE=1 git push        # Windows PowerShell: $env:GCM_TRACE=1; git push
   ```
4. 그래도 안 되면 위 `credential.*oauth*` 값을 모두 지우고
   [액세스 토큰 방식](#대안--액세스-토큰-gcm을-못-쓰는-환경)으로 돌아간다 — 서브패스
   자동 인식은 GCM의 미해결 이슈라 환경에 따라 걸릴 수 있다.
   ```bash
   git config --global --unset-all credential.https://host.example.com.oauthClientId
   git config --global --unset-all credential.https://host.example.com.oauthAuthorizeEndpoint
   git config --global --unset-all credential.https://host.example.com.oauthTokenEndpoint
   git config --global --unset-all credential.https://host.example.com.oauthRedirectUri
   ```

#### 대안 — 액세스 토큰 (GCM을 못 쓰는 환경)

CI 러너나 GCM 설치가 불가능한 PC에서는 개인 액세스 토큰을 쓴다. 프로필 → Settings →
Applications → Generate New Token, 스코프는 최소 `write:repository`.
```bash
git clone https://git.example.com/org/repo.git
#   Username: <Gitea 사용자명>
#   Password: <발급한 토큰>   ← SSO 비밀번호가 아니다
```

> **토큰을 URL에 넣지 말 것.** `https://user:token@host/...` 형태로 clone하면 토큰이
> `.git/config`에 평문으로 남아 리포를 압축해 보내거나 백업할 때 그대로 새어 나간다.
> 이미 그렇게 받았다면 `git remote set-url origin https://git.example.com/org/repo.git`로
> 정리한 뒤 위 방식으로 다시 인증한다.

**이미 틀린 자격 증명이 캐시된 경우** — 한 번 잘못 입력하면 helper가 그걸 저장해 두고
계속 재사용하므로, 토큰을 새로 발급해도 계속 실패한다. 저장된 항목을 먼저 지운다:
```powershell
# Windows: 제어판 → 자격 증명 관리자 → Windows 자격 증명 →
#          "git:http://git.example.com" 항목 제거
# 또는 명령으로:
cmdkey /list | findstr git
```
```bash
# 공통 — helper에게 직접 지우게 한다
printf 'protocol=http\nhost=git.example.com\n\n' | git credential reject
```

> VS Code는 자체 자격 증명 저장소를 쓰지 않고 위 git credential helper를 그대로
> 사용한다. 그래서 터미널에서 `git push`가 되면 VS Code에서도 된다 — 문제가 생기면
> **터미널에서 먼저 확인**하는 편이 원인을 가리기 쉽다.

## 최초 설정 (공통)

1. `https://git.example.com`(또는 위 서브패스 주소) 접속 → 설치 마법사에서 관리자 계정 생성
2. Site Administration → Configuration → **"Enable registration"이 꺼져 있는지 확인**
   (compose/K8s 모두 `DISABLE_REGISTRATION=true` 기본값 — 계정은 관리자가 초대)
3. **SSO 연동 — 플랫폼 계정으로 Gitea 로그인** (아래 A 권장)

### A. 플랫폼 자신을 IdP로 (권장 — 별도 SSO 서버 불필요)

플랫폼이 최소 OIDC Provider를 내장하고 있다(`app/services/oidc_provider.py`). 켜면
플랫폼의 **로그인 계정(UserAccount) 그 자체**가 Gitea 로그인 ID가 된다 — Keycloak 같은
별도 IdP를 세우고 계정을 두 곳에 유지할 필요가 없다. 콘솔에 로그인해 둔 브라우저로
Gitea를 열면 로그인 화면 없이 그대로 통과한다(세션 쿠키를 재사용).

**a) 클라이언트 시크릿을 하나 만든다** (아무 난수나 — 이 값을 양쪽에 똑같이 넣는다):
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

**b) 플랫폼 `.env`에 Provider를 켠다**:
```bash
PAAS_OIDC_PROVIDER_ENABLED=true
# 플랫폼 자신의 공개 주소 — 발급하는 토큰의 iss와 디스커버리 문서의 기준이 된다.
PAAS_PLATFORM_PUBLIC_URL=https://paas.example.com
# redirect_uris는 Gitea의 콜백 주소 — 아래 "redirect URI 정하기" 참고.
PAAS_OIDC_PROVIDER_CLIENTS={"gitea":{"secret":"<a에서 만든 값>","redirect_uris":["https://paas.example.com/gitea/user/oauth2/paas/callback"]}}
# 플랫폼 자신이 발급한 토큰을 플랫폼 API에서도 받으려면(선택) issuer를 자기 자신으로.
# 이 경우 JWKS는 로컬 개인키로 바로 검증하므로 PAAS_OIDC_JWKS_URL은 설정하지 않는다.
PAAS_OIDC_ISSUER=https://paas.example.com/paas
```

**c) Gitea에 OAuth2 소스로 등록** — 디스커버리 URL이 플랫폼 자신을 가리킨다:
```bash
gitea admin auth add-oauth \
  --name paas --provider openidConnect \
  --key gitea --secret "<a에서 만든 값>" \
  --auto-discover-url "https://paas.example.com/paas/.well-known/openid-configuration"
```

##### redirect URI 정하기

이 연동에는 **서로 다른 redirect URI가 두 개** 나온다. 이름이 같아 헷갈리기 쉬운데
쓰이는 곳도 값도 다르다.

| # | 어디에 적나 | 무엇의 콜백인가 | 값 |
| --- | --- | --- | --- |
| 1 | 플랫폼 `.env`의 `PAAS_OIDC_PROVIDER_CLIENTS` → `redirect_uris` | 플랫폼(IdP) → **Gitea**로 돌아가는 주소 | `<Gitea 외부주소>/user/oauth2/<이름>/callback` |
| 2 | (건드릴 일 없음) Gitea의 OAuth2 **애플리케이션** | Gitea(IdP) → **git 클라이언트**로 돌아가는 주소 | `http://127.0.0.1` — **개발자 PC**의 루프백. Gitea가 미리 등록해 둔다 |

**1번 만드는 법.** 세 조각을 이어 붙인다:

```
  https://paas.example.com/gitea      ← Gitea의 app.ini [server] ROOT_URL (끝 슬래시 빼고)
+ /user/oauth2/                       ← Gitea 고정 경로
+ paas                                ← gitea admin auth add-oauth 의 --name 값
+ /callback
────────────────────────────────────────────────────────────────────────
= https://paas.example.com/gitea/user/oauth2/paas/callback
```

- **`--name`과 경로의 이름이 반드시 같아야 한다.** `--name keycloak`으로 등록했으면
  경로도 `/user/oauth2/keycloak/callback`이다. 이름을 바꾸면 이 URI도 같이 바꿔야 한다.
- **`ROOT_URL`과 스킴·호스트·서브패스가 모두 같아야 한다.** 서브패스 배포인데
  `https://paas.example.com/user/oauth2/paas/callback`처럼 `/gitea`를 빠뜨리는 실수가 잦다.
- 플랫폼은 **등록된 값과 정확히 일치할 때만** 리다이렉트한다(부분 일치·와일드카드 없음).
  일치하지 않으면 로그인 화면으로 가기 전에 `redirect_uri not registered`로 끝난다 —
  신뢰하지 않은 주소로 사용자를 보내지 않기 위해서다.

**2번은 손대지 않는다.** Git Credential Manager가 브라우저 로그인을 할 때 쓰는
콜백으로, Gitea 1.21+가 `[oauth2] DEFAULT_APPLICATIONS`로 미리 등록해 둔다
(`http://127.0.0.1`, 포트는 루프백이라 가리지 않는다). GCM 쪽 설정을 직접 적을 때만
같은 값(`http://127.0.0.1`)을 `oauthRedirectUri`로 넣는다 — `localhost`는 안 된다.

> **이 `127.0.0.1`은 Gitea 서버가 아니라 개발자 PC다.** GCM이 `git push` 때
> 개발자 PC에 임의 포트로 로컬 리스너를 띄우고, 승인이 끝나면 Gitea는 브라우저에
> `Location: http://127.0.0.1:<임의포트>/?code=...` 리다이렉트를 **응답으로만** 내려준다.
> 그 주소로 실제 접속하는 건 같은 PC의 브라우저이고, 인가 코드는 PC 안에서 GCM에게
> 전달된다 — Gitea 서버가 `127.0.0.1`로 접속하는 일은 없다. 그래서 서버 방화벽과는
> 무관하고, 반대로 **브라우저가 없는 환경(헤드리스 서버·CI 러너)에서는 이 방식이
> 성립하지 않는다**(→ [액세스 토큰 방식](#대안--액세스-토큰-gcm을-못-쓰는-환경)).

**포트는 그대로 전달된다.** 등록값에 포트가 없는 건 GCM이 실행 시점에 빈 포트를 잡기
때문이지, 포트가 빠진 채 리다이렉트된다는 뜻이 아니다. Gitea는 **비교할 때만** 포트를
떼고, 리다이렉트는 클라이언트가 보낸 URI를 그대로 쓴다.

| | 값 |
| --- | --- |
| 등록된 redirect URI (Gitea DB) | `http://127.0.0.1` |
| GCM이 authorize에 보내는 `redirect_uri` | `http://127.0.0.1:49xxx/` |
| Gitea가 내려주는 `Location` | `http://127.0.0.1:49xxx/?code=…&state=…` |

그래서 정상이라면 `Location`에 `127.0.0.1:<포트>`가 통째로 들어 있다 — 위
[승인했는데 127.0.0.1로 넘어가지 않는다](#승인했는데-127001로-넘어가지-않는다-codestate까지만-가고-끊김)의
판별 기준이 이것이다.

포트를 떼는 처리에는 조건이 둘 붙는다. 어느 쪽이든 어긋나면 매칭 자체가 실패한다.

- **`http`만** 해당된다. `https://127.0.0.1:<포트>`는 `https://127.0.0.1`이 등록돼 있어도
  포트가 안 떨어져 안 맞는다.
- **공개(non-confidential) 클라이언트만** 해당된다. Gitea UI에서 앱을 직접 만들며
  confidential로 두면 이 처리가 통째로 빠진다(내장 GCM 앱은 공개 클라이언트다).
- `localhost`가 안 되는 이유도 같다 — 조건이 "호스트명이 루프백 **IP**인가"라서
  `localhost`는 IP 파싱이 안 돼 포트 제거가 일어나지 않는다.

**바꿨을 때 같이 고쳐야 하는 것**

| 바뀐 것 | 함께 고칠 곳 |
| --- | --- |
| Gitea `ROOT_URL`(도메인·서브패스·스킴) | 1번 `redirect_uris` |
| `add-auth --name` | 1번 `redirect_uris`의 경로 이름 |
| 플랫폼 공개 주소 | `PAAS_PLATFORM_PUBLIC_URL`, Gitea의 `--auto-discover-url` |

값을 고쳤으면 플랫폼을 재시작해야 반영된다(`.env`는 기동 시 읽는다).

**d) 자동 계정 생성 허용** — 아래 [공통 설정](#c-공통-자동-계정-생성-설정) 참고.

플랫폼이 노출하는 엔드포인트(설정 확인용):
`/paas/.well-known/openid-configuration`, `/paas/oauth2/authorize`,
`/paas/oauth2/token`, `/paas/oauth2/jwks`, `/paas/oauth2/userinfo`.

> 서명 키는 `PAAS_OIDC_PROVIDER_SIGNING_KEY_PATH`(기본 `./data/oidc-signing-key.pem`)에
> 최초 기동 시 자동 생성돼 계속 재사용된다. **이 파일을 지우거나 옮기면 그 순간부터
> 기존에 발급된 토큰이 전부 검증에 실패한다** — 백업 대상에 포함할 것.

#### "인증서가 일치하지 않는다" 오류가 날 때

Gitea는 `add-oauth`로 인증 소스를 **만드는 시점에** 디스커버리 URL의 TLS 인증서를
검증하고, 실패하면 소스 자체가 만들어지지 않는다(`x509: certificate signed by unknown
authority` / hostname mismatch). Gitea에는 이 검증을 끄는 옵션이 없다
([go-gitea#17867](https://github.com/go-gitea/gitea/issues/17867)) — 인증서 쪽을 맞춰야 한다.

먼저 어느 이름이 안 맞는지부터 확인한다(Gitea가 도는 호스트에서 실행):
```bash
curl -v https://paas.example.com/paas/.well-known/openid-configuration 2>&1 | grep -Ei "subject|issuer|SAN|CN|verify"
```

원인별 조치:

| 증상 | 원인 | 조치 |
| --- | --- | --- |
| `certificate signed by unknown authority` | 사설 CA·자체 서명 인증서 | 그 CA 인증서를 **Gitea가 도는 호스트**의 신뢰 저장소에 넣는다. Linux: `/usr/local/share/ca-certificates/`에 복사 후 `update-ca-certificates`. Docker: 호스트의 CA 파일을 컨테이너 `/etc/ssl/certs/`에 마운트. Windows: 로컬 컴퓨터 → 신뢰할 수 있는 루트 인증 기관에 가져오기 |
| hostname mismatch (`certificate is valid for A, not B`) | 디스커버리 URL의 호스트명(B)이 인증서의 CN/SAN(A)에 없음 | 아래 "hostname mismatch 풀기" 참고 |
| 위 둘 다 아닌데 실패 | 내부 DNS가 그 호스트명을 다른 서버로 보냄 | Gitea 호스트에서 `getent hosts paas.example.com`으로 확인. 필요하면 `/etc/hosts`(또는 Windows `hosts`)에 올바른 IP를 고정 |

##### hostname mismatch 풀기 (`certificate is valid for A, not B`)

**B(디스커버리 URL에 쓴 이름)를 A(인증서에 있는 이름)로 바꾸는 것이 정답이다.** 반대로
"연결이 되니까" IP나 내부 호스트명을 그대로 두고 인증서 검증만 우회하려는 시도는
Gitea에 그런 옵션이 없어서 통하지 않는다.

주의할 점: **URL을 바꾸면 세 곳이 동시에 같아야 한다.** Gitea(엄밀히는 go-oidc)는
디스커버리 문서의 `issuer` 값이 자기가 조회한 URL과 정확히 일치하는지도 확인하기
때문에, 하나만 바꾸면 이번엔 issuer 불일치로 실패한다.

1. 플랫폼 `.env`의 `PAAS_OIDC_ISSUER`(없으면 `PAAS_PLATFORM_PUBLIC_URL`)
2. Gitea 인증 소스의 `--auto-discover-url`
3. `PAAS_OIDC_PROVIDER_CLIENTS`의 `redirect_uris`(이건 Gitea 쪽 주소라 별개지만,
   주소 체계를 바꿨다면 같이 점검할 것)

플랫폼이 지금 자기 발급자를 뭐라고 알고 있는지는 기동 로그(`[paas] OIDC Provider
활성화 — issuer=...`)나 디스커버리 문서로 확인한다:
```bash
curl -s https://<공개도메인>/paas/.well-known/openid-configuration | grep -o '"issuer":"[^"]*"'
```

**공개 도메인으로 바꿨더니 이번엔 연결이 안 되는 경우**(사내망에서 자기 공개 주소로
못 돌아오는 hairpin NAT 등): 이름은 그대로 두고 **경로만** 고친다 — Gitea가 도는
호스트의 hosts 파일에 그 도메인을 내부 IP로 고정한다. 이름이 안 바뀌므로 인증서는
계속 유효하다(IP로 바꿔 적으면 다시 mismatch가 난다).
```
# Linux: /etc/hosts   |   Windows: C:\Windows\System32\drivers\etc\hosts
10.0.0.5   paas.example.com
```

인증서에 이름을 추가할 수 있는 상황이면(사내 CA 등) 반대로 B를 SAN에 넣어 재발급해도
된다 — 그 경우 위 1·2의 URL은 B로 통일한다.

##### 80 포트(평문 http)만 쓸 수 있는 경우

443을 못 열어 전 구간을 http로만 운영한다면 **인증서가 개입할 여지가 없으므로 이 절의
문제 자체가 발생하지 않는다.** 주소를 `http://`로 통일하기만 하면 된다:

```bash
# 플랫폼 .env — https가 아니라 http로 적는 것이 핵심
PAAS_PLATFORM_PUBLIC_URL=http://paas.example.com
PAAS_OIDC_ISSUER=http://paas.example.com/paas
PAAS_OIDC_PROVIDER_CLIENTS={"gitea":{"secret":"<시크릿>","redirect_uris":["http://paas.example.com/gitea/user/oauth2/paas/callback"]}}
```
```bash
gitea admin auth add-oauth --name paas --provider openidConnect \
  --key gitea --secret "<시크릿>" \
  --auto-discover-url "http://paas.example.com/paas/.well-known/openid-configuration"
```

`PAAS_PLATFORM_PUBLIC_URL`을 **반드시 `http://`로** 둘 것. 이 값이 `https://`면 로그인
세션 쿠키에 `secure`가 붙어 브라우저가 저장을 거부하고, 그러면 로그인은 성공한 것처럼
보이는데 authorize는 계속 미로그인으로 판단해 로그인 화면으로 되돌린다(무한 루프).

> 평문 http라 **로그인 비밀번호·세션 쿠키·인가 코드·토큰이 모두 암호화되지 않는다.**
> 사내망 전용이라는 전제에서만 쓸 것이고, 가능해지면 https로 올리는 것이 맞다.
> (TLS를 상단 게이트웨이에서 종료하고 이 서버는 80으로만 받는 구성이라면, 사용자가
> 실제로 보는 주소가 https이므로 `PAAS_PLATFORM_PUBLIC_URL`은 그 https 주소로 적는다.)

##### 공개 도메인으로 아예 `/paas`가 안 들어오는 경우

공개 도메인의 binding이 이 플랫폼을 가리키지 않아(다른 사이트가 물고 있거나 그 경로
규칙이 없어) `https://<공개도메인>/paas`로 애초에 도달할 수 없다면, 위 방법들은 쓸 수
없다. **원칙적인 해법은 그 도메인에 `/paas` 라우팅을 추가하는 것**이고(아래 참고),
그게 불가능하면 백채널만 분리한다.

Gitea가 서버에서 부르는 호출(discovery·token·jwks)과 브라우저가 여는 화면
(authorization_endpoint)은 **같은 주소일 필요가 없다** — 클라이언트(go-oidc)는
디스커버리 문서의 `issuer`가 자기가 조회한 URL과 같은지만 확인하고,
`authorization_endpoint`가 같은 호스트인지는 보지 않는다
([go-oidc#159](https://github.com/coreos/go-oidc/issues/159)). 그래서 서버 호출만
사내 주소(평문 http)로 빼면 **TLS 자체를 안 타므로 인증서 문제가 사라진다**:

```bash
# 플랫폼 .env
PAAS_PLATFORM_PUBLIC_URL=https://public.example.com        # 브라우저가 가는 주소(그대로)
PAAS_OIDC_PROVIDER_BACKCHANNEL_URL=http://10.0.0.5:7000/paas   # Gitea가 서버에서 닿는 주소
# PAAS_OIDC_ISSUER는 설정하지 않는다 — 내장 Provider만 쓸 때 발급·검증 모두 위
# 백채널 주소로 닫힌다. 여기에 다른 값을 넣으면 발급값과 검증값이 어긋나 우리 토큰이
# 401이 된다(외부 Keycloak을 함께 신뢰할 때만 그 주소를 넣을 것).
```
```bash
# Gitea 인증 소스도 그 사내 주소로 등록한다(issuer와 같아야 하므로)
gitea admin auth add-oauth \
  --name paas --provider openidConnect \
  --key gitea --secret "<시크릿>" \
  --auto-discover-url "http://10.0.0.5:7000/paas/.well-known/openid-configuration"
```
결과: `issuer`·`token_endpoint`·`jwks_uri`는 사내 주소, `authorization_endpoint`만
공개 주소가 된다. 사용자는 평소처럼 공개 도메인에서 로그인하고, Gitea는 인증서 없이
사내로 토큰을 받아 간다.

**Gitea와 플랫폼이 같은 서버라면 `localhost`가 가장 간단하다** — 네트워크를 아예 안 타므로
방화벽·DNS·인증서가 전부 무관해진다(플랫폼 기동 포트가 7000이라면):
```bash
PAAS_OIDC_PROVIDER_BACKCHANNEL_URL=http://localhost:7000/paas
```
```bash
gitea admin auth add-oauth --name paas --provider openidConnect \
  --key gitea --secret "<시크릿>" \
  --auto-discover-url "http://localhost:7000/paas/.well-known/openid-configuration"
```

두 가지만 주의한다:

- **`PAAS_PLATFORM_PUBLIC_URL`을 반드시 함께 설정할 것.** 안 하면
  `authorization_endpoint`까지 `http://localhost:7000/...`으로 나가고, 그건 **사용자
  자기 PC**를 가리켜 로그인 화면이 열리지 않는다. (설정을 빠뜨리면 기동 로그와
  디스커버리 응답에서 바로 오류로 알려준다.)
- **Gitea가 컨테이너 안에서 돈다면 `localhost`는 그 컨테이너 자신이다.** 이 경우
  호스트를 가리키는 주소로 바꾼다 — Docker Desktop(Windows/macOS)은
  `http://host.docker.internal:7000/paas`, Linux Docker는 호스트 IP(예:
  `http://172.17.0.1:7000/paas`)를 쓰고 플랫폼이 그 주소로 들어오는 연결을 받는지
  확인한다(uvicorn을 `--host 127.0.0.1`로 띄웠다면 컨테이너에서 못 닿는다).

> 평문 http를 쓰므로 **그 구간이 신뢰할 수 있는 사내망이어야 한다**(같은 호스트나 같은
> 서브넷). 인터넷을 지나는 경로라면 쓰지 말 것 — 이 구간에는 인가 코드와 토큰이 흐른다.

**참고 — 라우팅을 추가하는 쪽(권장).** 공개 도메인 사이트에 `/paas` 경로 규칙 하나를
더하면 위 우회가 필요 없다. Caddy는 [서브패스 절](#서브패스단일-포트로-노출하는-경우)의
`handle`과 같은 방식이고, IIS/ARR이면 그 사이트의 URL Rewrite에 `/paas/*`를
`http://127.0.0.1:7000/paas/{R:1}`로 보내는 규칙을 추가한다(경로를 벗기지 말 것).

> **IIS는 규칙만으로는 부족하다.** 절대 URL로 보내는 rewrite는 ARR이 실제 전달을
> 담당하므로, 서버 레벨에서 아래 두 개를 켜야 한다. 안 켜면 `/paas` 전 경로가
> (`/paas/health`까지) **502**로 떨어지고, 첫 번째만 켜면 Authorization 헤더가 떨어져
> Bearer/OIDC 인증이 깨진다:
> ```powershell
> %windir%\system32\inetsrv\appcmd set config -section:system.webServer/proxy /enabled:True /commit:apphost
> %windir%\system32\inetsrv\appcmd set config -section:system.webServer/proxy /passThroughAuthorizationHeader:True /commit:apphost
> ```
> 플랫폼이 만드는 사이트에는 이 둘이 자동 적용되지만(`services/proxy/iis_proxy.py`의
> `_ensure_arr_proxy_enabled`), 사람이 직접 만든 `/paas` 규칙에는 적용되지 않는다.

> 플랫폼 **자신**은 이 문제를 겪지 않는다. 자기가 발급한 토큰은 로컬 개인키로 바로
> 검증하고 JWKS를 HTTP로 가져오지 않는다(`app/security.py`의 `_verification_key`) —
> `PAAS_OIDC_JWKS_URL`을 따로 설정할 필요도 없다.

### B. 외부 Keycloak을 쓰는 경우 (대안)

이미 사내에 Keycloak이 있고 그쪽을 SSO 허브로 삼는다면, 위와 형태는 같고 발급자만
Keycloak이다. Keycloak 콘솔 → 해당 Realm → Clients → Create로 `gitea` 클라이언트를
만들고(Client authentication: On, Valid redirect URI는 위와 같은 콜백 주소), 그 secret으로:
```bash
gitea admin auth add-oauth \
  --name keycloak --provider openidConnect \
  --key <gitea client id> --secret <client secret> \
  --auto-discover-url "${PAAS_OIDC_ISSUER}/.well-known/openid-configuration"
```
(Keycloak 자체 설치는 이 문서의 범위 밖.)

### C. 공통 — 자동 계정 생성 설정

새로 SSO로 들어오는 사람의 Gitea 계정을 관리자가 매번 미리 만들지 않아도 되게 한다
(`DISABLE_REGISTRATION=true`는 **로컬** 가입 폼만 막고 이 값에는 영향을 주지 않는다):
```
GITEA__oauth2_client__ENABLE_AUTO_REGISTRATION=true
GITEA__oauth2_client__USERNAME=email
GITEA__oauth2_client__ACCOUNT_LINKING=auto
```
(docker-compose는 `environment:` 블록에, 네이티브 Windows는 nssm `AppEnvironmentExtra`에
위 서브패스 예시처럼 이어붙여 추가하고 `nssm restart gitea`.)

## 플랫폼과 연결

0. **(조직별 자동 관리 — 권장)** Site Administration → Applications에서 조직/리포 생성
   권한이 있는 API 토큰을 발급해 플랫폼 `.env`의 `PAAS_GITEA_API_TOKEN`에 설정하면,
   콘솔의 "조직" 페이지(admin)에서 조직을 만들 때마다 여기 동명의 Organization이
   자동 생성되고, 조직 소속 프로젝트의 리포도 플랫폼이 대신 만든다 — 사용자는
   Gitea 화면에서 직접 리포를 만들 필요가 없다(일반 사용자에게 git_url도 노출 안 됨).
1. **(레거시) 프로젝트 등록**: 조직을 쓰지 않는 경우 `POST /paas/api/v1/projects`의 `git_url`을
   이 Gitea 인스턴스 주소로 직접 지정 (예: `https://git.example.com/org/shop-api`).
   git 저장소가 아직 없다면 `POST /paas/api/v1/projects/upload`(zip 또는 폴더)로도 등록할 수 있다 —
   업로드 내용을 플랫폼이 새 리포에 최초 push한다(조직 소속 필수).
2. **웹훅 자동 배포**:
   - **자동 등록(권장)**: 플랫폼 `.env`에 `PAAS_PLATFORM_PUBLIC_URL`(플랫폼 자신의
     공개 주소)을 설정하면, 조직 소속 프로젝트 생성이나 업로드로 리포를 만들 때마다
     플랫폼이 아래 웹훅을 자동 등록한다 — 이 단계를 수동으로 할 필요가 없다.
   - **수동 등록**: `PAAS_PLATFORM_PUBLIC_URL`을 쓰지 않거나 레거시(직접 git_url 지정)
     경로라면 Gitea 리포 → Settings → Webhooks → Add Webhook에서 직접 등록:
     - Payload URL: `https://<플랫폼>/paas/webhooks/git`
     - Secret: 플랫폼 `.env`의 `PAAS_WEBHOOK_SECRET`과 동일 값
     - Trigger: Push events
3. 이후 흐름은 [deployment-guide.md 3.4절](../../../docs/deployment-guide.md)의 GitHub 웹훅
   절차와 동일 — 서명 헤더 이름만 다를 뿐 플랫폼이 자동으로 구분한다.

## 백업

`gitea-data`(compose 볼륨) 또는 `gitea-data` PVC(K8s) 하나가 저장소·설정·DB(SQLite)를
전부 담고 있다. 정기 스냅샷 또는 `gitea dump` 명령으로 백업할 것.
