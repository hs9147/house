import { getKey, logout } from './auth';
import type {
  ApiKeyIssued,
  ApiCatalogStatus,
  ApiCategory,
  ApiSearchResult,
  AuditRow,
  BuildProfile,
  BuildTaskOut,
  BuildTaskSync,
  CodeMapOut,
  ComplianceOut,
  GiteaSyncResult,
  HealthInfo,
  DeploymentOut,
  EnvVarRow,
  LlmProviderOut,
  McpDirectoryItem,
  ModuleOut,
  ModuleSummary,
  OrgOut,
  PlanArtifactContent,
  PlanArtifactOut,
  PlanBuildStatus,
  PlanChatMessage,
  PlanMergeOut,
  PlanMessageReply,
  PlanSessionOut,
  PlanSessionSummary,
  PreviewOut,
  ProjectCreate,
  ProjectFileContentOut,
  ProjectFilesOut,
  ProjectModuleReportOut,
  PlatformModuleReportOut,
  ProjectOut,
  ProjectType,
  RedirectRule,
  SchedulerSnapshot,
  ResourceItem,
  ReviewResult,
  ServerConfigOut,
  StatusSnapshot,
  StorageStore,
  UserAccountOut,
  UserOrgOut,
} from './types';

// 백엔드 라우터는 모두 /paas 아래 마운트된다. /health, /status는 버전 prefix 없이
// /paas만 받는다(로드밸런서/k8s probe 및 로그인 프로브가 버전과 무관한 고정 경로를 기대함) —
// 나머지는 /paas/api/v1. app/main.py의 PAAS_PREFIX/API_PREFIX와 반드시 맞출 것.
const PAAS_PREFIX = '/paas';
const API_BASE = `${PAAS_PREFIX}/api/v1`;
const UNVERSIONED_PATHS = new Set(['/health', '/status']);

function apiUrl(path: string): string {
  return UNVERSIONED_PATHS.has(path) ? `${PAAS_PREFIX}${path}` : `${API_BASE}${path}`;
}

// 터미널은 WebSocket이라 fetch 경로(x-api-key 헤더)를 쓸 수 없다. 브라우저는 WebSocket
// 핸드셰이크에 임의 헤더를 붙일 수 없어서 키를 **서브프로토콜**로 싣는다 — 쿼리스트링으로
// 보내면 IIS/ARR 접근 로그에 관리자 키가 그대로 남는다. 서버는 비밀값이 아닌
// 'paas-terminal' 쪽을 골라 되돌려 준다(app/api/system.py의 WS_KEY_PREFIX).
export const TERMINAL_SUBPROTOCOL = 'paas-terminal';

