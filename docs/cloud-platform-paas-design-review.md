# Cloud Platform(내부 PaaS) 개발 검토

> 대상: React · Python · Node · LLM 앱을 하나의 서버에서 배포·운영하는 자체 Deploy Server(내부 PaaS) 제안
> 작성일: 2026-07-12
> 관련 인프라: CHO-FAM(Firebase Hosting + Functions + Firestore), liv-ay 게임서버

---

## 1. 총평

제안된 구조(Build Manager / Container Manager / Reverse Proxy / SSL / Log / Monitor / Auto Restart / API)는
내부 PaaS의 표준 구성을 정확히 짚고 있고, 방향성 자체는 타당합니다. 다만 그대로 전부 구현하면
**사실상 Coolify/Dokploy를 재작성하는 규모**가 되므로, 아래 세 가지를 권장합니다.

1. **런타임은 처음부터 Docker로 고정** — "Docker 없이 프로세스 방식"은 제안서에 대안으로 있지만,
   의존성 충돌·포트 관리·보안 격리·재시작 정책을 전부 직접 구현하게 되어 오히려 일이 커집니다.
   Docker restart policy, 로그, 리소스 제한을 공짜로 얻는 쪽이 이득입니다.
2. **Reverse Proxy는 Caddy** — Let's Encrypt 발급·갱신이 내장되어 있어 제안서의
   "SSL Manager" 컴포넌트가 통째로 사라집니다. (Nginx 선택 시 certbot 연동·갱신 크론·리로드를 직접 관리)
3. **초기 스택 다이어트** — PostgreSQL + Redis + Celery/RabbitMQ는 1인~소규모 운영에는 과합니다.
   SQLite + FastAPI(BackgroundTasks 또는 arq)로 시작해도 동일한 기능을 구현할 수 있고,
   나중에 교체 비용도 낮습니다.

**결론: 자체 개발 가치는 "LLM/GPU 관리 레이어"에 있습니다.** 일반 웹앱 배포(React/Node/Python)는
기존 오픈소스가 이미 잘 해결한 영역이고, vLLM/Ollama 모델 관리·VRAM 스케줄링·API 키/사용량 제한은
기존 도구가 약한 영역이므로 여기에 개발력을 집중하는 것을 추천합니다.

---

## 2. 자체 개발 vs 오픈소스 활용

| 선택지 | 장점 | 단점 | 적합한 경우 |
| --- | --- | --- | --- |
| **자체 개발 (제안안)** | liv-ay/LLM 요구에 정확히 맞춤, 학습 효과 | 개발·유지보수 비용 최대, 보안 책임 전부 부담 | LLM 관리가 핵심이고 장기 운영 의지가 있을 때 |
| **Coolify** | Git push 배포, SSL, 도메인, 로그, 웹훅 전부 내장. 가장 활발한 커뮤니티. **Apache 2.0 완전 오픈소스** | LLM/GPU 관리 기능 없음, 커스터마이징 한계 | 웹앱 배포가 주 목적일 때 |
| **Dokploy** | Docker Compose 친화, Traefik 내장, 가벼움 | ⚠️ 코어는 Apache 2.0이지만 일부(`/proprietary`, 템플릿·멀티노드)가 source-available — 상용 재배포 제약. 수익 플랫폼 기반으로는 비추천 (10절 참고) | 내부 전용일 때만 |
| **하이브리드 (권장)** | 웹앱은 Coolify/Dokploy에 맡기고, **LLM 관리 플랫폼만 자체 개발** | 두 시스템 운영 | 개발 리소스가 제한적이고 LLM이 차별점일 때 |

전부 직접 만드는 경험 자체가 목표라면 자체 개발도 좋은 선택입니다. 그 경우 아래 3~7절의
설계 보완 사항을 반영하는 것을 전제로 합니다.

---

## 3. 컴포넌트별 검토

### 3.1 Build Manager

제안된 명령(npm build / pip install / vllm serve)은 맞지만, 두 가지 보완이 필요합니다.

- **빌드 격리**: 호스트에서 직접 `npm install`을 실행하면 프로젝트 간 Node/Python 버전 충돌이 납니다.
  빌드도 컨테이너 안에서 수행하세요. 프로젝트 타입별 기본 Dockerfile 템플릿을 플랫폼이 제공하고,
  리포에 Dockerfile이 있으면 그것을 우선하는 방식이 단순하고 예측 가능합니다.
  (Nixpacks/Buildpacks 자동 감지는 매력적이지만 디버깅이 어려워 초기에는 비추천)
- **빌드 산출물 = 이미지 태그**: 빌드 결과를 `프로젝트명:git-sha` 이미지로 남기면
  **Rollback이 "이전 이미지로 컨테이너 재기동" 한 줄**이 됩니다. 별도 버전 관리 로직이 거의 필요 없습니다.

### 3.2 Runtime Manager

- Docker Engine API(python `docker` SDK)로 충분합니다. Kubernetes는 단일 서버에서는 불필요.
- 컨테이너 생성 시 반드시 지정할 것: `restart_policy={"Name": "on-failure", "MaximumRetryCount": 3}`,
  메모리 제한(`mem_limit`), CPU 제한, 그리고 LLM 컨테이너는 `device_requests`로 GPU 할당.
- **포트는 플랫폼이 할당**(예: 8001부터 순차)하고 DB에 기록. 컨테이너는 내부 포트만 알면 됩니다.
- 제안서의 "Auto Restart → 3회 실패 → 관리자 알림"은 Docker restart policy + Docker 이벤트 스트림
  구독(`docker events`)으로 구현하면 폴링이 필요 없습니다. 알림은 이미 운영 중인
  **CHO-FAM 메일 API(`POST /api/mail/send`)를 그대로 재사용**하면 됩니다 — 신규 개발 불필요.

### 3.3 Reverse Proxy + SSL + Domain

- **Caddy 강력 추천.** 도메인 추가 = Caddyfile에 블록 한 개 추가 + `caddy reload`(무중단).
  인증서 발급·갱신·HTTP→HTTPS 리다이렉트가 전부 자동이라 제안서의 4·5번 컴포넌트가
  "Caddyfile 템플릿 렌더링 + reload API 호출"로 축소됩니다.
- 서브도메인이 많아질 예정이면 와일드카드 DNS(`*.deploy.example.com` → 서버 IP) 하나 잡아두면
  도메인 등록 절차 자체가 사라집니다.

### 3.4 Log Manager

