// platform/app/schemas.py 미러 — 백엔드 스키마 변경 시 이 파일을 함께 갱신한다.

export type ProjectType = 'react' | 'python' | 'node' | 'llm' | 'html' | 'streamlit' | 'composite';
export type BuildProfile = 'development' | 'release';
export type DeploymentStatus = 'building' | 'running' | 'failed' | 'stopped';

export interface ProjectOut {
  id: number;
  name: string;
  type: ProjectType;
  organization_id: number | null;
  org_name?: string | null;
  // organization_id로 생성된 프로젝트는 비관리자에게 마스킹된 값이 온다
  git_url: string;
  branch: string;
  domain: string | null;
  default_profile: BuildProfile;
  created_at: string;
}

export interface ProjectCreate {
  name: string;
  type: ProjectType;
  // 둘 중 하나만 — organization_id 지정 시 리포를 내부에서 자동 생성(git_url 불가)
  organization_id?: number | null;
  git_url?: string;
  branch: string;
  domain?: string | null;
  health_check_path?: string;
  default_profile?: BuildProfile;
}

export interface OrgOut {
  id: number;
  name: string;
  created_at: string;
  project_count: number;
}

export interface GiteaSyncSkip {
  name: string;
  kind: 'org' | 'project';
  reason: string;
}

export interface GiteaSyncResult {
  orgs_created: string[];
  projects_created: string[];
  repos_created: string[];
  projects_deleted: string[];
  skipped: GiteaSyncSkip[];
}

export interface DeploymentOut {
  id: number;
  project_id: number;
  git_sha: string;
  image_tag: string;
  profile: BuildProfile;
  status: DeploymentStatus;
  host_port: number | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  // composite 프로젝트에서만 값이 있음 — "backend" | "frontend"
  component?: string | null;
}

export interface EnvVarRow {
  key: string;
  is_secret: boolean;
  value: string; // 마스킹된 표시값
}

export interface ModuleOut {
  id: number;
  name: string;
  type: string;
  category: string | null;
  organization_id: number | null;
  config: Record<string, unknown>;
}

// 계정 승인 — 가입은 신청일 뿐이고 관리자가 승인해야 로그인할 수 있다
export interface UserOrgOut {
  id: number;
  name: string;
}

export interface UserAccountOut {
  id: number;
  email: string;
  name: string;
  is_approved: boolean;
  is_admin: boolean;
  organization_id?: number | null;
  organization_name?: string | null;
  organizations?: UserOrgOut[];
}

// 파일 저장소 — 로컬 경로 대신 창구 URL로만 다룬다 (services/storage.py)
export interface StorageFile {
  path: string;
  size: number;
}

export interface StorageListing {
  module: string;
  url: string;
  files: StorageFile[];
}

// 바인딩된 모듈은 A2A Agent Card 모양으로 내려온다 (services/a2a.build_agent_card)
export interface ModuleSummary {
  agent_name: string;
  type: string;
  category: string;
  description: string;
  skills: string[];
  env_prefix: string;
}

// 대화식 편집 화면 자원 리스팅 — 바인딩 여부와 무관하게 사용 가능한 모듈을 아이템화
export interface ResourceItem {
  id: number;
  name: string;
  type: string;
  category: string | null;
  scope: 'global' | 'org';
  env_prefix?: string;
  bound?: boolean;
}

export interface LlmProviderOut {
  id: number;
  name: string;
  kind: 'external' | 'internal';
  base_url: string;
  model: string;
  has_api_key: boolean;
  // 미지정(null) = 전역(모든 프로젝트에서 사용 가능), 지정 시 해당 조직 소속 프로젝트에서만 사용 가능
  organization_id?: number | null;
  org_name?: string | null;
}

// 에이전트 기획 (Agent Planning)
export interface PlanArtifactOut {
  stage: 'spec' | 'architecture' | 'solution' | 'principles';
  title: string;
  repo_path: string;
  commit_sha: string | null;
  confirmed: boolean;
  default_request?: string; // 입력창 기본값(바로 생성 요청 가능)
  // 확정 시 git 상태에 따라 자동 수행된 결과
  git_action?: 'committed' | 'merged' | 'pr_opened' | 'skipped' | null;
  git_detail?: string | null;
  pull_request_url?: string | null;
}

export interface PlanSessionOut {
  id: number;
  branch: string;
  provider: string;
  project_id: number;
  project_name: string;
  artifacts: PlanArtifactOut[];
}

export interface PlanMessageReply {
  summary: string; // 대화창에 보일 응답 개요
  document: string; // 산출물 편집기에 들어갈 문서 본문
  used_modules?: string[];
  context_files?: string[]; // 본문까지 참조한 리포 파일
  bound_modules?: string[]; // 솔루션 구성 단계에서 이번에 바인딩된 모듈
}

export interface PlanArtifactContent {
  stage: string;
  repo_path: string;
  content: string;
  confirmed: boolean;
  // session = 이 세션에서 확정 · repo = 리포에 이미 있던 문서 · '' = 없음
  source: 'session' | 'repo' | '';
}

// 기획 세션 이력 — 재개·삭제 대상 선택용
export interface PlanSessionSummary {
  id: number;
  project_id: number;
  project_name: string;
  provider: string;
  branch: string;
  confirmed_stages: string[];
  task_count: number;
  created_at: string | null;
}

export interface PlanChatMessage {
  role: 'user' | 'assistant';
  content: string;
  created_at: string | null;
}

// 외주 빌드 작업 지시(work order)
export type BuildTaskStatus = 'pending' | 'in_progress' | 'done' | 'blocked';

