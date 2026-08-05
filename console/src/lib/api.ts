import { getKey, logout } from './auth';
import type {
  ApiKeyIssued,
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
  ResourceItem,
  ReviewResult,
  ServerConfigOut,
  StatusSnapshot,
  StorageListing,
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

/** 파일 다운로드 — x-api-key가 필요해 <a href>로는 못 받는다. Blob으로 받아 저장한다. */
async function requestBlob(path: string, query: Record<string, string>): Promise<Blob> {
  const res = await fetch(`${apiUrl(path)}?${new URLSearchParams(query).toString()}`, {
    headers: { 'x-api-key': getKey() },
  });
  if (res.status === 401) {
    logout();
    window.location.hash = '#/login';
    throw new ApiError(401, '인증이 만료되었습니다. 다시 로그인하세요.');
  }
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  return res.blob();
}

export const api = {
  // 시스템
  health: () => request<HealthInfo>('GET', '/health'),
  status: () => request<StatusSnapshot>('GET', '/status'),
  audit: (limit = 100) => request<AuditRow[]>('GET', '/audit', undefined, { limit }),
  issueKey: (name: string, is_admin: boolean) =>
    request<ApiKeyIssued>('POST', '/keys', { name, is_admin }),

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
      domain?: string;
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
    if (form.domain) fd.append('domain', form.domain);
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
  deploy: (id: number, profile?: BuildProfile, git_sha?: string) =>
    request<DeploymentOut>('POST', `/projects/${id}/deploy`, {
      profile: profile ?? null,
      git_sha: git_sha || null,
    }),
  // 비블로킹 배포 — 즉시 building 레코드(들)를 받아 진행 상황을 폴링으로 추적한다.
  // composite 프로젝트는 backend/frontend 두 레코드를 배열로 돌려준다.
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
  // 외부 API 디렉터리 검색 + external_api 모듈 자동 추가 (admin)
  searchApis: (keyword: string) =>
    request<{ results: ApiSearchResult[] }>('GET', '/modules/search', undefined, { keyword }),
  importApiModule: (name: string, url: string, category?: string) =>
    request<ModuleOut>('POST', '/modules/import', { name, url, category: category || null }),

  // 외부 MCP 디렉터리 검색 + mcp 모듈 자동 추가 (admin)
  searchMcpDirectory: (q?: string) =>
    request<McpDirectoryItem[]>('GET', `/mcp/search?q=${encodeURIComponent(q || '')}`),
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
  uploadFileStorageModule: (
    zipFile: File,
    name: string,
    category?: string,
    organization_id?: number,
  ) => {
    const fd = new FormData();
    fd.append('zip_file', zipFile);
    fd.append('name', name);
    if (category) fd.append('category', category);
    if (organization_id) fd.append('organization_id', String(organization_id));
    return requestMultipart<ModuleOut>('/modules/upload-storage', fd);
  },
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

  // 파일 저장소 — 로컬 경로는 노출되지 않고 /storage/{모듈} 창구로만 다룬다
  listStorageFiles: (module: string) =>
    request<StorageListing>('GET', `/storage/${module}/files`),
  uploadStorageFile: (module: string, file: File, path?: string) => {
    const fd = new FormData();
    fd.append('file', file);
    if (path) fd.append('path', path);
    return requestMultipart<{ path: string }>(`/storage/${module}/files`, fd);
  },
  deleteStorageFile: (module: string, path: string) =>
    request<void>('DELETE', `/storage/${module}/files`, undefined, { path }),
  downloadStorageFile: (module: string, path: string) =>
    requestBlob(`/storage/${module}/files/content`, { path }),
  projectModules: (id: number) => request<ModuleSummary[]>('GET', `/projects/${id}/modules`),
  projectResources: (id: number) => request<ResourceItem[]>('GET', `/projects/${id}/resources`),
  bindModule: (projectId: number, moduleId: number, env_prefix: string) =>
    request<{ injected_env: string[] }>(
      'POST', `/projects/${projectId}/modules/${moduleId}/bind`, { env_prefix },
    ),

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

  // PowerShell 터미널 & 빌드 로그
  execPowerShell: (command: string) =>
    request<{ command: string; returncode: number; output: string }>('POST', '/system/powershell/exec', { command }),
  listBuildLogs: () =>
    request<{ files: { filename: string; relative_path: string; size_bytes: number; mtime: number }[]; log_dir: string }>('GET', '/system/build-logs'),
  getBuildLogContent: (filename: string, tail_lines = 1000) =>
    request<{ filename: string; total_lines: number; tail_lines: number; content: string }>('GET', `/system/build-logs/content?filename=${encodeURIComponent(filename)}&tail_lines=${tail_lines}`),
  swUpdate: () =>
    request<{ status: string; message: string; services?: string[]; error?: string | null }>(
      'POST', '/system/sw-update'),
};