- `docker logs --follow`를 FastAPI WebSocket으로 릴레이하면 실시간 로그는 하루면 구현됩니다.
- 주의: **로그 로테이션**을 Docker daemon 옵션(`json-file` + `max-size=10m`, `max-file=3`)으로
  걸어두지 않으면 디스크가 반드시 찹니다. 초기 설정에 포함할 것.
- 빌드 로그는 배포(Deployment) 레코드에 파일로 붙여 보관 — 실패 원인 추적에 필수.

### 3.5 Monitoring

- 시스템: `psutil` (CPU/메모리/디스크/네트워크), GPU: `nvidia-ml-py`(NVML 공식 바인딩) 또는
  `nvidia-smi --query-gpu=... --format=csv` 파싱. 컨테이너별은 Docker stats API.
- 초기에는 30초 주기 수집 → SQLite에 링버퍼(최근 24h)로 충분. Prometheus+Grafana는
  대시보드 요구가 커졌을 때 도입해도 늦지 않습니다.
- **VRAM은 LLM 플랫폼의 핵심 지표**: 모델 로드 전에 "요청 모델의 예상 VRAM vs 현재 여유 VRAM"을
  검사해서 OOM으로 GPU 전체가 죽는 것을 막는 로직이 기존 PaaS에 없는 진짜 차별화 기능입니다.

### 3.6 Auto Deploy (GitHub Webhook)

- **HMAC 서명 검증(`X-Hub-Signature-256`)은 선택이 아니라 필수**입니다. 미검증 웹훅 엔드포인트는
  임의 코드 실행 취약점과 같습니다.
- 같은 프로젝트에 push가 연달아 오는 경우를 대비해 **프로젝트별 배포 락**(동시 배포 1건) +
  마지막 커밋만 배포(중간 커밋 스킵)를 넣으세요.
- 배포 순서는 무중단을 고려해: 새 컨테이너 기동 → 헬스체크 통과 → 프록시 전환 → 구 컨테이너 종료.
  (blue-green 최소형. 처음부터 이렇게 잡는 게 나중에 고치는 것보다 쉽습니다)

---

## 4. 데이터 모델 보강

제안된 단일 `Project` 테이블에서 **배포 이력과 환경변수를 분리**해야 Rollback과 시크릿 관리가 됩니다.

```
Project                    Deployment                 EnvVar
--------                   -----------                -------
id                         id                         id
name                       project_id (FK)            project_id (FK)
type (react/python/        git_sha                    key
      node/llm)            image_tag                  value_encrypted   ← 평문 저장 금지
git_url                    status (building/running/  is_secret
branch                             failed/stopped)
domain                     build_log_path
port (플랫폼 할당)          created_at
health_check_path          finished_at
created_at
```

- `Deployment`가 이력 테이블이므로 Rollback = "이전 Deployment의 image_tag로 재기동".
- 환경변수는 **암호화 저장**(예: `cryptography.Fernet`, 키는 서버 환경변수로만 보관).
  현재 functions에서 시크릿을 Firebase secrets로 관리하는 것과 같은 원칙입니다.
- LLM 프로젝트는 확장 필드: `model_name`, `quantization`, `max_model_len`, `gpu_ids`, `estimated_vram_gb`.

---

## 5. 기술 스택 검토 (제안 대비 수정안)

| 구성 요소 | 제안 | 검토 의견 |
| --- | --- | --- |
| Backend | FastAPI | ✅ 유지. WebSocket·async 지원으로 적합 |
| Frontend | React + Vite | ✅ 유지. admin 대시보드 경험(public/admin) 재활용 가능 |
| DB | PostgreSQL | ⚠️ 초기엔 **SQLite**로 충분(단일 서버, 낮은 쓰기 빈도). SQLAlchemy로 작성해두면 이관 무비용 |
| Cache | Redis | ⚠️ 초기 불필요. 큐 도입 시 함께 |
| Queue | Celery/RabbitMQ | ⚠️ 과함. FastAPI BackgroundTasks → 부족해지면 **arq**(Redis 기반, 경량) |
| Container | Docker Engine API | ✅ 유지 (python `docker` SDK) |
| Proxy | Nginx 또는 Caddy | ✅ **Caddy로 확정 권장** (SSL 컴포넌트 제거 효과) |
| Process 관리 | Supervisor | ❌ 불필요. Docker restart policy로 대체 |
| 인증 | JWT + OAuth | ⚠️ 내부 도구는 **API 키(해시 저장) + 관리자 세션**으로 시작. OAuth는 사용자가 늘면 |
| LLM | vLLM, Ollama | ✅ 유지. OpenAI 호환 엔드포인트로 통일 |

---

## 6. 규모별 기술 스택 정의 — 1차 중소규모 / 2차 대기업 규모

> 원칙: 1차 → 2차는 **"교체"가 아니라 "위임 대상 확대"**입니다.
> 컨트롤 플레인(FastAPI API + React 대시보드 + PostgreSQL + Gitea)은 두 단계에서 동일하게 유지하고,
> 실행 계층만 "Docker 단일/소수 서버" → "Kubernetes 클러스터"로 바꿉니다.
> 전 항목이 10절의 라이선스 기준(무료·상용 무제한)을 충족합니다.

### 6.1 1차 — 중소규모

목표 규모: 서버 1~3대, 프로젝트 ~50개, 사용자 ~수십 명, GPU 1~2대. 운영 인력 1~2명.

| 영역 | 선택 | 근거 |
| --- | --- | --- |
| 컨트롤 플레인 | FastAPI + SQLAlchemy | 2차에서도 그대로 유지되는 자산 |
| DB | SQLite → PostgreSQL 16 | 프로젝트 10개/동시 사용자 5명 넘으면 PostgreSQL 전환 |
| 캐시·큐 | Valkey + arq | 큐가 필요해지는 시점(웹훅 폭주)까지는 BackgroundTasks로 버팀 |
| 런타임 | Docker Engine | restart policy·리소스 제한·로그 포함 |
| 프록시·SSL | Caddy | 도메인·인증서 자동화 일체 |
| 소스 관리·CI | Gitea + Gitea Actions | GitHub 대체 + CI 내장 (10.2절) |
| 모니터링 | psutil + nvidia-ml-py + Prometheus | 수집만 표준화, 저장은 SQLite 링버퍼 |
| 대시보드 | 자체 React 대시보드 | admin 대시보드 경험 재활용 |
| 로그 | Docker json-file(rotation) + WebSocket | 3.4절 |
| 인증 | API 키(해시) + 관리자 세션 | CHO-FAM x-api-key 패턴 재사용 |
| 시크릿 | DB 암호화 컬럼(Fernet) | 키는 서버 환경변수로만 |
| 스토리지 | 로컬 파일시스템 | 정적 배포 산출물·빌드 로그 |
| LLM | vLLM / Ollama 직결 | 단일 GPU 서버, 게이트웨이는 FastAPI가 겸함 |