export interface BuildTaskOut {
  id: number;
  title: string;
  detail: string;
  verify: string; // 완료 판정 기준
  status: BuildTaskStatus;
  note: string;
  commit_sha: string | null;
}

// 외주 빌드 결과의 LLM·모듈 사용 검증
export interface ComplianceFinding {
  rule: string;
  file: string;
  line: number;
  snippet: string;
  detail: string;
}

export interface ComplianceOut {
  project: string;
  findings: ComplianceFinding[];
  summary: Record<string, number>;
  builder_prompt: string;
}

export interface PlanBuildEvent {
  actor: string;
  action: string;
  detail: Record<string, unknown> | null;
  created_at: string | null;
}

export interface PlanBuildStatus {
  project: string;
  branch: string;
  events: PlanBuildEvent[];
}

export interface ReviewFinding {
  severity: string;
  file: string;
  comment: string;
}

export interface ReviewResult {
  findings: ReviewFinding[];
  max_severity: string;
}

export interface PreviewOut {
  id: number;
  project_id: number;
  branch: string;
  url: string;
  status: 'running' | 'expired' | 'failed';
  expires_at: string;
}

export interface ProjectFilesOut {
  files: string[];
}

// 코드 구조 시각화 — 정적 파싱으로 만든 파일→클래스/함수 계층 트리(요청 1)
export interface CodeMapNode {
  kind: 'class' | 'function' | 'method';
  name: string;
  signature: string;
  doc: string;
  lineno: number;
  children: CodeMapNode[];
}

export interface CodeMapFile {
  path: string;
  lang: string;
  summary: string;
  children: CodeMapNode[];
}

export interface CodeMapOut {
  files: CodeMapFile[];
}

// 외부 API 디렉터리 검색 결과(요청 3)
export interface ApiSearchResult {
  id: string;
  title: string;
  description: string;
  provider: string;
  categories: string[];
  homepage: string;
  spec_url: string;
}

export interface ProjectFileContentOut {
  path: string;
  content: string;
}

// composite 프로젝트의 컴포넌트별 상태 — 서버구성 표 + 토폴로지 다이어그램이 함께 쓴다
export interface ComponentStatus {
  name: string; // "backend" | "frontend"
  status: string;
  internal_port: number | null;
}

export interface RedirectRuleSummary {
  from_path: string;
  to_path: string;
  kind: 'redirect' | 'rewrite';
  status_code: number;
}

// 서버구성 시각화 — 런타임/프록시 백엔드 + 등록된 사이트(라우팅 항목) 목록
export interface ServerConfigSite {
  project_id: number;
  project_name: string;
  profile: BuildProfile;
  domain: string;
  path_prefix: string;
  status: string;
  redirect_count: number;
  redirects: RedirectRuleSummary[];
  // composite 프로젝트만 채워짐 — 일반 프로젝트는 null
  components: ComponentStatus[] | null;
  // 프록시 설정(IIS web.config 등)에 실제로 라우팅이 구성됐는지 — 추적하지 않는
  // 백엔드(caddy/apache)에서는 null
  in_proxy: boolean | null;
}

export interface UnregisteredSite {
  name: string;
  rewrite_targets: string[];
}

export interface ServerConfigOut {
  runtime_backend: string;
  proxy_backend: string;
  sites: ServerConfigSite[];
  // 프록시 설정(web.config)에만 있고 DB 프로젝트로 등록되지 않은 항목 (추적 백엔드만)
  unregistered: UnregisteredSite[];
}

export interface RedirectRule {
  id: number;
  project_id: number;
  from_path: string;
  to_path: string;
  kind: 'redirect' | 'rewrite';
  status_code: number;
  created_at: string;
}

export interface AuditRow {
  actor: string;
  action: string;
  target: string;
  detail: Record<string, unknown> | null;
  at: string;
}

export interface GpuInfo {
  index: number;
  name: string;
  vram_total: number;
  vram_used: number;
  util_percent: number;
}

export interface StatusSnapshot {
  host_os?: string;
  gpu_supported?: boolean;
  docker_hint?: string;
  cpu_percent?: number;
  memory?: { total: number; used: number; percent: number };
  disk?: { total: number; used: number; percent: number };
  gpus: GpuInfo[];
  system?: string;
}

export interface HealthInfo {
  ok: boolean;
  platform_name?: string;
  tier: string;
  host_os: string;
  features: string[];
  gitea_url: string | null;
}

export interface ApiKeyIssued {
  name: string;
  key: string;
  is_admin: boolean;
}

export interface McpDirectoryItem {
  id: string;
  name: string;
  category: string;
  description: string;
  url: string;
  vendor: string;
}

export interface ModuleUsageItem {
  id: number;
  name: string;
  type: string;
  category?: string | null;
  env_prefix: string;
  injected_env_keys: string[];
}

export interface ModuleHistoryItem {
  id: number;
  actor: string;
  action: string;
  target: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ProjectModuleReportOut {
  project_id: number;
  project_name: string;
  org_name?: string | null;
  total_active_modules: number;
  total_injected_envs: number;
  active_modules: ModuleUsageItem[];
  history: ModuleHistoryItem[];
}

export interface GlobalModuleUsageSummary {
  module_id: number;
  module_name: string;
  type: string;
  category?: string | null;
  organization_name?: string | null;
  bound_project_count: number;
  bound_projects: string[];
  created_at: string;
}

export interface PlatformModuleReportOut {
  total_modules: number;
  total_bindings: number;
  modules: GlobalModuleUsageSummary[];
  recent_history: ModuleHistoryItem[];
}
