# chofam cloud platform (내부 PaaS)

React · HTML(정적) · Python(FastAPI) · Streamlit · Node · LLM 앱을 배포·운영하는 자체 Deploy Server.
설계 배경과 검토 내용은 [docs/cloud-platform-paas-design-review.md](../docs/cloud-platform-paas-design-review.md) 참고.

## 두 가지 티어 (동일 컨트롤 플레인, 실행 계층만 교체)

| | 1차 — 중소규모 (`PAAS_TIER=small`) | 2차 — 대기업 규모 (`PAAS_TIER=enterprise`) |
| --- | --- | --- |
| 런타임 | Docker Engine (블루-그린 교체 + 헬스체크) | Kubernetes (Deployment/Service/Ingress 생성) |
| 도메인·SSL | Caddy 사이트 파일 + 무중단 reload | Ingress + cert-manager |
| 무중단 배포 | 새 컨테이너 기동 → 헬스체크 → Caddy 전환 → 구 컨테이너 제거 | RollingUpdate(maxUnavailable 0) |
| replicas | 항상 1 | release 2 / development 1 |
| 클러스터 미접근 시 | — | 매니페스트를 `data/k8s-manifests/`에 출력 (kubectl apply / GitOps 연계) |

런타임은 `Runtime` 인터페이스(`app/services/runtime/base.py`)로 추상화되어 있어
`DockerRuntime` ↔ `K8sRuntime` 교체가 설정 한 줄입니다.

## 빌드 옵션: development / release

배포 시 `profile`로 지정합니다. 생략하면 프로젝트의 `default_profile`(기본 release).

| 효과 | development | release |
| --- | --- | --- |
| 실행 방식 | dev 서버 (Vite HMR, uvicorn `--reload`) | 프로덕션 빌드 (minify, 멀티스테이지, non-root) |
| 이미지 태그 | `{name}:{sha}-dev` | `{name}:{sha}` |
| 환경변수 | `APP_ENV/NODE_ENV=development` | `production` |
| URL (1차/small) | `{base_domain}/apps/{조직 또는 "_"}/{name}~dev/` | `{base_domain}/apps/{조직 또는 "_"}/{name}/` |
| URL (2차/enterprise) | `{name}-dev.{base_domain}` | `{name}.{base_domain}` |
| 리소스 | release의 50% | 100% |
| replicas (k8s) | 1, Recreate | 2, RollingUpdate |
| LLM(vLLM) | `--enforce-eager`, VRAM 50% | VRAM 90% |

dev와 release는 별도 유닛(`paas-{name}-dev` / `paas-{name}`)으로 **동시 기동**되므로
개발 확인용 배포가 운영 트래픽에 영향을 주지 않습니다.

Dockerfile 결정 규칙: 리포에 `Dockerfile`이 있으면 우선 사용(`--build-arg APP_PROFILE` 전달),
없으면 `templates/dockerfiles/{type}.{profile}.Dockerfile` 템플릿 사용.

프로젝트 `type`별 템플릿 요약 (`app/models.py ProjectType`):

| type | 내부 포트 | 실행 방식 |
| --- | --- | --- |
| `react` | dev 3000 / release 80 | dev: Vite HMR, release: 빌드 산출물을 Caddy로 정적 서빙 |
| `html` | 80 (dev/release 동일) | 빌드 단계 없이 리포 내용을 그대로 Caddy로 정적 서빙 |
| `python` | 8000 | FastAPI, `app.main:app`을 uvicorn으로 실행(dev `--reload`) |
| `streamlit` | 8501 | 리포 루트 `app.py`를 `streamlit run`으로 실행(dev `--server.runOnSave`) |
| `node` | 3000 | `npm run dev` / `npm start` |
| `llm` | 8000 | vLLM OpenAI 호환 서버(GPU) |

## 설치 빌드옵션

설치본마다 **기능 모듈**과 **운영환경(OS)** 두 축을 조합해 구성합니다.

### 기능 모듈 (`PAAS_FEATURES`, 기본 `deploy,workspace`)

| 모듈 | 내용 | 필요 설정 |
| --- | --- | --- |
| core (항상) | 프로젝트·환경변수·API 키·감사 로그·Module 레지스트리·/status | — |
| `deploy` | 배포·롤백·로그·웹훅 자동 배포·프리뷰 | Docker 또는 K8s |
| `workspace` | 에이전트 기획 (프로바이더·기획 세션·작업 지시·사용 검증·리뷰) | LLM 프로바이더 |

```bash
PAAS_FEATURES=deploy                          # 배포 전용 서버
PAAS_FEATURES=workspace                       # 코드 워크스페이스 전용
PAAS_FEATURES=deploy,workspace                # 전체 (기본)
```

비활성 모듈의 엔드포인트는 404로 감춰지고, 콘솔 메뉴도 `/health`의 features에 맞춰 숨겨집니다.
메일과 결제 수납·지급대행은 이 Platform 프로세스에서 분리되어 CHO-FAM Functions의
`/api/mail`, `/api/payments`, `/api/payout`이 담당합니다. 관리 화면은 각각
`/admin/mail/`, `/admin/payment/`에 있습니다. 업그레이드 호환을 위해 예전
`PAAS_FEATURES`의 `mail`, `payment` 값은 오류 없이 무시됩니다.

### 운영환경 OS (`PAAS_HOST_OS=auto`, 자동 감지)

| OS | 컨테이너 런타임 | GPU | 비고 |
| --- | --- | --- | --- |
| **Linux (운영 권장)** | Docker Engine (Apache 2.0, 무료) | NVIDIA Container Toolkit | 전 기능 |
| macOS | Colima(무료) 권장, Docker Desktop은 기업 유료 주의 | ❌ 미지원 — LLM은 CPU(Ollama) 또는 원격 GPU | 개발·데모용 |
| Windows | Docker Desktop + WSL2 | WSL2 백엔드 경유 | **가능하면 WSL2 안에서 Linux 모드 운영 권장** |

- GPU 미지원 OS에서 llm 프로젝트를 배포하면 GPU 없이 기동하고, GPU를 명시 요구하면
  한국어 에러로 조기 실패합니다 (`PAAS_FORCE_GPU=true`로 강제 가능).
- 감지가 틀리는 환경(컨테이너 등)은 `PAAS_HOST_OS=linux`처럼 명시하세요.
- CI가 3-OS 매트릭스(ubuntu·macos·windows)로 전체 테스트를 돌립니다
  (`.github/workflows/platform-ci.yml`).

## 실행

```bash
cd platform
pip install -r requirements.txt
pip install docker            # 1차(small) 런타임
# pip install kubernetes      # 2차(enterprise)에서 직접 apply할 때
cp .env.example .env          # 값 채우기
uvicorn app.main:app --port 7000
```

Caddy는 메인 Caddyfile에 `import ./data/caddy-sites/*.caddy` 한 줄만 추가하면 됩니다.

### Docker Compose로 실행 (옵션)