### 6.2 2차 — 대기업 규모

요구 변화: 무중단 HA, 수백 프로젝트·수백 사용자, 팀/권한(RBAC), SSO(AD·LDAP), 감사 로그,
테넌트 격리, 이미지 취약점 스캔, 장기 메트릭 보관.

| 영역 | 선택 | 라이선스 | 근거 |
| --- | --- | --- | --- |
| 오케스트레이션 | Kubernetes (K3s부터 가능) | Apache 2.0 | 스케줄링·오토스케일·롤링배포·자가치유를 직접 구현하지 않고 위임. 플랫폼의 Runtime Manager는 "K8s 매니페스트 생성기"로 역할 전환 |
| Ingress·SSL | Traefik 또는 ingress-nginx + cert-manager | MIT / Apache 2.0 | Caddy 역할의 클러스터 버전 |
| DB | PostgreSQL HA (CloudNativePG 또는 Patroni) + PgBouncer | Apache 2.0 / PostgreSQL | 컨트롤 플레인 DB 무중단화 |
| 큐·이벤트 | Valkey Cluster + NATS JetStream | BSD-3 / Apache 2.0 | 배포 이벤트·웹훅 팬아웃 |
| 이미지 레지스트리 | Harbor (+Trivy 스캔) | Apache 2.0 | 프라이빗 레지스트리 + 취약점 스캔 + 프로젝트별 RBAC |
| SSO·IAM | Keycloak | Apache 2.0 | OIDC/SAML, AD·LDAP 연동. Gitea·대시보드·API 전부 OIDC로 통합 — Git 서버 SSO를 유료판 없이 해결 |
| 시크릿 | OpenBao | MPL 2.0 | ⚠️ HashiCorp Vault는 BSL 전환 — Linux Foundation 포크인 OpenBao 사용 |
| IaC | OpenTofu + Ansible | MPL 2.0 / GPL | ⚠️ Terraform도 BSL — OpenTofu 사용 |
| 관측성 | Prometheus + VictoriaMetrics(장기 보관) + OpenTelemetry, 로그 VictoriaLogs 또는 OpenSearch | Apache 2.0 | Grafana/Loki(AGPL) 없이 구성 가능 (10절 기준) |
| 스토리지 | SeaweedFS 또는 Rook-Ceph | Apache 2.0 | 산출물·모델 파일 분산 저장 |
| GPU | NVIDIA GPU Operator + device plugin | Apache 2.0 | GPU 노드풀 스케줄링, MIG 분할 |
| LLM 게이트웨이 | 자체 게이트웨이 유지 또는 LiteLLM(코어 MIT) | MIT | API 키·rate limit·사용량 집계는 Phase 3 자산 그대로 |
| 소스 관리 | Gitea/Forgejo HA + Keycloak OIDC | MIT / GPL-3.0 | 1차 자산 유지, SSO만 추가 |

### 6.3 1차에서 지키면 2차 전환이 싸지는 설계 규칙

1. **배포 단위를 이미지 태그로 고정** (3.1절) — K8s 전환 시 Deployment 매니페스트에 태그만 꽂으면 됨
2. **Runtime Manager를 인터페이스로 추상화** — `DockerRuntime` 구현체를 `K8sRuntime`으로 교체하는 구조
3. **SQLAlchemy 사용** — SQLite→PostgreSQL→HA 전환 무비용
4. **배포되는 앱에 12-factor 강제** — 설정은 환경변수, 로그는 stdout, 로컬 상태 금지
5. **인증을 미들웨어 한 곳에 집중** — API 키 → Keycloak OIDC 교체 지점을 단일화
6. **감사 대상 행위(배포·롤백·시크릿 변경·키 발급)를 1차부터 이벤트 테이블에 기록** — 2차의 감사 로그 요구는 스키마가 아니라 UI 문제가 되도록

---

## 7. 보안 체크리스트 (자체 개발 시 반드시)

1. GitHub Webhook HMAC 서명 검증 (3.6절)
2. 환경변수/시크릿 암호화 저장, API 응답에서 값 마스킹
3. 배포 API 전체 인증 필수 — CHO-FAM 메일 API의 `x-api-key` 패턴 재사용 가능
4. 컨테이너에 `--privileged` 금지, 호스트 볼륨 마운트 최소화, 가능하면 non-root 실행
5. Docker 소켓(`/var/run/docker.sock`)을 노출하는 컨테이너 금지 — 배포 서버 프로세스만 접근
6. 관리 대시보드는 공개 인터넷에 그대로 노출하지 말 것 (최소한 IP 제한 또는 VPN/Tailscale)
7. LLM API 키 발급 시: 키는 해시로 저장, 프리픽스만 노출, 사용량 카운팅은 Redis 도입 후 정확화
8. 빌드 시 리포지토리 코드가 호스트 권한으로 실행되지 않도록 빌드 컨테이너 격리 (3.1절)

---

## 8. 단계별 로드맵

### Phase 1 — MVP (핵심 루프 검증)
- Project CRUD + git clone/pull
- Dockerfile 기반 빌드 → `이름:sha` 이미지 → 컨테이너 실행
- Caddy 연동(도메인 + 자동 HTTPS)
- WebSocket 실시간 로그
- API 키 인증
- 스택: FastAPI + SQLite + Docker SDK + Caddy

### Phase 2 — 자동화
- GitHub Webhook 자동 배포(서명 검증 + 배포 락)
- 헬스체크 기반 무중단 전환(blue-green 최소형)
- Rollback (Deployment 이력 기반)
- Crash 알림 → CHO-FAM 메일 API 연동
- React 관리 대시보드

### Phase 3 — LLM 플랫폼 (차별화 구간)
- vLLM/Ollama 서버 등록·모델 관리
- VRAM 사전 검사 + GPU 할당 스케줄링 (3.5절)
- OpenAI 호환 게이트웨이 + API 키 발급·rate limit·사용량 집계
- GPU/VRAM 대시보드 (NVML)

### Phase 4 — 확장 (필요해질 때만)
- 2차(대기업 규모) 스택으로 전환 — Kubernetes·Harbor·Keycloak·OpenBao 등 6.2절 참고
- 이미지 생성 서버(Flux 등) 등록

---

## 9. 리스크