export function openTerminalSocket(): WebSocket {
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${scheme}//${window.location.host}${API_BASE}/system/powershell/ws`;
  return new WebSocket(url, [TERMINAL_SUBPROTOCOL, `paas-key.${getKey()}`]);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

// FastAPI 422는 detail이 [{loc, msg, type}, ...] 배열 — String(array)는 "[object Object]"가
// 되어버리므로 사람이 읽을 수 있는 메시지로 풀어낸다. 나머지 에러(409 등)는 detail이
// 문자열이라 그대로 반환된다.
function formatDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (d && typeof d === 'object' && 'msg' in d) {
          const loc = Array.isArray((d as { loc?: unknown[] }).loc)
            ? (d as { loc: unknown[] }).loc.join('.')
            : '';
          const msg = String((d as { msg: unknown }).msg);
          return loc ? `${loc}: ${msg}` : msg;
        }
        return JSON.stringify(d);
      })
      .join('; ');
  }
  return JSON.stringify(detail);
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  query?: Record<string, string | number | undefined>,
): Promise<T> {
  let url = apiUrl(path);
  if (query) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== '') params.set(k, String(v));
    }
    const qs = params.toString();
    if (qs) url += `?${qs}`;
  }
  const res = await fetch(url, {
    method,
    headers: {
      'x-api-key': getKey(),
      ...(body !== undefined ? { 'content-type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    // 만료/무효 키 — 전역 단일 처리 지점
    logout();
    window.location.hash = '#/login';
    throw new ApiError(401, '인증이 만료되었습니다. 다시 로그인하세요.');
  }
  if (res.status === 204) return undefined as T;
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    /* 본문 없는 응답 */
  }
  if (!res.ok) {
    const detail =
      data && typeof data === 'object' && 'detail' in data
        ? formatDetail((data as { detail: unknown }).detail)
        : `HTTP ${res.status}`;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

async function requestMultipart<T>(path: string, formData: FormData): Promise<T> {
  const res = await fetch(apiUrl(path), {
    method: 'POST',
    headers: { 'x-api-key': getKey() },
    body: formData,
  });
  if (res.status === 401) {
    logout();
    window.location.hash = '#/login';
    throw new ApiError(401, '인증이 만료되었습니다. 다시 로그인하세요.');
  }
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    /* 본문 없는 응답 */
  }
  if (!res.ok) {
    const detail =
      data && typeof data === 'object' && 'detail' in data
        ? formatDetail((data as { detail: unknown }).detail)
        : `HTTP ${res.status}`;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export const api = {
  // 시스템
  health: () => request<HealthInfo>('GET', '/health'),
  status: () => request<StatusSnapshot>('GET', '/status'),
  audit: (limit = 100) => request<AuditRow[]>('GET', '/audit', undefined, { limit }),
  issueKey: (name: string, is_admin: boolean) =>
    request<ApiKeyIssued>('POST', '/keys', { name, is_admin }),

  // 주기 갱신 모니터 — 목록은 서버가 저장소·모듈 현황에서 만든다(사람이 등록하지 않는다).
  schedulerSnapshot: () => request<SchedulerSnapshot>('GET', '/scheduler'),
  runScheduledJob: (id: number) =>
    request<{ job: string; status: string; ms: number; detail: Record<string, unknown> }>(
      'POST', `/scheduler/jobs/${id}/run`),
  toggleScheduledJob: (id: number) =>
    request<{ id: number; name: string; enabled: boolean }>(
      'POST', `/scheduler/jobs/${id}/toggle`),

  // 프로젝트
  listProjects: () => request<ProjectOut[]>('GET', '/projects'),
  createProject: (body: ProjectCreate) => request<ProjectOut>('POST', '/projects', body),
  // 프로젝트 삭제(admin) — Gitea 리포는 남고 플랫폼 등록 정보만 지워진다.
  deleteProject: (id: number) => request<void>('DELETE', `/projects/${id}`),

  // zip/폴더 업로드로 프로젝트 등록 (조직 필수 — 사내 Gitea 리포로 최초 push)
  uploadProject: (
    form: {
      name: string;
      type: ProjectType;
      organization_id: number;
      branch: string;
      health_check_path?: string;
      default_profile: BuildProfile;
      deploy_after_upload: boolean;
    },
    source: { kind: 'zip'; file: File } | { kind: 'folder'; files: FileList },
  ) => {
    const fd = new FormData();
    fd.append('name', form.name);
    fd.append('type', form.type);
    fd.append('organization_id', String(form.organization_id));
    fd.append('branch', form.branch);
    fd.append('health_check_path', form.health_check_path ?? '/');
    fd.append('default_profile', form.default_profile);
    fd.append('deploy_after_upload', String(form.deploy_after_upload));
    if (source.kind === 'zip') {
      fd.append('zip_file', source.file);
    } else {
      Array.from(source.files).forEach((f) => {
        fd.append('files', f, f.webkitRelativePath || f.name);
      });
    }
    return requestMultipart<ProjectOut>('/projects/upload', fd);
  },
  getProjectModuleReport: (id: number) =>
    request<ProjectModuleReportOut>('GET', `/projects/${id}/module-report`),

  // 조직 (사내 Gitea 작업공간)
  listOrgs: () => request<OrgOut[]>('GET', '/orgs'),
  createOrg: (name: string) => request<OrgOut>('POST', '/orgs', { name }),
  syncOrgsFromGitea: (onMissingRepo: 'create' | 'delete' = 'create') =>
    request<GiteaSyncResult>('POST', '/orgs/sync', undefined, { on_missing_repo: onMissingRepo }),
  // 비블로킹 배포 — 즉시 building 레코드(들)를 받아 진행 상황을 폴링으로 추적한다.
  // composite 프로젝트는 backend/frontend 두 레코드를 배열로 돌려준다. 콘솔의 모든
  // 배포 진입점(서버구성·프로젝트 개요)이 이 경로로 통일돼 DeployProgressModal로
  // 같은 진행 팝업을 보여준다.
  deployQueued: (id: number, profile?: BuildProfile, git_sha?: string) =>
    request<DeploymentOut | DeploymentOut[]>('POST', `/projects/${id}/deploy`, {
      profile: profile ?? null,
      git_sha: git_sha || null,
      wait: false,
    }),
  rollback: (id: number, profile: BuildProfile) =>
    request<DeploymentOut>('POST', `/projects/${id}/rollback`, undefined, { profile }),
  stop: (id: number, profile: BuildProfile) =>
    request<void>('POST', `/projects/${id}/stop`, undefined, { profile }),
  deployments: (id: number) => request<DeploymentOut[]>('GET', `/projects/${id}/deployments`),
  // 진행 중(building)인 배포의 현재 로그 tail — 빌드/설치가 오래 걸리거나 멈춰 있어도
  // 지금까지 실행된 명령과 출력을 볼 수 있다(DeployProgressModal이 폴링).
  deploymentBuildLog: (projectId: number, deploymentId: number, tail = 200) =>
    request<{ content: string; done: boolean }>(
      'GET', `/projects/${projectId}/deployments/${deploymentId}/build-log`, undefined, { tail },
    ),
  logs: (id: number, profile: BuildProfile, tail: number) =>
    request<{ logs: string }>('GET', `/projects/${id}/logs`, undefined, { profile, tail }),
  projectStatus: (id: number) =>
    request<Record<BuildProfile, string>>('GET', `/projects/${id}/status`),
  listEnv: (id: number) => request<EnvVarRow[]>('GET', `/projects/${id}/env`),
  setEnv: (id: number, key: string, value: string, is_secret: boolean) =>
    request<void>('PUT', `/projects/${id}/env`, { key, value, is_secret }),

  // 코드 확인 화면 (읽기 전용 — 수정은 채팅/diff 승인으로만)
  projectFiles: (id: number) => request<ProjectFilesOut>('GET', `/projects/${id}/files`),
  projectFileContent: (id: number, path: string) =>
    request<ProjectFileContentOut>('GET', `/projects/${id}/files/content`, undefined, { path }),
  // 코드 구조 트리 (정적 파싱 — 확대/축소 시각화 + 채팅 LLM 컨텍스트와 동일 소스)
  projectCodemap: (id: number) => request<CodeMapOut>('GET', `/projects/${id}/codemap`),

  // 모듈
  listModules: () => request<ModuleOut[]>('GET', '/modules'),
  getPlatformModuleReport: () =>
    request<PlatformModuleReportOut>('GET', '/modules/usage-report'),
  // 외부 API 카탈로그 검색 + external_api 모듈 자동 추가 (admin)
  searchApis: (keyword: string, category?: string, source?: string) =>
    // 검색은 수집해 둔 표만 읽는다(아웃바운드 아님). warnings = 카탈로그가 비어 있다는
    // 사실. 결과가 적은 것이 "그런 API가 없다"인지 "아직 수집하지 않았다"인지 화면에서
    // 구분되어야 한다.
    request<{ results: ApiSearchResult[]; warnings: string[] }>('GET', '/modules/search', undefined, {
      keyword,
      // 빈 값이면 조건을 안 건다(= 전체). 서버 기본값과 같은 뜻이라 굳이 보내지 않는다.
      ...(category ? { category } : {}),
      ...(source ? { source } : {}),
    }),
  // 소스별 수집 현황 — 검색 화면의 소스 선택지가 여기서 나온다.
  apiCatalogStatus: () => request<ApiCatalogStatus>('GET', '/modules/search/status'),
  listApiCategories: () =>
    request<{ categories: ApiCategory[]; uncategorized_label: string }>(
      'GET', '/modules/search/categories'),
  // 카탈로그 수집 — **여기가 유일한 아웃바운드 경로다**(검색은 표만 읽는다).
  // 평소에는 하루 한 번 백그라운드로 돌고, 이 버튼은 그 사이에 당겨 쓰는 자리다.
  refreshApiCatalog: (source?: string) =>
    request<{
      added: number; updated: number; restored: number; removed: number; unchanged: number;
      sources: string[]; skipped: string[]; warnings: string[];
    }>('POST', '/modules/search/refresh', undefined, { ...(source ? { source } : {}) }),
  importApiModule: (name: string, url: string, category?: string) =>
    request<ModuleOut>('POST', '/modules/import', { name, url, category: category || null }),

  // 사내 MCP 서버 검색 + mcp 모듈 자동 추가 (admin) — 목록은 이 플랫폼이 노출하는 서버다
  searchMcpDirectory: (q?: string) =>
    request<McpDirectoryItem[]>('GET', `/mcp/search?q=${encodeURIComponent(q || '')}`),
  // 등록된 mcp 모듈이 실제로 응답하는지 확인(tools/list 1회). 실패도 200으로 오고
  // ok/error로 구분한다 — 여러 모듈을 나열하며 표시하기 때문이다.
  checkMcpModule: (moduleId: number) =>
    request<{ module_id: number; name: string; url: string; ok: boolean; error: string | null;
              tool_count: number; tools: string[];
              // 사내 서버인데 키가 비어 있다 = 그 자리에서 발급할 수 있다. 오류 문구를
              // 파싱해 판단하면 문구를 다듬을 때마다 버튼이 조용히 사라진다.
              can_issue_key: boolean }>('POST', `/modules/${moduleId}/mcp-check`),
  // 이미 등록된 사내 mcp 모듈에 전용 키를 발급해 넣는다. 모듈을 지웠다 다시 만들면
  // 바인딩된 프로젝트를 잃으므로, 그 자리에서 고칠 수 있어야 한다.
  issueMcpModuleKey: (moduleId: number) =>
    request<{ id: number; name: string; key_issued: boolean; config: Record<string, unknown> }>(
      'POST', `/modules/${moduleId}/mcp-key`),
  importMcpModule: (name: string, url: string, category?: string) =>
    request<ModuleOut>('POST', '/modules/import-mcp', { name, url, category: category || null }),
  createModule: (
    name: string,
    type: string,
    config: Record<string, unknown>,
    category?: string,
    organization_id?: number,
  ) =>
    request<ModuleOut>('POST', '/modules', {
      name, type, config,
      category: category || null,
      organization_id: organization_id ?? null,
    }),
  deleteModule: (id: number) => request<void>('DELETE', `/modules/${id}`),

  me: () =>
    request<{ name: string; is_admin: boolean; allowed_email_domain: string | null; organization_id: number | null; organization_name: string | null; organizations?: UserOrgOut[] }>('GET', '/auth/me'),
  listAccounts: () => request<UserAccountOut[]>('GET', '/auth/accounts'),
  approveAccount: (id: number) => request<UserAccountOut>('POST', `/auth/accounts/${id}/approve`),
  updateAccountOrganization: (id: number, organization_id: number | null) =>
    request<UserAccountOut>('POST', `/auth/accounts/${id}/organization`, { organization_id }),
  modifyAccountOrganization: (id: number, organization_id: number, action: 'add' | 'remove') =>
    request<UserAccountOut>('POST', `/auth/accounts/${id}/organizations/modify`, { organization_id, action }),
  rejectAccount: (id: number) => request<void>('DELETE', `/auth/accounts/${id}`),

  // 파일 저장소 — 목록은 환경변수(PAAS_STORAGE_ROOT·PAAS_DOC_ROOTS)가 정한다
  listStorageStores: () => request<StorageStore[]>('GET', '/storage/stores'),
  // 파일 목록·다운로드·삭제 엔드포인트는 백엔드에 그대로 있지만 콘솔에서는 쓰지 않는다 —
  // 파일 관리 화면은 저장소 상태와 업로드만 다루고, 내용을 찾는 창구는 paas-docs다.
  uploadStorageFile: (store: string, file: File, path?: string) => {
    const fd = new FormData();
    fd.append('file', file);
    if (path) fd.append('path', path);
    return requestMultipart<{ path: string }>(`/storage/${store}/files`, fd);
  },
  projectModules: (id: number) => request<ModuleSummary[]>('GET', `/projects/${id}/modules`),
  projectResources: (id: number) => request<ResourceItem[]>('GET', `/projects/${id}/resources`),
  bindModule: (projectId: number, moduleId: number, env_prefix: string) =>
    request<{ injected_env: string[] }>(
      'POST', `/projects/${projectId}/modules/${moduleId}/bind`, { env_prefix },
    ),
  unbindModule: (projectId: number, bindingId: number) =>
    request<void>('DELETE', `/projects/${projectId}/modules/bindings/${bindingId}`),

  // LLM
  listProviders: () => request<LlmProviderOut[]>('GET', '/llm/providers'),
  deleteProvider: (id: number) => request<void>('DELETE', `/llm/providers/${id}`),
  createProvider: (body: {
    name: string; kind: string; base_url: string; api_key?: string; model: string;
    organization_id?: number | null;
  }) => request<LlmProviderOut>('POST', '/llm/providers', body),
  review: (projectId: number, provider_id: number, diff?: string, base_ref?: string) =>
    request<ReviewResult>('POST', `/projects/${projectId}/review`, {
      provider_id, diff: diff || null, base_ref: base_ref || null,
    }),

  // 에이전트 기획 (Agent Planning)
  createPlanSession: (project_id: number, provider_id: number, branch?: string) =>
    request<PlanSessionOut>('POST', '/plan/sessions', {
      project_id, provider_id, branch: branch || null,
    }),
  getPlanSession: (sessionId: number) =>
    request<PlanSessionOut>('GET', `/plan/sessions/${sessionId}`),
  // 세션 이력 — 목록·대화 복원(재개)·삭제
  listPlanSessions: () => request<PlanSessionSummary[]>('GET', '/plan/sessions'),
  planSessionMessages: (sessionId: number) =>
    request<PlanChatMessage[]>('GET', `/plan/sessions/${sessionId}/messages`),
  deletePlanSession: (sessionId: number) =>
    request<void>('DELETE', `/plan/sessions/${sessionId}`),
  // 참조 파일은 서버가 요청 문장을 보고 고른다 — 경로를 사람이 적지 않는다.
  // compact=true는 컨텍스트 한도 초과(413) 후 재시도할 때만.
  sendPlanMessage: (sessionId: number, stage: string, content: string, draft: string,
                    compact = false) =>
    request<PlanMessageReply>('POST', `/plan/sessions/${sessionId}/stages/${stage}/messages`,
      { content, draft, compact }),
  // 단계 산출물 본문 — 세션 재개·단계 이동 시 편집기를 채운다
  planArtifactContent: (sessionId: number, stage: string) =>
    request<PlanArtifactContent>('GET', `/plan/sessions/${sessionId}/stages/${stage}/artifact`),
  // overwrite=true는 리포에 이미 있는 문서를 덮어쓸 때만(412 확인 후 재시도)
  confirmPlanStage: (sessionId: number, stage: string, content: string, overwrite = false) =>
    request<PlanArtifactOut>('POST', `/plan/sessions/${sessionId}/stages/${stage}/confirm`,
      { content, overwrite }),
  // 세션 마무리 — 작업 브랜치를 기본 브랜치로 반영
  mergePlanSession: (sessionId: number) =>
    request<PlanMergeOut>('POST', `/plan/sessions/${sessionId}/merge`),
  planBuildStatus: (sessionId: number) =>
    request<PlanBuildStatus>('GET', `/plan/sessions/${sessionId}/build-status`),
  planConstraints: (projectId: number) =>
    request<{ document: string }>('GET', `/plan/projects/${projectId}/constraints`),
  // 외주 빌드 작업 지시(work order)
  generatePlanTasks: (sessionId: number) =>
    request<BuildTaskOut[]>('POST', `/plan/sessions/${sessionId}/tasks/generate`),
  listPlanTasks: (sessionId: number) =>
    request<BuildTaskOut[]>('GET', `/plan/sessions/${sessionId}/tasks`),
  // 진행 현황을 기본 브랜치(main) 기준으로 갱신 — 보고가 아니라 반영된 커밋이 기준이다
  syncPlanTasks: (sessionId: number) =>
    request<BuildTaskSync>('POST', `/plan/sessions/${sessionId}/tasks/sync`),
  updatePlanTask: (taskId: number, body: { status?: string; note?: string }) =>
    request<BuildTaskOut>('PATCH', `/plan/tasks/${taskId}`, body),
  // 외주 결과의 LLM·모듈 사용 검증
  planCompliance: (projectId: number) =>
    request<ComplianceOut>('GET', `/plan/projects/${projectId}/compliance`),

  // 서버구성 (런타임/프록시 백엔드 시각화 + redirect/rewrite 규칙)
  serverConfig: () => request<ServerConfigOut>('GET', '/server-config'),
  listRedirects: (projectId: number) =>
    request<RedirectRule[]>('GET', `/projects/${projectId}/redirects`),
  createRedirect: (
    projectId: number, from_path: string, to_path: string, kind: string, status_code: number,
  ) =>
    request<RedirectRule>('POST', `/projects/${projectId}/redirects`, {
      from_path, to_path, kind, status_code,
    }),
  deleteRedirect: (id: number) => request<void>('DELETE', `/redirects/${id}`),

  // 프리뷰
  createPreview: (projectId: number, branch?: string, ttl_minutes = 60) =>
    request<PreviewOut>('POST', `/projects/${projectId}/preview`, {
      branch: branch || null, ttl_minutes,
    }),
  listPreviews: (projectId: number) =>
    request<PreviewOut[]>('GET', `/projects/${projectId}/previews`),
  deletePreview: (id: number) => request<void>('DELETE', `/previews/${id}`),

  // 계정 회원가입
  registerUser: (body: { email: string; name: string; password: string }) =>
    request<{ name: string; email: string; key: string; is_admin: boolean }>('POST', '/auth/register', body),

  // PowerShell 터미널 & 서버 로그
  terminalPreflight: () =>
    request<{
      shell: string;
      backend: string;
      // 설정이 'auto'일 때 실제로 골라진 백엔드 — Server 2016에서 무엇이 쓰였는지가
      // 여기서만 드러난다.
      resolved_backend: string;
      ok: boolean;
      // 기계가 읽는 사유 코드(pty_terminal.REASON_*). 화면에는 error·hint를 그대로
      // 띄우면 되지만, 서버에서 도는 진단 스크립트는 한글 문장을 쓸 수 없어 이걸 본다.
      reason: string;
      exit_status: number | null;
      error: string;
      hint: string;
    }>('GET', '/system/terminal/preflight'),

  execPowerShell: (command: string) =>
    request<{ command: string; returncode: number; output: string }>('POST', '/system/powershell/exec', { command }),
  listServerLogs: () =>
    request<{ files: { filename: string; relative_path: string; size_bytes: number; mtime: number }[]; log_dir: string }>('GET', '/system/server-logs'),
  getServerLogContent: (filename: string, tail_lines = 1000) =>
    request<{ filename: string; total_lines: number; tail_lines: number; content: string }>('GET', `/system/server-logs/content?filename=${encodeURIComponent(filename)}&tail_lines=${tail_lines}`),
  // 응답 형태는 app/api/system.py의 sw_update와 맞춘다(실패는 HTTP 500 + detail).
  swUpdate: () =>
    request<{ status: string; message: string; error: string | null; services: string[] }>(
      'POST', '/system/sw-update'),
};
