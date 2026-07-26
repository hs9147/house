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
sudo ufw allow 2222/tcp    # git SSH clone/push (docker-compose.yml이 컨테이너의 22를 여기로 매핑)
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

## 최초 설정 (공통)

1. `https://git.example.com` 접속 → 설치 마법사에서 관리자 계정 생성
2. Site Administration → Configuration → **"Enable registration"이 꺼져 있는지 확인**
   (compose/K8s 모두 `DISABLE_REGISTRATION=true` 기본값 — 계정은 관리자가 초대)
3. (선택) Keycloak SSO 연동 — 플랫폼의 `PAAS_OIDC_ISSUER`와 동일 Realm을 재사용해
   콘솔·Gitea 로그인을 통일:
   ```bash
   gitea admin auth add-oauth \
     --name keycloak --provider openidConnect \
     --key <gitea client id> --secret <gitea client secret> \
     --auto-discover-url "${PAAS_OIDC_ISSUER}/.well-known/openid-configuration"
   ```
   (Keycloak 쪽에 `gitea`용 OIDC 클라이언트를 먼저 등록해야 한다 — Keycloak 자체 배포는
   이 문서의 범위 밖.)

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