| 리스크 | 내용 | 완화 |
| --- | --- | --- |
| 유지보수 부담 | 배포 서버 자체가 SPOF. 배포 서버가 죽으면 모든 앱 배포 불가 | 앱 런타임(Docker+Caddy)은 배포 서버 프로세스와 독립적으로 동작하도록 설계 — 배포 서버가 죽어도 서비스는 계속 뜸 |
| 보안 사고 | 웹훅/시크릿/도커 소켓 취급 실수 시 서버 전체 장악 가능 | 7절 체크리스트를 Phase 1부터 적용 |
| 범위 팽창 | 10개 컴포넌트 동시 개발 시 완성 전에 동력 상실 | Phase 1을 2주 내 완결 가능한 크기로 고정 |
| 오픈소스와의 중복 | Phase 1~2는 Coolify가 이미 제공 | LLM 요구가 확실치 않으면 2절 하이브리드안 재검토 |

---

## 10. 오픈소스 라이선스·비용 검토 (사내·수익 활동 기준)

> 기준: 자체 서버에 self-host, 기업 내부 및 수익 활동 사용, 소프트웨어 비용 0원, 상용 제약 없는 라이선스.
> 결론부터: **전 구간을 이 기준으로 구성 가능**합니다. 비용은 서버·도메인·트래픽뿐입니다.

### 10.1 라이선스 판단 기준 (3줄 요약)

| 라이선스 계열 | 상용/사내 self-host | 비고 |
| --- | --- | --- |
| MIT / Apache 2.0 / BSD | ✅ 무제한 안전 | 수정·재판매·비공개 포크 전부 가능 |
| GPL / AGPL | ✅ 내부 사용·수익 활동 안전 | **수정본을 배포하거나(GPL) 외부에 네트워크 서비스로 제공(AGPL)할 때만** 소스 공개 의무. 도구를 "쓰는" 것만으로는 의무 없음 |
| SSPL / BSL / RSAL / fair-source | ⚠️ 회피 권장 | "오픈소스처럼 보이는" 상용 제약 라이선스. 대체재가 있으면 쓰지 말 것 |

### 10.2 소스 관리 — GitHub 대체 (self-host Git 서버)

| 후보 | 라이선스 | 평가 |
| --- | --- | --- |
| **Gitea (권장)** | MIT | 경량(RAM 수백 MB), GitHub 스타일 UI, 웹훅·REST API, **Gitea Actions(GitHub Actions 호환 CI) 내장**. 우리 Auto Deploy 웹훅 설계를 그대로 연결 가능 |
| Forgejo | GPL-3.0 | Gitea의 커뮤니티 포크(Codeberg e.V. 비영리 거버넌스). 기능 동등 이상, 보안 패치 공개가 더 투명. GPL이지만 self-host 사용엔 제약 없음 |
| GitLab CE | MIT | 기능 최다이나 무겁다(권장 RAM 4GB+). 단일 서버에 앱들과 동거시키기엔 부담 |
| Gogs | MIT | 가장 가볍지만 개발 활동 저조 — 비추천 |

Gitea 선택 시: 배포 서버의 GitHub Webhook 처리(3.6절)는 Gitea 웹훅과 페이로드 형식이 거의 동일해
(HMAC 서명 헤더만 `X-Gitea-Signature`) 코드 수정이 최소화됩니다.

**구현 완료** — `platform/infra/gitea/`에 1차(Docker Compose)·2차(K8s manifests) 배포 산출물과
웹훅·Keycloak SSO 연동 절차를 제공한다. 콘솔에도 `PAAS_GITEA_URL` 설정 시 **Git 메뉴**가
나타나 등록 프로젝트별 리포 바로가기를 보여준다. 상세: `platform/infra/gitea/README.md`,
[deployment-guide.md 3.8절](./deployment-guide.md).

**기업용 코드 내부 관리 강제** — `PAAS_GIT_INTERNAL_ONLY=true`로 켜면 프로젝트 등록 시
`git_url` 호스트가 `PAAS_GITEA_URL`과 다르면 422로 거부한다(`app/git_policy.py`). internal
LLM 프로바이더가 라벨일 뿐 강제되지 않던 문제(15절)와 같은 원칙을 소스 저장소 등록에도
적용해, "기업용은 사내 Gitea에 올린 코드만 관리한다"는 정책이 설정이 아니라 코드로
보장되게 했다.

**조직별 작업공간 + 리포 내부 생성** — `Organization` 모델 추가(`app/models.py`).
콘솔 조직 페이지(admin)에서 조직을 만들면 `services/gitea.py`가 사내 Gitea에 동일한
Organization을 함께 생성한다. 조직 소속 프로젝트(`POST /projects`의 `organization_id`)는
리포 생성 자체를 플랫폼이 Gitea API로 대신 수행하므로 사용자가 Git 주소를 다루지 않는다.
`ProjectOut.git_url`은 비관리자 응답에서 `"(내부 관리 — 관리자만 조회 가능)"`으로 마스킹되어
"사용자에게 URL 등 메타 정보를 노출하지 않는다"는 요건을 충족한다. organization_id 없이
git_url을 직접 지정하는 레거시 경로는 하위 호환을 위해 유지된다.

**zip/폴더 업로드 등록 + 코드 확인 화면 + 원클릭/자동 배포 완결** — 아직 git 저장소가
없는 코드도 프로젝트로 등록할 수 있어야 한다는 요건에 `POST /projects/upload`(조직 필수)를
추가했다. zip 또는 폴더(다중 파일)를 올리면 `services/upload.py`가 안전하게 스테이징한 뒤
사내 Gitea 신규 리포에 최초 커밋으로 push한다 — 대용량·악성 업로드 방어는 (1) 업로드 원본
자체의 스트리밍 크기 상한, (2) 압축 해제 시 **zip 헤더 선언값이 아닌 실제 압축 해제
바이트 수**로 강제하는 총량 상한(zip bomb 방어의 핵심 — 헤더는 위조 가능하므로 신뢰하지
않는다), (3) 엔트리 수 상한, (4) 파일별 압축비 사전 점검, (5) 절대경로·상위 디렉토리
탈출(zip slip)·심볼릭 링크 엔트리 거부로 구성했다. 조직/업로드로 만든 리포는 Gitea에
생성된다(현재 public — `services/gitea.py`). push는 가시성과 무관하게 항상 인증이
필요하므로 `services/git_auth.py`가 `PAAS_GITEA_API_TOKEN`을 git 프로세스에
`http.extraHeader`로 주입해 인증한다(git_url 자체엔 토큰을 심지 않음) — 이는 앞서
구현된 조직별 작업공간의 잠재 버그(당시 private 리포를 플랫폼이 스스로 clone하지
못하던 문제)를 함께 고친 것이기도 하다. 코드 **수정**은 여전히 12절 원칙대로
LLM 채팅 → diff 제안 → 승인(`POST /changes/{id}/apply`)으로만 가능하지만, 코드 **확인**을
위한 읽기 전용 파일 트리·내용 조회 API(`GET /projects/{id}/files`, `/files/content`)와
콘솔 코드 탭을 추가해 저장/수정 엔드포인트 없이도 현재 코드를 볼 수 있게 했다. 마지막으로
자동 배포의 남은 수동 단계였던 Gitea 웹훅 등록을 `PAAS_PLATFORM_PUBLIC_URL` 설정 시
리포 생성 때마다 플랫폼이 스스로 등록하도록 자동화했다(베스트 에포트 — 실패해도 프로젝트
생성 자체는 성공 처리, 미설정 시 `infra/gitea/README.md`의 수동 절차로 대체 가능).