백엔드+콘솔을 이미지 하나로 묶어 `docker compose up`으로 기동할 수도 있습니다
(`Dockerfile`, `docker-compose.yml`). Linux 호스트(또는 WSL2) 전용 — 배포된 프로젝트
컨테이너와의 `127.0.0.1` 헬스체크 전제 때문에 `network_mode: host`가 필요하고, 이는
Docker Desktop(macOS/Windows 네이티브)에서 지원되지 않습니다. 호스트 Docker 데몬으로
프로젝트를 빌드·기동하기 위해 `/var/run/docker.sock`도 마운트합니다("docker outside of
docker"). 상세 설정은 [docs/deployment-guide.md §3.2b](../docs/deployment-guide.md#32b-docker-compose로-설치-옵션) 참고.

```bash
cp .env.example .env
docker compose up -d --build
```

### 콘솔 자기 배포 (옵트인)

`PAAS_SELF_DEPLOY_CONSOLE=true`면 위 정적 마운트 대신, 백엔드가 기동할 때 콘솔을
`admin` 조직 소속 `paas-console`이라는 일반 `react` Project(`source_subdir=platform/console`)로
등록해 플랫폼 자신의 배포 파이프라인(build_image → DockerRuntime → 리버스프록시)으로
띄웁니다 — `services/self_deploy.py`. `https://{base_domain}/apps/admin/paas-console/`에서
접속하며, 최초 1회만 자동 배포합니다. 상세 설정은
[docs/deployment-guide.md §3.2c](../docs/deployment-guide.md#32c-콘솔을-배포-파이프라인으로-자기-배포-옵트인) 참고.

## API 요약 (인증: `x-api-key` 헤더)

모든 엔드포인트는 `/paas` 아래 마운트된다(여러 내부 서비스가 게이트웨이를 공유할 때
경로로 구분하기 위함). 그 안에서 `/health`, `/status`, `/webhooks/git`은 버전 없이
`/paas/...`만 받고, 나머지는 `/paas/api/v1/...`다 — `/health`·`/status`는 로드밸런서/
k8s probe·콘솔 로그인 프로브가 버전과 무관한 고정 경로를 기대하기 때문이고,
`/webhooks/git`은 외부(Gitea/GitHub)가 한 번 등록해두는 콜백 URL이라 API 버전이
올라가도 깨지면 안 되기 때문이다(`app/main.py`의 `PAAS_PREFIX`/`API_PREFIX` 참고).

```
GET  /paas/health                     # 인증 불필요, 버전 prefix 없음
GET  /paas/status                     # CPU/메모리/디스크/GPU (admin), 버전 prefix 없음
POST /paas/api/v1/keys                     # API 키 발급 (admin)
GET  /paas/api/v1/audit                    # 감사 로그 (admin)

POST /paas/api/v1/orgs                     # {name} → 사내 Gitea에 동명 Organization 생성 (admin)
GET  /paas/api/v1/orgs                     # 조직 목록 + 프로젝트 수
POST /paas/api/v1/orgs/sync                # {on_missing_repo?: create(기본)|delete} — Gitea 기준 동기화 (admin)
                                      #   Gitea에만 있는 조직/리포는 가져오고(type은 시그니처 파일로 추론),
                                      #   플랫폼에만 있는(조직 소속) 프로젝트는 on_missing_repo대로 리포 재생성/프로젝트 삭제

GET  /paas/api/v1/projects                 # git_url은 관리자·그 프로젝트 조직 소속 계정에게만 노출
                                      #   (전역 프로젝트는 누구나) — 그 외 응답에는 마스킹
POST /paas/api/v1/projects                 # {name, type, branch, ...}
                                      #   + organization_id(내부 리포 자동 생성) 또는 git_url(직접 지정) 중 하나
POST /paas/api/v1/projects/upload          # multipart: zip_file 또는 files[](폴더) 중 하나 + organization_id 필수
                                      #   업로드 내용을 사내 Gitea 신규 리포에 최초 push (대용량/zip bomb/zip slip 방어)
                                      #   deploy_after_upload=true면 push 직후 배포 큐에 등록(원클릭)
GET  /paas/api/v1/projects/{id}/files      # 읽기 전용 파일 트리 (workspace 기능)
GET  /paas/api/v1/projects/{id}/files/content?path=  # 읽기 전용 파일 내용 — 수정 엔드포인트 없음(구현은 외부 빌더)
GET  /paas/api/v1/projects/{id}/codemap    # 코드 구조 트리(파일→클래스/함수+요약, 정적 파싱)
POST /paas/api/v1/projects/{id}/deploy     # {profile?: development|release, git_sha?}
POST /paas/api/v1/projects/{id}/rollback?profile=release
POST /paas/api/v1/projects/{id}/stop?profile=development
GET  /paas/api/v1/projects/{id}/deployments
GET  /paas/api/v1/projects/{id}/logs?profile=release&tail=200
GET  /paas/api/v1/projects/{id}/status
PUT  /paas/api/v1/projects/{id}/env        # {key, value, is_secret} — Fernet 암호화 저장
GET  /paas/api/v1/projects/{id}/env        # 시크릿 값은 마스킹

GET  /paas/api/v1/server-config            # 서버구성 시각화 — 런타임/프록시 백엔드 + 프로젝트별
                                      #   (프로필별) 도메인·상태·리다이렉트 규칙 수
POST /paas/api/v1/projects/{id}/redirects  # {from_path, to_path, kind: redirect|rewrite, status_code?}
                                      #   다음 배포·롤백부터 리버스프록시 설정에 반영
GET  /paas/api/v1/projects/{id}/redirects
DELETE /paas/api/v1/redirects/{id}

POST /paas/webhooks/git             # GitHub/Gitea push → default_profile로 자동 배포
                                      # HMAC 서명(X-Hub-Signature-256 / X-Gitea-Signature) 필수
```

## 배포 흐름 예시

```bash
ADMIN=... BASE=http://localhost:7000/paas/api/v1

# 프로젝트 등록
curl -X POST $BASE/projects -H "x-api-key: $ADMIN" -H 'content-type: application/json' \
  -d '{"name":"front","type":"react","git_url":"https://git.example.com/org/portal-front"}'

# development 배포 → {base_domain}/apps/_/front~dev/ (organization_id로 등록했다면 /apps/_/ 대신 /apps/{조직}/)
curl -X POST $BASE/projects/1/deploy -H "x-api-key: $ADMIN" \
  -H 'content-type: application/json' -d '{"profile":"development"}'

# release 배포 → {base_domain}/apps/_/front/
curl -X POST $BASE/projects/1/deploy -H "x-api-key: $ADMIN" \
  -H 'content-type: application/json' -d '{"profile":"release"}'

# 롤백 (재빌드 없이 직전 성공 이미지로)
curl -X POST "$BASE/projects/1/rollback?profile=release" -H "x-api-key: $ADMIN"
```

## 코드 워크스페이스 (설계 문서 12절)

```
POST /paas/api/v1/llm/providers                     # 외부(Claude/OpenAI) 또는 내부(project://<llm 프로젝트>) 등록 (admin)
GET  /paas/api/v1/llm/providers                     # api_key는 has_api_key로만 노출
POST /paas/api/v1/plan/sessions                     # {project_id, provider_id, branch?} — 기본 브랜치 paas/plan-{id}-{hex}
GET  /paas/api/v1/plan/sessions?project_id=         # 세션 이력(최근 순) — 확정 단계·작업 수 요약
GET  /paas/api/v1/plan/sessions/{id}/messages       # 재개용 대화 이력 복원
DELETE /paas/api/v1/plan/sessions/{id}              # 세션·대화·산출물 포인터·작업 지시 + 작업 브랜치 삭제(머지된 문서는 남음)
POST /paas/api/v1/plan/sessions/{id}/stages/{stage}/messages   # 단계 생성 요청 — git 파일 목록·앞 단계 확정본이 컨텍스트
                                                # 단계는 spec → architecture → solution → principles → tasks(5단계)
                                                # tasks 단계는 대화로 쓰지 않는다 — 409, tasks/generate로 산출물을 만든다
                                                # 빈 응답(컨텍스트 한도 초과)이면 413 — compact=true로 압축 재시도
                                                # solution 단계에서는 bind_module 도구로 사용 결정 모듈을 즉시 바인딩하고,
                                                # 바인딩된 mcp 모듈의 도구({모듈명}__{도구명})로 실제 규격을 확인
POST /paas/api/v1/plan/sessions/{id}/stages/{stage}/confirm    # 확정 → Gitea 커밋 후 git 상태에 따라 PR·머지 자동
                                                # 리포에 다른 내용의 같은 문서가 있으면 412 — overwrite=true로 재요청
POST /paas/api/v1/plan/sessions/{id}/tasks/generate # 확정 산출물 → 외주 빌드 작업 지시(work order)
                                                # ⑤ tasks 단계 산출물(05-작업지시.md)은 이 목록을 렌더한 문서
GET  /paas/api/v1/plan/sessions/{id}/tasks          # 작업 지시 목록·상태
POST /paas/api/v1/plan/sessions/{id}/tasks/sync     # 진행 현황을 기본 브랜치 기준으로 갱신
                                                # 보고된 커밋이 기본 브랜치에 도달 가능할 때만 완료
PATCH /paas/api/v1/plan/tasks/{id}                  # {status?, note?, commit_sha?}
POST /paas/api/v1/plan/sessions/{id}/merge          # 세션 마무리 — 작업 브랜치를 기본 브랜치로 반영
GET  /paas/api/v1/plan/projects/{id}/constraints    # 가용 모듈 제약(외부 빌드 guardrail)
GET  /paas/api/v1/plan/projects/{id}/compliance     # LLM·모듈 사용 검증 + 외주 빌더 전달용 수정 지시
POST /paas/api/v1/plan/projects/{id}/mcp            # 외부 빌드 도구용 MCP 서버(JSON-RPC 2.0)
POST /paas/api/v1/projects/{id}/review              # {provider_id, diff? , base_ref?} → 심각도 분류 findings

POST /paas/api/v1/modules                           # external_api | internal_api | database | mcp | llm
                                                #   + category?(예: news, llm — API 카테고리별 그룹핑)
                                                #   + organization_id?(지정 시 해당 조직 프로젝트에만 노출)
                                                # config의 api_key/dsn/secret 등은 Fernet 암호화 저장
GET  /paas/api/v1/modules/search?keyword=&category=  # 외부 API 디렉터리 검색 (admin, 아웃바운드 조회)
                                                #   두 조건은 AND, 각각 비우면 안 건다(카테고리 기본=전체)
                                                #   category=기타 → 카테고리가 없는 항목만
GET  /paas/api/v1/modules/search/categories        # 카테고리 선택지 + 개수 (디렉터리에 실제로 있는 값)
GET  /paas/api/v1/mcp/search?q=                     # 사내 MCP 서버 검색 — 이 플랫폼이 노출하는 것만
POST /paas/api/v1/modules/import-mcp                # 검색 결과를 mcp 모듈로 원클릭 등록
POST /paas/api/v1/modules/import                    # 검색 결과를 external_api 모듈로 자동 추가 (admin, 이름 정규화)
POST /paas/api/v1/projects/{id}/modules/{mid}/bind  # {env_prefix: "PAY"} → 배포 시 PAY_URL 등 자동 주입
GET  /paas/api/v1/projects/{id}/modules             # LLM 컨텍스트용 A2A Agent Card 목록
                                                #   (비밀값 제외, 바인딩된 모듈만)
GET  /paas/api/v1/projects/{id}/resources           # 대화식 편집 화면 자원 리스팅 — 바인딩 여부와 무관하게
                                                #   이 프로젝트에서 쓸 수 있는 모든 모듈을 카테고리별로 아이템화

GET  /paas/api/v1/a2a/agents                        # 등재된 에이전트 카드 목록 (?type= ?category= ?project_id=)
GET  /paas/api/v1/a2a/agents/{name}/card            # 단일 카드
POST /paas/api/v1/a2a/agents/{name}/task            # {capability, input} → 대상 에이전트로 중계

GET  /paas/api/v1/storage/stores                    # 열려 있는 저장소 목록(이름·경로·읽기 전용 여부)
GET  /paas/api/v1/storage/{저장소}/files             # 파일 목록 + 창구 URL
GET  /paas/api/v1/storage/{저장소}/files/content     # ?path= 다운로드
POST /paas/api/v1/storage/{저장소}/files             # multipart {file, path?} 업로드 (읽기 전용이면 403)
DELETE /paas/api/v1/storage/{저장소}/files           # ?path= 삭제 (읽기 전용이면 403)

POST /paas/api/v1/mcp/ops                           # 사내 MCP 서버 — 운영 조회(배포 상태·로그·라우팅·호스트·감사)
POST /paas/api/v1/mcp/code                          # 사내 MCP 서버 — 코드 조회(project 인자로 프로젝트 선택)
POST /paas/api/v1/mcp/docs                          # 사내 MCP 서버 — 사내 문서 본문 검색(저장소를 가로질러)
                                                #   list_sources·search_docs·read_doc·reindex_docs·index_status
POST /paas/api/v1/mcp/storage/{저장소}              # 사내 MCP 서버 — 저장소 파일(루트 밖으로 못 나감)
                                                #   list_files·read_file + search_docs·reindex_docs·index_status
                                                #   문서 폴더는 기본 읽기/쓰기 — PAAS_DOC_ROOTS_READONLY에 적은 폴더만 쓰기·삭제 도구가 빠짐
                                                #   플랫폼 저장소(internal)는 서버 목록에 안 나오지만 주소로는 닿음
POST /paas/api/v1/mcp/db/{module}                   # 사내 MCP 서버 — database 모듈 조회(SELECT 전용)
                                                #   PAAS_MCP_DB_MODULES에 이름이 있는 모듈만 열림(기본 전부 차단)

POST /paas/api/v1/projects/{id}/preview             # {branch?, ttl_minutes=60} → {name}-pv{n}.{base_domain}
GET  /paas/api/v1/projects/{id}/previews            # 조회 시 만료 프리뷰 자동 회수
DELETE /paas/api/v1/previews/{id}
```

- **A2A 게이트웨이 — 사내 자원을 에이전트로 추상화**: 모듈 레지스트리에 등록한 DB·API·
  파일저장소·MCP 서버는 타입에 따라 호출 가능한 능력(`skills`)을 갖는 Agent Card로
  정규화되어(`services/a2a.py`) `/a2a/agents`에서 열거됩니다. 호출은 반드시 게이트웨이를
  거치며 **호출자는 대상의 자격증명을 보지 못합니다** — 게이트웨이가 복호화해
  `Authorization`에 싣고, 호출자 신원을 `x-paas-calling-agent`로 전달한 뒤 감사 로그에
  남깁니다. 같은 카드가 에이전트 기획의 **가용 모듈 제약**으로 정리되어 외부 빌더에게
  전달되므로, 외부 빌더는 카드에서 본 이름으로만 게이트웨이를 통해 다른 에이전트를 호출합니다.
  현재 카드는 자체 규약입니다 — 표준 A2A 클라이언트가 붙으려면 `/.well-known/agent-card.json`
  공개와 JSON-RPC `message/send` 수용이 남아 있습니다(미구현).
- **파일 저장소는 환경변수로 정하고, MCP와 창구로 다룹니다**: 저장소는 모듈로 등록하지
  않습니다 — `PAAS_STORAGE_ROOT`가 내부 저장소 하나(이름 `internal`, 쓰기 가능),
  `PAAS_DOC_ROOTS`가 사내 문서 폴더를 정합니다(기본 읽기 전용). 디스크 경로는 서버를 설치한
  사람이 이미 아는 사실이지 콘솔에서 등록할 일이 아니고, 모듈로 두면 같은 폴더가 이름만
  달리 두 번 등록되거나 존재하지 않는 경로가 등록돼도 열어 보기 전까지 아무도 모릅니다.
  `PAAS_DOC_ROOTS`는 쉼표로 여러 개를 받고 각 항목은 `이름=경로` 또는 경로만 씁니다.
  저장소 이름은 소문자·숫자·하이픈만 쓸 수 있고(URL 조각이자 색인 파일 이름이며, 모듈로
  가져올 때 모듈 이름이 됩니다 — IIS/ARR 서브패스 뒤라 경로에 한글을 넣지 않습니다),
  경로만 주면 마지막 폴더 이름에서 만들어 봅니다(`Company Docs` → `company-docs`).
  만들 수 없으면(한글 폴더 등) 이름을 직접 쓰라고 알려 줍니다. 윈도우 경로를 감싼
  따옴표(굽은 것 포함)는 벗기고, 경로 안 공백은 그대로 둡니다:

  ```
  PAAS_STORAGE_ROOT=D:\paas\data\storage
  PAAS_DOC_ROOTS=rules=D:\공유\사내규정,costdb=D:\cost db
  PAAS_DOC_ROOTS_READONLY=rules         # 잠글 폴더만 (기본: 전부 읽기/쓰기)
  # PAAS_STORAGE_ROOT는 플랫폼 자신의 저장소입니다 — 파일 관리 화면과 MCP 서버
  # 목록에는 안 나오고, 이름(internal)으로는 그대로 닿습니다.

  삭제는 완전 삭제가 아니라 저장소 안 `.trash`로 옮기는 것입니다 — 목록·검색에서는 곧바로
  빠지지만 파일은 남아 있어 되돌릴 수 있습니다. 사내 공유 폴더에는 되돌리기가 없고
  (서비스 계정이 SMB로 지우면 윈도우 휴지통에 가지 않습니다) 이 경로는 LLM도 부릅니다.
  ```

  접근 창구는 셋입니다 — LLM은 `/mcp/docs`(본문 검색)와 `/mcp/storage/{저장소}`,
  사람은 콘솔의 **파일 관리** 화면(= `/paas/api/v1/storage/{저장소}`)입니다. 경로 탈출과
  절대 경로는 모두 거부됩니다(`services/storage.py`). 사내망 전제라 앱마다 별도
  자격증명을 발급하지 않는 대신, 업로드·다운로드·삭제는 모두 호출 주체(`x-api-key`의 키
  이름 또는 OIDC `preferred_username`)와 함께 감사 로그에 남습니다. 설정이 잘못돼 있으면
  조용히 빼지 않고 어느 항목이 문제인지 그대로 알려 줍니다 — 목록에서 사라지면 "그 폴더에
  문서가 없다"와 구분되지 않습니다.
- 내부 LLM 프로바이더(`project://llm-main`)를 쓰면 소스가 사내망을 벗어나지 않습니다.
- internal_api 모듈과 `project://` LLM 프로바이더의 URL은 티어에 따라 자동
  해석됩니다(small: target 프로젝트의 실제 배포 URL과 동일한 서브패스 —
  `https://{base_domain}/{조직 또는 "_"}/{target}/`, enterprise: `http://paas-{target}.{ns}.svc`).
- 프리뷰는 development 프로필 빌드를 재사용하되 CPU 50%·GPU 금지·동시 5개 제한·TTL 회수가 걸립니다.
- **코드 구조 시각화**: 콘솔 "코드 확인" 화면에서 정적 파싱(Python `ast`,
  JS/TS 정규식)으로 만든 파일→클래스/함수 계층 트리를 확대/축소로 확인할 수 있고,
  **같은 개요가 에이전트 기획의 LLM 컨텍스트에도 주입**되어 전체 구조·항목별 기능 요약을
  참조해 문서를 작성합니다(`services/codemap.py`, `GET /projects/{id}/codemap`).
- **외부 API 검색 → 모듈 자동 추가**: 수집해 둔 API 카탈로그(`api_catalog` 표)를
  키워드·카테고리·소스로 검색해 선택 결과를 external_api 모듈로 바로 추가합니다.
  키워드는 이름·설명·카테고리와 **주소**(홈페이지·스펙 URL)에 걸립니다 — 받아 둔 URL을
  붙여 넣어 그게 무슨 API였는지 되짚을 수 있습니다. 이름은 모듈 규약으로 자동 정규화
  (`services/apisearch.py`).

  **검색은 표만 읽습니다**(아웃바운드 없음) — 그래서 같은 검색을 사내 MCP 서버
  `/mcp/apis`로도 열어 두었습니다. 밖으로 나가는 것은 수집뿐이고, 수집은 하루 한 번
  자동으로 돌거나 `POST /modules/search/refresh`(admin, 콘솔의 "수집" 버튼)로 당깁니다.
  `/mcp/apis`에도 `sync_catalog` 도구가 있어 **요청이 있을 때 최신화**할 수 있습니다
  (`source`로 공공데이터만 골라 받을 수 있음). 이 도구가 넓히는 것은 권한이 아니라
  호출 횟수뿐이라 — 목적지는 환경변수가 정하고 쓰는 곳은 카탈로그 표 하나입니다 —
  소스마다 최소 간격(5분)을 두고, 그 안에 다시 부르면 받지 않고 `skipped`로 답합니다.
  사람이 누르는 수집에는 간격을 걸지 않습니다.

  소스는 둘입니다:

  ```
  PAAS_API_DIRECTORY_URL   # apis.guru(기본값 있음) — 사내 미러로 교체 가능
  PAAS_PUBLIC_DATA_URL     # 국내 공공데이터 카탈로그 (기본 비어 있음 = 아예 안 부름)
  PAAS_PUBLIC_DATA_KEY     # 위 주소의 인증키 — serviceKey로 실립니다
  ```

  공공데이터포털(data.go.kr) 계열을 붙일 때 알아 둘 것:

  - 주소에 적은 질의(`pageNo` 등)는 그대로 살아갑니다. `_type=json`과 `numOfRows`는
    비어 있을 때만 채워 넣습니다 — 이 계열은 `_type`을 안 주면 XML을 주고 `numOfRows`
    기본값이 10입니다.
  - 인증키는 포털이 인코딩된 것/아닌 것 두 벌로 줍니다. **어느 쪽을 넣어도 됩니다** —
    한 번 풀어서 실으므로 이중 인코딩(`%2B` → `%252B`)으로 "등록되지 않은 키"가 되는
    일은 없습니다.
  - **한 번에 한 페이지만 받습니다.** 카탈로그가 `numOfRows`보다 크면 주소에
    `numOfRows`를 키워 적으세요(페이지 순회는 아직 없습니다).
  - 응답 형식은 카탈로그마다 다릅니다. 목록은 어느 깊이에 있든 찾고 필드 이름은 후보를
    훑지만, 못 알아보면 **받은 최상위 키를 적어 실패합니다** — 그 메시지를 보고 후보를
    늘리면 됩니다.
- **mcp 모듈 — MCP(Model Context Protocol) 서버**: `config: {url, api_key?}`로
  등록하면 다른 모듈처럼 배포 앱에 `{PREFIX}_URL`/`{PREFIX}_API_KEY`가 주입되고,
  가용 모듈 제약에 실려 외부 빌더가 게이트웨이 경유로 쓸 수 있게 됩니다.
  `services/mcp_client.py`(JSON-RPC 2.0 tools/list·tools/call, 단일 JSON 응답
  트랜스포트만 지원 — SSE 스트리밍은 범위 밖)가 플랫폼이 MCP **클라이언트**로 붙는 경로이며,
  **에이전트 기획의 솔루션 구성 단계**에서 바인딩된 MCP 서버의 도구를 모델에 넘겨
  직접 호출하게 합니다(서버 간 이름 충돌은 `{모듈명}__{도구명}`으로 구분, 응답하지 않는
  서버는 조용히 빠짐). 반대 방향 — 외부 빌드 도구가 붙는 MCP **서버**는
  `/plan/projects/{id}/mcp`입니다. 도구 목록(`tools/list`)은 60초 캐시로 감싸므로
  솔루션 단계 매 턴마다 바인딩된 서버 전부에 왕복하지 않고, 응답하지 않는 서버의
  타임아웃도 턴마다 다시 기다리지 않습니다.
- **사내 MCP 서버 — 플랫폼이 가진 것을 도구로 노출**(`api/mcp_servers.py`): 운영
  조회(`/mcp/ops`), 코드 조회(`/mcp/code`), 파일 저장소
  (`/mcp/storage/{모듈}`), DB 조회(`/mcp/db/{모듈}`) 네 개입니다. 공개 레지스트리의
  동종 서버는 대부분 벤더 호스팅 원격 엔드포인트(소스·운영 데이터가 사외로 나감)이거나
  stdio 전용(이 플랫폼 클라이언트로는 통신 불가)이라 사내에서 만든 것입니다. 이 주소를
  `mcp` 타입 모듈로 등록해 프로젝트에 바인딩하면 기획 솔루션 구성 단계 대화가 도구로
  씁니다 — 플랫폼 자신의 주소이므로 **사내에서 실제로 닿는 주소**를 넣습니다(공개
  도메인이 이 플랫폼으로 라우팅되지 않는 구성이면 내부 주소, 예:
  `http://localhost:7000/paas/api/v1/mcp/ops`). 위험은 도구 쪽에서 막습니다: ops·code는
  읽기 전용, storage는 저장소 루트 안에 갇히고, db는 `PAAS_MCP_DB_MODULES` 허용 목록
  (기본 빈 목록 = 전부 차단) + SELECT 한 문장 + 행 수 상한 + 실행 SQL 감사 기록입니다.
  DB 드라이버(psycopg 등)는 선택 의존성이며, 없으면 무엇을 설치해야 하는지 알려 줍니다.
  이 서버들은 **사내 MCP 검색**(`GET /mcp/search`, 콘솔 모듈 화면의 "사내 MCP 검색")에
  그대로 나와 원클릭으로 모듈 등록되며, 이때 **그 모듈 전용 API 키가 함께 발급**됩니다
  (`mcp-{모듈명}`, 비관리자). 사내 MCP 서버도 다른 엔드포인트와 같은 키를 요구하므로,
  키 없이 등록하면 등록은 성공한 채 연결 확인이 401로 떨어지고 바인딩된 앱도 붙지
  못합니다. 발급 키를 **비관리자**로 두는 이유는 mcp 모듈의 `api_key`가 바인딩된 앱의
  환경변수(`{PREFIX}_API_KEY`)로도 주입되기 때문입니다 — 관리자 키를 넣으면 관리자
  권한이 앱 env로 새어 나갑니다. 키는 감사 로그에 남고 키 목록에서 회수할 수 있습니다.
  (사외 서버는 자격증명을 플랫폼이 지어낼 수 없으므로 비워 두고, 모듈 수정에서
  `config.api_key`에 넣습니다.) 목록은 고정 표가 아니라 **지금 있는 것에서**
  만듭니다 — 저장소 서버는 `PAAS_STORAGE_ROOT`·`PAAS_DOC_ROOTS`가 연 저장소마다,
  DB 서버는 `PAAS_MCP_DB_MODULES`
  허용 목록에 있어야, 코드 서버는 프로젝트마다 하나씩 나옵니다(없는 대상을 목록에 올리면
  "등록했는데 왜 안 되나"를 추적하게 됩니다). 주소의 기준은 `PAAS_MCP_INTERNAL_BASE_URL`
  (비우면 백채널 주소 → 공개 주소 순)이며, **플랫폼이 자기 자신에게 닿는 주소**여야
  합니다 — 셋 다 없으면 주소 없이 경로만 나가고 등록은 막힙니다.
- **아웃바운드 검증과 `Secured` 배지**(`services/egress.py`): 모듈 목록의 각 항목에
  `egress`가 함께 실려 나가고, 콘솔이 배지로 표시합니다 — `🏠 사내`(사내 주소라 망을
  벗어나지 않음) / `🔒 Secured`(사외지만 플랫폼이 보내는 호출에 내부 정보가 없음을 확인) /
  `⚠ 점검 필요`(주소에 자격증명이 박혔거나, 사외인데 http). 판정은 저장하지 않고 볼 때마다
  계산합니다 — 주소를 바꾸면 즉시 반영돼야 하는데 플래그로 굳히면 "검증됨"이 남습니다.
  공개 도메인처럼 생긴 사내 주소는 `PAAS_INTERNAL_DOMAINS`에 접미사를 적어야 사내로 봅니다.
  > **판정 범위는 플랫폼이 보내는 것뿐입니다.** 배포된 앱은 바인딩으로 `{PREFIX}_URL`을
  > 받아 직접 호출하므로 앱이 무엇을 보내는지는 이 검증으로 알 수 없습니다. 배지 설명
  > 문구도 그 범위로만 적혀 있습니다.
  >
  > 이 검증을 붙이면서 실제 유출 두 곳을 고쳤습니다: 모듈 프록시가 들어온 헤더를 통째로
  > 대상에 넘겨 **호출자의 `x-api-key`·`cookie`·`authorization`이 사외 API로 나가던 것**
  > (허용 목록만 넘기도록 변경), A2A 게이트웨이가 호출자 신원(`x-paas-calling-agent`,
  > 대개 이메일)을 사외 대상에도 붙이던 것(사내 대상에만 붙이도록 변경).
- **서버 디스크의 사내 문서 폴더 붙이기**: `PAAS_DOC_ROOTS`에 절대 경로를 적으면 그
  디렉터리가 저장소가 되고 `/mcp/storage/{저장소}`로 바로 읽힙니다. 이렇게 붙인 폴더는
  **항상 읽기 전용**입니다 — 플랫폼이 만든 것이 아니므로 쓰기·삭제 도구를 아예 광고하지
  않고, 콘솔 파일 관리 화면의 업로드·삭제도 403으로 막습니다. 목록은 `glob`으로 걸러 상한까지만
  주며(잘리면 그렇다고 알립니다), 파일 내용은 **본문 텍스트를 추출**해서 줍니다
  (`services/doctext.py`) — 형식은 확장자가 아니라 컨테이너 매직으로 판별합니다:
  `docx·xlsx·pptx·hwpx`는 zip+XML이라 표준 라이브러리만으로, `pdf`는 `pypdf`(선택
  의존성)로, 97-2003 바이너리(`doc·xls·ppt`)는 LibreOffice 변환(`PAAS_SOFFICE_PATH`)으로
  처리하고, 평문은 utf-8 → cp949 순서로 디코드합니다(한국어 윈도우 txt·csv). 추출할 수
  없으면 깨진 글자 대신 이유를 돌려줍니다(스캔 PDF면 OCR이 필요하다고 알립니다).
- **읽기용 마크다운과 `.ready` 캐시**(`services/docready.py`): 문서를 평문으로만 뽑으면
  **표가 셀 나열로 무너집니다** — "구분 / 산정 기준 / 적용 시점 / 국내 자재 / 직전 분기
  평균 매입가 / 분기 초"에서 "분기 초"가 어느 항목의 값인지 복원할 방법이 없습니다.
  그래서 `read_doc`·`read_file`은 **마크다운**으로 줍니다: docx의 제목 단계는 `#`,
  표는 마크다운 표, 스프레드시트는 시트 이름 + 표가 됩니다.
  > HTML도 같은 일을 하지만 재어 보니 표가 큰 문서에서 토큰이 1.6~1.9배였고(100행×8열
  > 스프레드시트: 11,660자 대 6,016자), 태그가 검색 발췌의 절반을 먹고, `td`·`tr`·`th`를
  > 부분 문자열로 포함하는 질의가 **모든** 문서에 오탐으로 걸립니다. 그래서 마크다운을
  > 쓰되, 마크다운 표가 담을 수 없는 **가로 병합 셀**이 있는 표만 인라인 HTML로
  > 떨어뜨립니다(GFM이 허용합니다).

  결과는 `{PAAS_DOC_INDEX_DIR}\.ready\{저장소}\{원본경로}.md`에 남고, 원본의 크기·mtime을
  front matter에 적어 두고 맞을 때만 씁니다. 색인이 이미 추출을 하므로 그때 함께 쓰이고,
  읽기는 파일 읽기 한 번이 됩니다(실측 8~34배). 문서 폴더 안이 아니라 색인 폴더 아래에
  두는 이유가 둘입니다 — 사내 공유 폴더는 읽기 전용이라 거기 캐시를 쓰면 남의 드라이브에
  정체불명 폴더가 생기고 백업에 딸려 가며, `.md`는 색인 제외 확장자가 아니라서 문서 폴더
  안에 두면 **자기 캐시를 다시 색인**합니다(문서마다 결과가 둘씩). 캐시는 파생 데이터라
  지워도 다음 읽기·색인에 다시 만들어집니다. 운영자가 열어 보면 **모델이 실제로 보는
  것**을 그대로 확인할 수 있다는 것이 sqlite에 넣지 않은 이유입니다.
- **문서 본문 검색**(`services/docsearch.py`): 저장소 하나를 다루는
  `/mcp/storage/{모듈}`과, 저장소를 가로질러 한 번에 찾는 **`/mcp/docs`** 두 곳에 있습니다 —
  뒤쪽은 저장소 이름을 모르는 쪽이 부르는 창구라 결과에 어느 저장소인지를 실어 주고
  (`source`), `list_sources`로 대상과 색인 커버리지를 함께 봅니다. `search_docs`는 파일명이
  아니라 **본문**을
  찾습니다 — 공백으로 끊은 낱말을 모두 포함하는 문서를 골라 일치 대목 발췌와 함께 줍니다.
  색인에는 그 마크다운에서 파이프·태그를 벗긴 **평문**이 들어갑니다 — 발췌에 마크업이
  섞이면 값이 안 보이고 마크업 낱말이 질의에 걸립니다.
  > **공유 폴더의 부산물은 색인하지 않습니다.** 오피스 잠금 파일(`~$규정.docx` — 문서를
  > 여는 동안 만들어지는, 계정명 몇 바이트가 든 바이너리), 백업 사본(`.wbk`·`.xlk`·`.bak`),
  > 맥이 남기는 `.DS_Store`·`._원본`, `desktop.ini`, 메일 보관 파일(`.pst`·`.ost`), 그리고
  > **휴지통(`$RECYCLE.BIN`)과 `System Volume Information`은 들어가지도 않습니다**.
  > 실제 폴더로 대조해 보면 9개 파일 중 문서는 2개인데, 거르지 않으면 8건이 색인되고
  > 그중 3건이 **휴지통에서 지운 문서**였습니다 — 지운 문서가 검색·읽기로 되살아나던
  > 셈입니다. 나머지도 실패로 남아 `failure_reasons`를 덮어 진짜 문제(스캔 PDF,
  > 97-2003 파일)를 가렸습니다. 권한이 막혀 못 읽은 폴더가 있으면 `reindex_docs` 응답에
  > `unreadable_dirs`로 알리고, **그 아래 문서는 색인에 그대로 둡니다** — "목록에 없다"를
  > "지워졌다"로 받으면 네트워크 드라이브가 잠깐 끊긴 것만으로 색인이 통째로 비워지고,
  > 다시 붙었을 때 수천 건을 처음부터 다시 추출해야 합니다. 막힌 폴더 **밖**의 삭제는
  > 평소대로 정리되므로, 폴더 하나가 막혔다고 지운 문서가 남지도 않습니다.
  색인은 `reindex_docs`로 만들고(`PAAS_DOC_INDEX_DIR`, 저장소별 sqlite 파일), 크기·mtime이
  같은 파일은 건너뛰는 증분이며 **추출 실패도 캐시**합니다(97-2003 파일 하나를 LibreOffice로
  열어 보는 데 2초쯤 들어서, 실패를 기억하지 않으면 색인마다 그 시간을 다시 씁니다).
  한 호출은 정해진 시간만 진행하고 `remaining`을 돌려주므로 MCP 요청 타임아웃(30초)을
  넘기지 않습니다 — `done`이 false면 다시 부릅니다. `index_status`는 확장자별 성공·실패
  건수와 **실패 이유**를 묶어 주므로 "붙였는데 검색이 안 된다"가 색인 문제인지 질의 문제인지
  바로 갈립니다.
  > 검색은 FTS5가 아니라 추출 텍스트 테이블 + LIKE입니다. FTS5는 한국어에서 깨집니다 —
  > 기본 unicode61은 공백 토큰만 잡아 `규정`으로 "규정은"을 못 찾고, trigram은 3글자 미만
  > 질의를 아예 받지 못해 `정산`·`휴가` 같은 2음절 키워드가 전부 탈락합니다. LIKE는 부분
  > 일치가 정확하고, 본문 6MB/5,000건 전체 스캔이 30ms였습니다.

## 콘솔 UI (`console/`)

React + Vite 관리 대시보드. 빌드 산출물(`console/dist`)이 있으면 FastAPI가 `/console`에 자동
마운트합니다(없어도 API는 동일 기동).

```bash
cd platform/console
npm install
npm run build        # tsc 타입체크 + vite build → dist/
# 개발 모드: npm run dev (http://localhost:5173/console/, API는 :7000으로 프록시)
```

- 접속: `http://<서버>:7000/console/` → API 키로 로그인
  (admin 키: 대시보드·감사 로그·키 발급·프로바이더 등록 포함, 일반 키: 프로젝트 운영 화면)
- 레이아웃: 메뉴는 왼쪽 고정 사이드바(`components/Layout.tsx`)에 배치되고, OS 태그·계정
  구분·로그아웃은 사이드바 하단에 있다
- 화면: 시스템 대시보드(CPU/메모리/디스크/GPU 게이지, 키 발급), 프로젝트(생성 시 git_url 직접
  입력/조직 소속 자동 생성/zip·폴더 업로드 3가지 방식 선택, dev/release 배포·롤백·중지·배포
  이력·로그 3초 폴링·환경변수·모듈 바인딩·프리뷰), 코드 확인(읽기 전용 파일 트리·내용 뷰어 —
  수정 경로 없음 — 구현은 외부 개발도구), 모듈 레지스트리(카테고리·조직 범위 표시 + admin은 "외부 API
  검색"으로 공개 디렉터리에서 external_api 자동 추가), 파일 관리(환경변수가 연 저장소의
  경로·창구 URL 표시 + 목록·다운로드, 쓰기 가능한 저장소만 업로드·삭제), 계정 승인(가입 신청 승인/거절), LLM 프로바이더,
  대화식 코드 편집(diff 뷰 + 승인/거절 + 브랜치 리뷰 + 프로젝트 선택 시 카테고리별 사용 가능
  자원 패널 + 코드 구조 트리 확대/축소), 서버구성(런타임/
  프록시 백엔드 표시, 프로젝트×프로필별 도메인·상태·배포/중지, 리다이렉트/재작성 규칙
  관리), 감사 로그
- 인증은 `x-api-key`를 sessionStorage에 보관(기존 admin/mail 관례). 로그인 검증은 admin 전용
  `GET /paas/status` 응답 코드(200 admin / 403 일반 / 401 무효)를 프로브로 재사용
- 의존성: react·react-dom·react-router-dom (전부 MIT). 라우팅은 해시 기반이라 새로고침·딥링크에
  백엔드 폴백이 필요 없습니다

## 기업용 옵션 (14.2절 갭 구현)

- **사내 Git 서버(Gitea)**: GitHub 대신 소스가 사외로 나가지 않는 self-host Git 서버 배포.
  Docker Compose(1차)/K8s manifests(2차) + 웹훅·Keycloak SSO 연동은
  [`infra/gitea/README.md`](infra/gitea/README.md) 참고. `PAAS_GITEA_URL`을 설정하면
  콘솔 상단 메뉴에 **Git** 탭이 나타나 등록된 프로젝트별 리포 바로가기를 보여준다.
- **코드 내부 관리 강제(기본값 켜짐)**: `PAAS_GIT_INTERNAL_ONLY` 기본값이 `true`라
  `PAAS_GITEA_URL`을 설정하지 않으면 프로젝트 등록 자체가 503으로 막히고, 설정했다면
  `git_url` 호스트가 사내 Gitea와 다를 때 422로 거부한다(github.com 등 외부 호스트 등록
  원천 차단). internal LLM 프로바이더 강제(12절)와 동일한 원칙 — 외부 호스트를 허용하려면
  `PAAS_GIT_INTERNAL_ONLY=false`로 명시적으로 꺼야 한다.
- **조직별 작업공간**: 콘솔의 조직 페이지(admin)에서 조직을 만들면 사내 Gitea에 동일한
  이름의 Organization이 함께 생성된다(`PAAS_GITEA_API_TOKEN` 필요). 조직 소속 프로젝트는
  리포를 플랫폼이 내부에서 자동 생성·관리하며, git_url 등 메타 정보는 **일반 사용자
  응답에서 마스킹**된다(admin만 실제 값 조회 가능) — `POST /paas/api/v1/orgs`, `GET /paas/api/v1/orgs`,
  `POST /paas/api/v1/projects`의 `organization_id` 참고.
- **Gitea 기준 동기화(양방향 정합성)**: 위 흐름은 플랫폼 → Gitea(생성)뿐이라, 누군가
  Gitea에서 직접 조직/리포를 만들거나 지우면 플랫폼이 모른다. `POST /paas/api/v1/orgs/sync`
  (admin, 콘솔 조직 페이지의 "Gitea에서 동기화" 버튼)가 그 간극을 메운다.
  - Gitea에는 있지만 플랫폼 DB에 없는 조직/리포를 찾아 Organization/Project로 가져온다.
    리포의 `type`은 Gitea API만으론 알 수 없어 얕은 clone으로 시그니처 파일
    (requirements.txt/pyproject.toml→python, package.json+react 의존성→react,
    package.json만→node, index.html만→html, backend/frontend 서브폴더 둘 다 있으면
    →composite)을 확인해 추론하고, 추론 불가하거나 이름 규칙(`^[a-z0-9][a-z0-9-]{1,40}$`)에
    안 맞으면 만들지 않고 이유와 함께 건너뛴다.
  - 반대로 플랫폼(조직 소속 프로젝트)에는 있지만 Gitea에 리포가 없으면(수동 삭제 등),
    `on_missing_repo` 파라미터대로 처리한다 — `create`(기본값)는 리포를 다시 만들고
    `git_url`을 갱신하며, `delete`는 배포 이력·환경변수·리다이렉트 규칙 등 딸린 데이터를
    포함해 플랫폼 쪽 프로젝트를 지운다(되돌릴 수 없음 — 콘솔은 이 선택 시 확인창을 띄운다).
    git_url을 직접 지정한(조직 없는) 레거시 프로젝트는 애초에 Gitea 관리 대상이 아니므로
    대상에서 제외된다.
  (`services/gitea_sync.py`). 자동/주기 실행은 하지 않는다 — 필요할 때 관리자가 수동으로.
- **zip/폴더 업로드 등록**: `POST /paas/api/v1/projects/upload`(조직 필수) — git 저장소가 아직 없는
  코드를 zip 또는 폴더(다중 파일)로 올리면 플랫폼이 사내 Gitea에 신규 리포를 만들어
  최초 커밋으로 push한다. 대용량·악성 업로드 방어(`app/services/upload.py`):
  업로드 원본 스트리밍 크기 상한(`PAAS_UPLOAD_MAX_ZIP_MB`), 압축 해제 시 실제 바이트
  기준 총량 상한(`PAAS_UPLOAD_MAX_UNCOMPRESSED_MB`, zip 헤더 선언값을 신뢰하지 않음),
  엔트리 수 상한(`PAAS_UPLOAD_MAX_FILES`), 파일별 압축비 상한(`PAAS_UPLOAD_MAX_COMPRESSION_RATIO`),
  절대경로·상위 디렉토리 탈출(zip slip)·심볼릭 링크 엔트리 거부. `deploy_after_upload`로
  push 직후 배포까지 원클릭 진행 가능.
- **코드 확인 화면**: `GET /paas/api/v1/projects/{id}/files`, `/files/content`로 리포를 읽기 전용
  브라우징. 저장/수정 엔드포인트는 존재하지 않는다 — 실제 코드 변경은 외부 개발도구가
  리포에 직접 커밋하며, 플랫폼은 기획·제약·검증·모니터링만 담당한다.
- **웹훅 자동 등록**: `PAAS_PLATFORM_PUBLIC_URL` 설정 시 조직 소속/업로드로 리포를 만들
  때마다 플랫폼이 자신의 `/paas/webhooks/git`을 Gitea 웹훅으로 자동 등록한다(베스트 에포트 —
  실패해도 프로젝트 생성은 성공 처리). 비워두면 기존처럼 `infra/gitea/README.md`의
  수동 웹훅 설정이 필요하다.
- **Gitea private 리포 인증**: 조직/업로드로 생성된 리포는 Gitea에 `private:true`로
  생성되므로, 플랫폼이 직접 clone/fetch/push할 때도 `PAAS_GITEA_API_TOKEN`을 git
  프로세스에 `http.extraHeader`로 주입해 인증한다(`app/services/git_auth.py`) —
  git_url 자체에는 토큰을 심지 않는다.
- **운영환경별 런타임/리버스프록시 선택 + 서버구성 시각화**: 1차(small)는 실행 런타임을
  `PAAS_RUNTIME_BACKEND`(docker 기본 | windows_service — Docker 없이 nssm으로 네이티브
  프로세스를 Windows Service로 등록), 리버스프록시를 `PAAS_PROXY_BACKEND`(caddy 기본 |
  iis | apache)로 각각 독립적으로 선택할 수 있다(`app/services/runtime/`,
  `app/services/proxy/`). windows_service는 리포 루트의 `paas-start.cmd`(PORT
  환경변수로 리슨 포트 전달) 관례로 기동하며, IIS는 `web.config`(URL Rewrite)+appcmd,
  Apache는 VirtualHost(mod_proxy/mod_rewrite)+`apachectl graceful`로 사이트를
  등록·반영한다. `GET /paas/api/v1/server-config`가 현재 선택된 백엔드와 프로젝트별(프로필별)
  도메인·실행 상태·리다이렉트 규칙 수를 한 화면에서 보여주고, 콘솔 "서버구성" 메뉴는
  이를 표와 함께 proxy → 사이트 → runtime 관계를 그리는 토폴로지 다이어그램(순수 SVG,
  신규 의존성 없음)으로도 시각화한다.
- **프로젝트별 URL redirect/rewrite 규칙**: `POST/GET /paas/api/v1/projects/{id}/redirects`,
  `DELETE /paas/api/v1/redirects/{id}`로 등록하면 다음 배포·롤백 때 선택된 프록시 백엔드 설정에
  자동 반영된다(Caddy `redir`/`rewrite`, IIS URL Rewrite rule, Apache
  `Redirect`/`RewriteRule`).
- **복합(백엔드+프론트엔드) 프로젝트**: `type: composite`로 등록하면 리포 루트의
  `backend/`, `frontend/` 서브폴더를 배포 시점에 자동 감지(시그니처 파일 기준 —
  requirements.txt/pyproject.toml→python, package.json+react 의존성→react,
  package.json만→node, index.html만→html)해 각각 별도 이미지로 빌드·기동하고, 같은
  도메인 아래 `/api/*`는 백엔드로, `/*`는 프론트엔드로 자동 라우팅한다(Caddy
  `handle_path`/IIS URL Rewrite 조건부 규칙/Apache `ProxyPass` 접두사 — 세 프록시
  백엔드 모두 지원). 배포는 원자적이다: 한쪽이 실패하면 실패한 컴포넌트만 재빌드 없이
  직전 정상 이미지로 복구한 뒤에만 프록시를 갱신하므로, 부분 실패가 서비스 중단으로
  이어지지 않는다(`app/services/deployer.py`의 `deploy_composite_sync` 참고).
- **OIDC/RBAC (Keycloak 호환)**: `PAAS_OIDC_ISSUER` 설정 시 `Authorization: Bearer <JWT>`
  인증 병행. `realm_access.roles`에 `PAAS_OIDC_ADMIN_ROLE`(기본 paas-admin)이 있으면 admin.
- **내장 OIDC Provider (SSO 허브 — 별도 IdP 불필요)**: `PAAS_OIDC_PROVIDER_ENABLED=true`면
  플랫폼 자신이 OIDC 발급자가 된다(`app/services/oidc_provider.py`, Authorization Code
  플로우). 플랫폼의 **로그인 계정 그 자체**가 SSO ID이므로 Gitea 등 사내 서비스에 계정을
  이중으로 두지 않아도 되고, Keycloak 같은 서버를 따로 세울 필요도 없다 — 콘솔에 로그인해
  둔 브라우저로 Gitea를 열면 로그인 화면 없이 통과한다(연동 절차는
  [infra/gitea/README.md](infra/gitea/README.md)). 발급하는 토큰의 클레임 모양이 위
  Keycloak 경로와 같아서, `PAAS_OIDC_ISSUER`를 자기 자신으로 두면 같은 검증 코드가
  그대로 쓰인다. 옵트인이며(기본 꺼짐) 서명 키는
  `PAAS_OIDC_PROVIDER_SIGNING_KEY_PATH`(기본 `./data/oidc-signing-key.pem`)에 자동
  생성·재사용된다 — **이 파일은 백업 대상**(지우면 기존 토큰이 전부 무효).
- **계정 비밀번호와 세션**: 사람 계정(`user_accounts`)의 비밀번호는 **솔트 + scrypt**로만
  저장한다(표준 라이브러리라 폐쇄망에 의존성을 늘리지 않는다). 기계용 API 키(`api_keys`)는
  `issue_key()`가 만드는 256비트 난수라 sha256으로 충분해 그대로 둔다 — 추측 가능한
  비밀번호와 난수 키는 같은 방식으로 지킬 수 없다.
  로그인은 비밀번호에서 유도되지 않는 **난수 세션 토큰**(`user_sessions`, 기본 12시간)을
  발급하고 해시만 저장한다. `POST /paas/api/v1/auth/logout`으로 서버에서 폐기되며,
  만료된 토큰은 `require_api_key`가 지운다. 비밀번호 원문은 TLS 위로만 오가고 어디에도
  저장되지 않는다.
- **가입은 관리자 승인제**: `/auth/register`는 신청만 만든다(`is_approved=false`) — 세션을
  주지 않으므로 승인 전에는 로그인할 수 없다(403). 도메인이 맞기만 하면 누구나 계정을
  만들 수 있던 것을 막는다. admin이 콘솔 **계정 승인** 화면 또는
  `GET /auth/accounts` · `POST /auth/accounts/{id}/approve` · `DELETE /auth/accounts/{id}`로
  처리하며, 삭제하면 그 계정의 세션도 함께 폐기된다. 최초 관리자는 `PAAS_ADMIN_API_KEY`다.
- **비동기 배포**: `POST /paas/api/v1/projects/{id}/deploy`에 `"wait": false` → 202 즉시 반환,
  `GET /paas/api/v1/projects/{id}/deployments`로 진행 폴링. 워커 수는 `PAAS_DEPLOY_WORKERS`(기본 2).
- **OpenBao 시크릿**: `PAAS_OPENBAO_URL/TOKEN/KEY_PATH` 설정 시 Fernet 키를 KV v2에서 로드.
- **멀티테넌시 격리**: `PAAS_K8S_ISOLATION=true` → 유닛별 NetworkPolicy
  (ingress 컨트롤러 네임스페이스는 `PAAS_K8S_INGRESS_NAMESPACE`).
- **외부 호출 재시도 + 서킷브레이커**: 외부 API 디렉터리 조회는 네트워크 오류에 한해
  3회 백오프 재시도하고, 호스트별 연속 5회 실패 시 60초 차단 후 half-open 복구.
- **GitOps(ArgoCD)**: `PAAS_K8S_GITOPS_REPO` 설정 시 직접 apply 대신 매니페스트를
  해당 리포에 커밋·푸시 (`PAAS_K8S_GITOPS_BRANCH`/`_PATH`). ArgoCD가 sync 담당.
- **키 회전**: 새 키를 `PAAS_FERNET_KEY`로, 기존 키를 `PAAS_FERNET_KEYS_OLD`로 옮겨
  재기동 → `POST /paas/api/v1/admin/rotate-secrets`(admin) → 완료 후 구 키 제거.
- **네임스페이스 Quota**: `PAAS_K8S_QUOTA_CPU`/`_MEMORY` 설정 시 ResourceQuota +
  기본 LimitRange 매니페스트 생성.

## 플랫폼 내 Git 구현

플랫폼은 **자체 리포(chofam)와는 무관하게**, 배포 대상 프로젝트마다 독립된 로컬 git 체크아웃을
`work_dir`(기본 `./data/workspaces/{project}`) 아래 두고 조작한다. 전부 `subprocess`로 시스템 git을
호출하며(플랫폼 자체 git 라이브러리 없음), 외부로 나가는 지점은 웹훅 수신과 GitOps 푸시 둘뿐이다.

| 컴포넌트 | 파일 | 동작 |
| --- | --- | --- |
| **배포 체크아웃** | `services/build.py` `checkout()` | 최초엔 `git clone --branch`, 이후는 `fetch`+`reset --hard`로 매 배포마다 최신화. `git_sha` 지정 시 해당 커밋으로 `checkout`. **읽기 전용** — 이 리포에 커밋하지 않음 |
| **웹훅 자동 배포** | `api/webhooks.py` | GitHub/Gitea push 이벤트를 **수신**(HMAC 서명 검증 필수)해 위 checkout→build 파이프라인을 트리거. 플랫폼이 밖으로 나가는 방향이 아니라 받는 방향 |
| **기획 산출물 커밋** | `services/workspace.py` `write_and_commit()` | 확정된 기획 문서만 세션별 작업 브랜치(`paas/plan-{id}-{hex}`)에 커밋하고 사내 Gitea로 push한 뒤, git 상태에 따라 PR·머지를 자동 수행한다. 플랫폼이 리포에 쓰는 경로는 이것뿐이며 **구현 코드는 쓰지 않는다** |
| **GitOps 연계** | `services/runtime/k8s_runtime.py` `_gitops_push`/`_sync_gitops_repo` | 2차(K8s) 티어에서 `PAAS_K8S_GITOPS_REPO` 설정 시에만 활성화. 배포 **매니페스트**(이미지 태그·비-시크릿 env)를 별도 GitOps 리포에 커밋·푸시해 ArgoCD가 반영하게 한다. **시크릿은 여기 포함되지 않음**(15절) — 애초에 소스 코드가 아니라 K8s 매니페스트만 다루는 경로 |
| **프리뷰** | `services/preview.py` | 위 checkout 재사용, 별도 git 조작 없음 |

**핵심 경계선**: 프로젝트 소스 코드 자체가 플랫폼 밖의 git 리포로 나가는 경로는 없다
(체크아웃은 읽기 전용, 플랫폼이 쓰는 것은 사내 Gitea의 기획 산출물뿐). 외부로 실제 전송되는
것은 두 가지뿐이다 — ① 기획 시 파일 **내용**이 API 호출로 프로바이더에 전송(어느 프로바이더인지는 12절/15절의
internal·external 구분과 admin 게이트로 통제), ② GitOps 모드에서 배포 **매니페스트**(소스 아님)가
운영자가 지정한 리포로 푸시.

## DB 마이그레이션

`create_all()`(기동 시 자동 실행)은 **아예 없는 테이블만** 새로 만든다 — 이미 있는
테이블에 새 컬럼을 추가하는 스키마 변경(예: `projects.organization_id`,
`redirect_rules` 테이블 등)은 반영하지 못한다. 그래서 "SQLite 빠른 시작이라
create_all로 충분하다"는 건 **DB 파일이 아예 없는 최초 기동에만** 해당하는
얘기다 — **이미 떠 있던 서버를 최신 코드로 업데이트할 때는 SQLite·PostgreSQL
관계없이 반드시 Alembic으로 스키마를 맞춰야 한다:**

```bash
# 실행 위치: platform/ — SQLite면 PAAS_DATABASE_URL 생략(.env 설정 그대로 사용)
python -m alembic upgrade head
# PostgreSQL이면:
PAAS_DATABASE_URL=postgresql://user:pw@host/paas python -m alembic upgrade head
# 모델 변경 후 새 리비전: python -m alembic revision --autogenerate -m "설명"
```

**증상**: 이 단계를 건너뛰면 새로 추가된 컬럼/테이블을 실제로 쓰는 API만 콕 집어
500 Internal Server Error가 난다(다른 API는 멀쩡) — 예를 들어 조직(Organization)
기능 도입 이전 DB로 계속 돌리다가 코드만 업데이트하면, `/orgs/sync`·`/server-config`·
`POST /projects`처럼 `organization_id`를 만지는 엔드포인트만 "no such column"
류의 에러로 500이 난다. `python -m alembic current`로 지금 DB가 어느 리비전인지,
`python -m alembic heads`로 코드가 기대하는 최신 리비전이 뭔지 비교해 보면 바로
확인된다.

## 테스트

```bash
cd platform && python -m pytest tests/ -q   # 107 passed
```

Docker/K8s 미설치 환경에서도 컨트롤 플레인·매니페스트 생성·프로필 로직이 검증됩니다.