### 10.3 스택 전체 라이선스 표

| 구성 요소 | 도구 | 라이선스 | 판정 |
| --- | --- | --- | --- |
| Backend | FastAPI | MIT | ✅ |
| Frontend | React, Vite | MIT | ✅ |
| DB | SQLite → PostgreSQL | Public Domain / PostgreSQL(BSD계) | ✅ |
| 컨테이너 | **Docker Engine** (Linux) | Apache 2.0 | ✅ ⚠️ **Docker Desktop은 기업 유료** — 서버는 Engine만 쓰므로 무관하나 개발 PC에서 주의 |
| Proxy + SSL | Caddy | Apache 2.0 | ✅ |
| 인증서 | Let's Encrypt | 무료 CA (상용 OK) | ✅ |
| Cache/Queue | ~~Redis~~ → **Valkey** | BSD-3 | ✅ Redis는 2024년 SSPL 전환 → 2025년 Redis 8부터 AGPLv3 복귀로 혼란. Linux Foundation 포크인 Valkey(BSD-3, 드롭인 호환)가 상용 기준 가장 깔끔 |
| 작업 큐 | arq (Valkey 사용) | MIT | ✅ |
| 모니터링 수집 | psutil, nvidia-ml-py, Prometheus | BSD / Apache 2.0 | ✅ |
| 대시보드 | 자체 React 대시보드 권장 | — | Grafana는 AGPLv3 — 내부 사용은 문제없으나, 회피하려면 자체 구현 또는 VictoriaMetrics(Apache 2.0) 계열 |
| Git 서버 | Gitea | MIT | ✅ |
| CI | Gitea Actions 또는 Woodpecker CI | MIT / Apache 2.0 | ✅ Drone CI는 BSL 전환 — 회피 |
| LLM 서빙 | vLLM / Ollama | Apache 2.0 / MIT | ✅ |
| 오브젝트 스토리지 | (필요 시) SeaweedFS | Apache 2.0 | MinIO는 AGPL + 2025년 커뮤니티판 관리 UI 축소 — 회피. 초기엔 로컬 파일시스템으로 충분 |
| 기성 PaaS(하이브리드안) | Coolify | Apache 2.0 | ✅ / Dokploy는 일부 source-available — 회피 |

### 10.4 요주의 목록 정리

- **Redis** → Valkey로 대체 (드롭인 호환, 코드 수정 불필요)
- **Dokploy** → 수익 플랫폼 기반으로는 부적합, Coolify 사용
- **MinIO** → SeaweedFS 또는 파일시스템
- **Drone CI** → Woodpecker CI 또는 Gitea Actions
- **Docker Desktop** → 서버는 Docker Engine(무료), 맥/윈도 개발 PC는 기업 규모에 따라 유료일 수 있음 (대안: OrbStack 유료, Colima 무료)
- **Grafana/Loki (AGPL)** → 내부 사용은 합법·무료지만, 플랫폼 기능으로 외부 제공·수정 배포 계획이 있으면 자체 대시보드로

---

## 11. 결론

- 설계 방향은 타당하며, 구성 요소 목록도 빠짐없음.
- **Docker 고정 + Caddy 채택 + 초기 스택 축소(SQLite, 큐 생략)** 세 가지만 반영하면
  구현 난이도가 크게 내려가고, 제안서의 컴포넌트 중 2개(SSL Manager, Process 관리)가 사실상 제거됨.
- 자체 개발의 진짜 가치는 **Phase 3(LLM/GPU 관리)**에 있으므로, 웹앱 배포(Phase 1~2)를
  오픈소스로 대체하는 하이브리드안도 병행 검토 권장.
- 기존 CHO-FAM 자산(메일 API, x-api-key 인증 패턴, admin 대시보드 UI)을 알림·인증·프론트엔드에
  재사용하면 개발량을 추가로 줄일 수 있음.
- 라이선스·비용: **전 스택을 무료·상용 무제한 라이선스로 구성 가능**(10절).
  소스 관리는 Gitea(MIT) self-host로 GitHub 대체, Redis 대신 Valkey 사용이 핵심 포인트.

---

## 12. 코드 워크스페이스 — LLM 대화식 코드 작성·리뷰·실행 미리보기

배포 플랫폼 위에 "코드를 만지는" 레이어를 얹는다. 세 기능은 별개가 아니라 하나의 루프다:

```
대화(LLM) → 코드 편집/리뷰(diff) → 실행 미리보기 → 다시 대화
```

### 12.1 대화식 코드 작성·편집 (외부/내부 LLM)

- **프로바이더 추상화 — OpenAI 호환으로 통일.** 외부(Claude API, OpenAI 등)와
  내부(플랫폼에 llm 타입으로 배포된 vLLM/Ollama)를 같은 인터페이스로 등록하고 대화 시 선택한다.
  내부 LLM 옵션이 있으므로 **소스 코드가 회사 밖으로 나가지 않는 모드**가 가능 — 기업 내부 사용의
  핵심 요구이자 이 플랫폼의 차별점(1절의 LLM 레이어 전략과 합치).
- 외부 프로바이더의 API 키는 기존 EnvVar와 동일하게 **Fernet 암호화 저장**, 응답에서는 마스킹.
- 채팅 세션은 프로젝트 워크스페이스(checkout된 리포)에 바인딩된다. 파일 트리·선택 파일을
  컨텍스트로 주입하고, LLM의 수정 제안은 **항상 diff(patch)로 생성** → 웹 UI diff 뷰에서
  검토 → 승인 시에만 작업 브랜치에 커밋. LLM이 리포에 직접 쓰는 일은 없다.
- **자동 코드 리뷰**: push/배포 전 diff를 LLM에 전달해 리뷰 코멘트(버그·보안·스타일, 심각도 분류)를
  생성. 초기엔 참고용, 이후 "심각도 high 발견 시 release 배포 차단" 같은 게이트를 선택 적용.
- **모델 라우팅 정책**: 프로젝트/조직 단위로 "이 프로젝트는 내부 LLM만 허용" 규칙을 설정
  (2차 대기업 요구인 데이터 거버넌스에 대응).

### 12.2 외부/내부 API·파일·DB 모듈화 및 환경설정

코드가 의존하는 자원(API·파일 저장소·DB)을 **Module로 등록하고 프로젝트에 바인딩**하면
환경설정이 자동 주입되는 구조. 코드에서 자격증명이 사라지고, LLM 코드 생성의 재료가 된다.

| Module 타입 | 예 | 바인딩 시 주입되는 환경변수(규약) |
| --- | --- | --- |
| external_api | 결제 API, CHO-FAM 메일 API | `{PREFIX}_URL`, `{PREFIX}_API_KEY` |
| internal_api | 플랫폼에 배포된 다른 프로젝트 | `{PREFIX}_URL` — 1차: Caddy 도메인, 2차: K8s Service DNS로 자동 해석 |
| database | PostgreSQL, SQLite | `{PREFIX}_DSN` |
| ~~file_storage~~ | 로컬 볼륨, SeaweedFS | (폐기 — 아래 주 참고) |

> **file_storage 타입은 폐기됐다.** 저장소 경로는 모듈로 등록하는 것이 아니라
> `PAAS_STORAGE_ROOT`(내부 저장소)·`PAAS_DOC_ROOTS`(사내 문서 폴더, 읽기 전용)
> 환경변수가 정하고, 접근은 `/storage` 창구와 사내 MCP 서버(`/mcp/docs`,
> `/mcp/storage/{저장소}`)가 맡는다. 디스크 경로는 서버를 설치한 사람이 이미 아는
> 사실이지 콘솔에서 등록할 일이 아니었고, 모듈로 두면 같은 폴더가 이름만 달리 두 번
> 등록되거나 존재하지 않는 경로가 등록돼도 열어 보기 전까지 아무도 모른다. 따라서
> 저장소는 바인딩 대상이 아니며 `{PREFIX}_URL`·`{PREFIX}_BUCKET` 주입도 없다.

- 자격증명은 전부 기존 EnvVar 암호화 경로 재사용 — 새 보안 표면을 만들지 않는다.
- internal_api 모듈은 **서비스 디스커버리를 겸한다**: 프로젝트 간 호출을 도메인 하드코딩 없이
  모듈 참조로 연결하면 1차→2차 전환 시 주소 체계가 바뀌어도 코드 수정이 없다(6.3절 규칙 4와 합치).
- **LLM 연계가 모듈화의 실익**: 채팅 컨텍스트에 바인딩된 모듈 목록·스키마를 제공하면
  "등록된 결제 모듈로 결제 연동 코드 짜줘"가 환경변수 규약에 맞는 코드로 바로 생성된다.
- **자원 아이템화 리스팅(구현 완료)**: 대화식 편집 화면에서 "이 프로젝트에서 뭘 쓸 수 있는지"를
  바인딩 여부와 무관하게 미리 볼 수 있도록 `GET /projects/{id}/resources`를 추가했다. Module에
  `category`(자유 텍스트 — "news", "llm" 등 API를 카테고리별로 묶는 용도)와 `organization_id`
  (지정 시 해당 조직 프로젝트에만 노출 — "조직별 DB" 같은 조직 전용 자원)를 추가해, 콘솔 채팅
  화면이 API(카테고리별)·DB(조직별 포함)를 아이템화해 보여준다(서버내 공유 파일은 위
  주석대로 모듈이 아니라 환경변수로 정하므로 이 리스팅에 나오지 않는다).
  실제 LLM 프롬프트 컨텍스트(`context_for_llm`)는 기존대로 **바인딩된 모듈만** 주입한다 —
  이 리스팅은 사용자가 무엇을 바인딩할지 판단하기 위한 참고용이며, LLM이 실제로 배포 시
  주입되지 않는 미바인딩 자원을 코드에서 참조하도록 유도하지 않기 위해 컨텍스트 주입 범위는
  분리해 두었다.

### 12.3 실행 결과 미리보기

- **1단계 (이미 확보)**: development 프로필 배포가 곧 미리보기다 —
  `{name}-dev.{base_domain}`에서 운영과 격리된 실행 결과를 확인(13절 구현 완료).
- **2단계 — 편집 세션별 임시 프리뷰(PreviewSession)**: 채팅에서 diff를 적용한 작업 브랜치를
  development 프로필로 빌드해 **TTL이 있는 임시 유닛**(`{name}-pv{n}.{base_domain}`)으로 기동.
  웹 UI에서 iframe 미리보기 + 실행 로그 패널을 나란히 보여주고, TTL 만료·세션 종료 시 자동 회수.
  Netlify의 Deploy Preview 개념을 development 프로필 재사용으로 구현하는 것.
- react(정적)는 빌드 산출물 디렉토리 서빙이라 프리뷰가 수 초 내로 뜨고,
  python/node는 dev 컨테이너(HMR/--reload)라 편집 반영이 즉각적이다.
- **안전장치(필수)**: 프리뷰 유닛은 리소스 상한 축소(dev 프로필의 50%), 외부 네트워크 차단
  (바인딩된 Module만 허용), TTL 기본 1시간, 동시 프리뷰 수 제한. LLM이 생성한 코드가
  실행되는 지점이므로 7절 체크리스트 중 컨테이너 격리 항목이 여기서 가장 중요해진다.

### 12.4 데이터 모델·API 확장 초안

```
LlmProvider(id, name, kind: external|internal, base_url, api_key_encrypted, model, allowed_scope)
ChatSession(id, project_id, provider_id, branch, created_at)
ProposedChange(id, session_id, diff, status: proposed|applied|rejected, applied_sha)
Module(id, name, type, config_json)            ModuleBinding(project_id, module_id, env_prefix)
PreviewSession(id, project_id, branch, url, expires_at, status)
```

```
POST /llm/providers                POST /chat/sessions
POST /chat/sessions/{id}/messages  POST /changes/{id}/apply | /reject
POST /projects/{id}/review         # diff 리뷰 요청
POST /modules  GET /modules        POST /projects/{id}/modules/{mid}/bind
POST /projects/{id}/preview        DELETE /previews/{id}
```

### 12.5 로드맵 편입

8절 로드맵 기준: Module 레지스트리와 dev 프리뷰 활용은 **Phase 2**에, LLM 프로바이더
추상화·대화식 편집·자동 리뷰는 **Phase 3**(LLM 플랫폼 구간)에, 임시 PreviewSession과
모델 라우팅 정책·리뷰 게이트는 **Phase 4**에 편입한다.

---

## 13. 구현 현황

컨트롤 플레인 초기 구현이 [`platform/`](../platform/README.md)에 있다.

- 1차(small=Docker)·2차(enterprise=Kubernetes) 런타임 모두 구현 — `PAAS_TIER` 설정으로 전환,
  `Runtime` 인터페이스 교체 구조(6.3절 규칙 2 적용)
- 빌드 옵션 **development / release** 구분: 이미지 태그(-dev), dev 서버 vs 프로덕션 빌드,
  리소스 50%/100%, replicas 1/2, 도메인 {name}-dev.* / {name}.*, 동시 기동 지원
- 무중단 배포(1차: 블루-그린+헬스체크+Caddy 전환, 2차: RollingUpdate), 롤백(이미지 태그 재기동),
  웹훅 HMAC 검증(GitHub/Gitea), EnvVar Fernet 암호화, API 키 인증, 감사 로그 포함
- **설치 빌드옵션 구현 완료**: 기능 모듈 선택(`PAAS_FEATURES` — deploy/workspace,
  core 상시) + 운영환경 OS(`PAAS_HOST_OS` — linux/macos/windows 자동 감지, GPU 기능 매트릭스),
  3-OS CI 매트릭스 포함. mail/payment/payout은 CHO-FAM Functions로 분리
- **12절 코드 워크스페이스 구현 완료**: LLM 프로바이더 추상화(외부 + `project://` 내부 해석),
  대화식 편집(diff 제안 → 승인 시에만 커밋), 코드 리뷰(심각도 분류), Module 레지스트리
  4타입(민감 config 암호화·티어별 internal_api URL 해석·배포 시 자동 주입),
  TTL PreviewSession(CPU 50%·GPU 금지·동시 5개 제한·lazy 회수)
- **zip/폴더 업로드 등록 + 코드 확인 화면 + 웹훅 자동 등록 구현 완료**(10.2절):
  `POST /projects/upload`(zip bomb/zip slip 방어), 읽기 전용 파일 트리·내용 조회 API +
  콘솔 코드 탭(수정은 여전히 채팅/diff 승인으로만), 사내 Gitea 리포 push 인증
  (`services/git_auth.py`, 리포 자체는 public), `PAAS_PLATFORM_PUBLIC_URL` 설정 시 웹훅 자동 등록
- **자원 아이템화 리스팅 + 콘솔 좌측 사이드바 전환 구현 완료**(12.2절): Module에
  `category`/`organization_id` 추가, `GET /projects/{id}/resources`로 대화식 편집 화면에
  API(카테고리별)·공유 파일·조직별 DB를 아이템화해 표시. 콘솔 상단 수평 메뉴를 왼쪽 고정
  사이드바로 재구성(`components/Layout.tsx`)
- **운영환경별 런타임/리버스프록시 선택 + 서버구성 시각화 구현 완료**(6.3절 규칙 2의
  "런타임 인터페이스 교체 구조"를 리버스프록시에도 동일하게 적용): 1차(small) 안에서도
  실행 런타임(`PAAS_RUNTIME_BACKEND`: docker | windows_service)과 리버스프록시
  (`PAAS_PROXY_BACKEND`: caddy | iis | apache)를 서로 독립적으로 선택할 수 있게 했다.
  `services/runtime/`가 Runtime 인터페이스로 Docker/K8s를 이미 추상화하고 있던 것과
  같은 원칙으로, `services/proxy/`에 `ReverseProxy` 인터페이스를 신설해 기존 Caddy
  구현을 그대로 옮기고 IIS(web.config URL Rewrite + appcmd)·Apache(VirtualHost
  mod_proxy/mod_rewrite + apachectl)를 추가했다. windows_service 런타임은 컨테이너
  이미지 대신 체크아웃된 리포 루트의 `paas-start.cmd`(PORT 환경변수로 리슨 포트 전달)를
  nssm(public domain)으로 Windows Service에 등록해 기동하며, 기존 DockerRuntime과
  동일한 -a/-b 블루-그린 패턴을 그대로 따른다. 프로젝트별 URL redirect/rewrite 규칙
  (`RedirectRule` 모델)을 추가해 배포·롤백 시 선택된 프록시 백엔드 설정에 반영되게 했고,
  `GET /server-config`로 현재 백엔드 선택과 프로젝트×프로필별 도메인·상태·리다이렉트
  규칙 수를 한 화면에서 보여준다(콘솔 "서버구성" 메뉴 — "서버구성 시각화"와 "메뉴(라우팅/
  사이트 항목)·redirect/rewrite·service 등록 관리" 요건에 대응). 런타임 상태 조회가
  실패해도(예: docker SDK 미설치) 전체 화면이 죽지 않도록 개별 사이트 단위로 예외를
  흡수해 "unknown"으로 표시한다.

---

## 14. 운영 사례 리뷰 — 좋은 점 · 개선할 점 · 추가 아이디어

두 운영 사례를 기준으로 리뷰한다. 배포 절차는 [deployment-guide.md](./deployment-guide.md) 참고.

### 14.1 사례 A — 소규모 서버리스 운영 (단계 0: Firebase/Netlify + CHO-FAM Functions)

**좋은 점**

- 고정비 ≈ 0원. 서버·SSL·배포·롤백·PR 프리뷰를 전부 관리형이 처리 — 운영 인력 사실상 불필요
- 이미 운영 중인 자산(메일·결제 수납·지급대행 Functions, x-api-key 패턴) 그대로 재사용
- 트래픽 급증 자동 대응 — 소규모의 최다 장애 원인인 "서버 죽음"이 구조적으로 없음

**개선할 점** (우선순위순)

| # | 항목 | 조치 |
| --- | --- | --- |
| 1 | **비용 폭주 가드 부재** — 종량제에서 무한 루프·봇 트래픽 = 청구 폭탄 | GCP 예산 알림 + Functions `maxInstances` 상한 (즉시) |
| 2 | 콜드스타트 — 결제 confirm 등 지연 민감 경로 첫 호출 수 초 | 해당 함수만 min instances 1 |
| 3 | 스테이징 부재 — 운영에서 직접 실험하게 됨 | Firebase 프로젝트 dev/prod 분리 |
| 4 | 관측성 분산 — 로그가 GCP 콘솔 곳곳에 | 구조화 로그 + 실패 시 메일 알림 일원화 |
| 5 | 벤더 종속 | Express 라우터 분리 구조 유지 + Firestore 접근을 서비스 계층 뒤에 |

**추가 아이디어**

- PR 열릴 때 GitHub Actions로 `firebase hosting:channel:deploy` 자동 실행 — 서버리스판 PR 프리뷰 자동화
- **Cloud Run 다리 전략**: FastAPI 워크로드를 Cloud Run으로 수용 — 플랫폼의 이미지 태그 빌드와
  호환되므로 단계 1(자체 PaaS) 전환 시 이미지를 그대로 이관 가능

### 14.2 사례 B — 대규모 풀버전 기업용 (2차: K8s + 전 모듈)

**좋은 점**

- 컨트롤 플레인 유지 + 실행 계층만 K8s 위임 → **재작성 없는 확장** (6.3절 규칙의 결실)
- 전 스택 무료·상용 무제한 라이선스 — 사용자 수가 늘어도 라이선스비 0원 (10절)
- **내부 LLM 모드 = 데이터 주권** — 소스가 사외로 나가지 않는 코드 워크스페이스는 보안 심사의 결정적 차별점
- 감사 로그가 1차부터 스키마에 있어 컴플라이언스 요구가 UI 작업으로 축소
- 기능 모듈 설치 옵션으로 부서별 맞춤 설치 (예: 배포 전용 부서는 workspace 끔)

**개선할 점** (현재 구현 ↔ 기업 요구 갭, 우선순위순)

| # | 갭 | 조치 | 상태 |
| --- | --- | --- | --- |
| 1 | 인증이 API 키 단일 체계 | Keycloak OIDC/RBAC — Bearer JWT·JWKS·롤 매핑, API 키 병행 | ✅ 구현됨 |
| 2 | 배포가 동기 실행 | 작업 큐 — `wait=false` 202 + 백그라운드 실행 (arq 교체 가능한 submit 경계) | ✅ 구현됨 |
| 3 | DB 기본 SQLite | Alembic 마이그레이션 체계 (PostgreSQL 전환 절차 README) | ✅ 구현됨 |
| 4 | K8s apply 실패 시 조용한 파일 폴백 | 접근 불가만 폴백, apply 실패는 3회 재시도 후 에러 표면화 + **GitOps(ArgoCD) 모드**(`PAAS_K8S_GITOPS_REPO` — 매니페스트 커밋·푸시) | ✅ 구현됨 |
| 5 | 시크릿 Fernet 단일 키 | OpenBao KV v2 키 로드 + **키 회전**(`PAAS_FERNET_KEYS_OLD` 병행 복호화, `POST /admin/rotate-secrets` 재암호화) | ✅ 구현됨 |
| 6 | 멀티테넌시 격리 없음 | 유닛별 NetworkPolicy + **네임스페이스 ResourceQuota·LimitRange**(`PAAS_K8S_QUOTA_*`) | ✅ 구현됨 |
| 7 | 외부 API 디렉터리 조회 재시도 없음 | 네트워크 오류 한정 백오프 재시도 + **호스트별 서킷브레이커**(연속 5회 실패 → 60초 차단, half-open 복구) | ✅ 구현됨 |

**추가 아이디어**

- **LLM 리뷰 게이트 승격**: 심각도 high 발견 시 release 배포 차단 — 12절 설계를 기업 품질 게이트로
- **비용 미터링/차지백**: 프로젝트별 CPU·GPU 시간 집계 → 부서 과금 리포트
- GPU 예약제: VRAM 사전 검사(3.5절) + 시간대별 GPU 공유 스케줄
- 사내 표준 스캐폴드: 조직 표준 Dockerfile·CI 포함 프로젝트 생성기
- DR 자동화: DB·Fernet 키 백업 + 복구 리허설, 헬스체크 기반 상태 페이지 자동 생성

---

## 15. 데이터 유출 보안 점검 및 조치

"자료가 외부로 유출되지 않는가"를 코드 레벨(리포 공개 설정이 아니라 실제 네트워크 전송 경로)로
점검했다. GitHub 리포(`hs9147/chofam`)는 private로 확인됨. 코드 감사에서 발견된 실질적 위험
3건은 전부 수정 완료했다.

| # | 위험 | 조치 | 파일 |
| --- | --- | --- | --- |
| 1 | GitOps 매니페스트에 프로젝트 시크릿(EnvVar·Module `*_API_KEY`/`*_DSN`)이 평문으로 포함되어 admin이 설정한 외부 git 리포에 그대로 커밋·푸시됨 — 키 회전 후에도 과거 커밋에 영구히 남음 | `RuntimeSpec.secret_keys`로 시크릿 키를 별도 추적, K8s 매니페스트에서 `Secret`(stringData) 오브젝트로 분리. **GitOps 푸시에서는 Secret을 완전히 제외**하고 로컬 전용 파일(`{unit}-secrets.local.yaml`, git 미대상)에만 기록 — 클러스터 반영은 운영자가 별도 채널(kubectl apply, External Secrets Operator 등)로 수행 | `services/deployer.py`(`secret_env_keys`), `services/runtime/base.py`, `services/runtime/k8s_runtime.py` |
| 2 | `LlmProviderKind.internal`이 라벨일 뿐 코드로 강제되지 않음 — admin이 실수로 `kind=internal` + 외부 URL을 등록해도 그대로 통과, "소스가 사외로 나가지 않는다"는 12절 주장이 코드로 보장되지 않음 | `LlmProviderCreate`에 `model_validator` 추가: `kind=internal`이면 `base_url`이 반드시 `project://`로 시작해야 등록 허용(그 외 422 거부) | `app/schemas.py` |
| 3 | 채팅/리뷰 API가 `require_api_key`(비관리자 포함 전체 키)로만 보호되어, 일반 키 하나로 임의 프로젝트 + 임의(외부 포함) 프로바이더를 조합해 소스를 외부 LLM으로 보낼 수 있었음 | 세션 생성(`/chat/sessions`)·리뷰(`/projects/{id}/review`) 시점에 provider가 `external`이면 admin 키만 허용(403). `internal` 프로바이더는 계속 일반 키에 열어둠(사내망을 벗어나지 않으므로) | `app/api/llm.py`(`_require_admin_for_external`) |

부수 하우스키핑: 실수로 커밋된 `platform/delete.txt`(pip freeze 덤프) 제거, 루트·`platform/.gitignore`에
`.pytest_cache/` 추가.

검증: pytest 107건 통과(신규 10건 — Secret 분리·GitOps 히스토리 무유출·internal 스킴 강제·
admin 게이트 4종), 콘솔 `tsc`+`vite build`+vitest 통과.
