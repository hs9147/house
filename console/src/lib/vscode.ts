// 프로젝트 리포 버튼("VS Code로 작업", "ZIP 다운로드")이 쓰는 순수 로직.
//
// 둘 다 백엔드 API를 거치지 않는다 — clone은 사용자 PC의 VS Code가(등록해 둔 vscode:
// URI 핸들러), zip은 Gitea가 직접 내려준다.

/** VS Code에 clone을 요청하는 URI. VS Code의 내장 git 확장이 이 형식을 처리한다. */
export function vscodeCloneUri(gitUrl: string): string {
  return `vscode://vscode.git/clone?url=${encodeURIComponent(gitUrl)}`;
}

/**
 * Gitea의 zip 아카이브 주소 — `<리포>/archive/<브랜치>.zip`.
 *
 * git_url 끝의 `.git`은 clone에는 있어도 되지만 아카이브 경로에는 들어가면 안 된다
 * (`repo.git/archive/...`는 없는 리포로 404). 브랜치 이름에는 `/`가 들어갈 수 있으므로
 * (`feature/x`) 경로 구분자로 살려 두는 encodeURI를 쓴다 — encodeURIComponent로 `%2F`가
 * 되면 Gitea가 ref를 못 찾는다.
 */
export function gitArchiveZipUrl(gitUrl: string, branch: string): string {
  const repo = gitUrl.trim().replace(/\/+$/, '').replace(/\.git$/i, '');
  return `${repo}/archive/${encodeURI(branch.trim() || 'main')}.zip`;
}

/**
 * 리포의 Gitea 웹 페이지 주소 — clone 주소에서 끝의 `.git`만 뗀 것.
 *
 * Gitea는 `<host>/<owner>/<repo>`가 곧 웹 화면이라 clone 주소와 경로가 같다. `.git`을
 * 남기면 없는 리포로 404가 된다(gitArchiveZipUrl과 같은 이유).
 */
export function gitRepoWebUrl(gitUrl: string): string {
  return gitUrl.trim().replace(/\/+$/, '').replace(/\.git$/i, '');
}

/**
 * 이 프로젝트의 git_url이 실제 주소인지 — 리포 버튼들의 활성화 조건.
 *
 * git_url은 비관리자·비소속 사용자에게는 안내 문구로 마스킹돼 내려온다
 * (api/projects.py의 GIT_URL_MASK). 그 문구를 그대로 넘기면 VS Code가 열리고도 실패하거나
 * 깨진 주소로 이동하므로, 실제 주소일 때만 버튼을 활성화한다. 문구 자체를 비교하지 않는
 * 이유는 백엔드 문구가 바뀌어도 이 판정이 따라 깨지지 않게 하기 위함이다.
 */
export function hasUsableGitUrl(gitUrl: string | null | undefined): boolean {
  return typeof gitUrl === 'string' && /^https?:\/\//i.test(gitUrl.trim());
}

// "Orca로 작업" — Orca에는 vscode:처럼 주소로 clone하는 링크가 없다(orca: 링크는 공유 스킬·봇·페어링뿐이고
// CLI의 `orca repo add`도 이미 받은 폴더 경로만 받는다). 그래서 clone은 git이, 등록은 orca가 하도록
// PowerShell 한 줄을 만들어 사용자가 붙여 넣게 한다. 경로는 큰따옴표로 감싸 $env:USERPROFILE을 펼치고,
// 큰따옴표·백틱만 PowerShell 방식(`)으로 이스케이프한다. 주소는 펼칠 것이 없으니 작은따옴표로 감싼다.

/** clone할 기본 위치 — 사용자 폴더 아래 Projects\<프로젝트 이름>. */
const BACKSLASH = '\u005c';

export function orcaDefaultPath(projectName: string): string {
  return ['$env:USERPROFILE', 'Projects', projectName].join(BACKSLASH);
}

function psPath(path: string): string {
  return `"${path.trim().replace(/[`"]/g, (c) => `\`${c}`)}"`;
}

/** 설치 페이지 — 프로그램이 없을 때 새 창(브라우저 새 탭)으로 연다. */
export const INSTALL_URLS = {
  vscode: 'https://code.visualstudio.com/download',
  orca: 'https://orca.dev/download',
  git: 'https://git-scm.com/downloads/win',
} as const;

// 붙여 넣은 PC에 git·orca가 없으면 하던 일을 시작하지 않고 설치 페이지를 연다(Start-Process는 기본 브라우저로
// 연다). 검사 없이 돌리면 git만 있는 PC에서 clone은 되고 등록은 「orca 인식 안 됨」으로 실패해, 등록 안 된 폴더가 남는다.
function ifInstalled(tools: ('git' | 'orca')[], body: string): string {
  const missing = tools.map((t, i) => `${i ? 'elseif' : 'if'} (-not (Get-Command ${t} -ErrorAction SilentlyContinue)) `
    + `{ '${t === 'git' ? 'git이' : 'Orca가'} 설치돼 있지 않습니다 — 설치 페이지를 엽니다'; Start-Process '${INSTALL_URLS[t]}' }`);
  return `${missing.join(' ')} else { ${body} }`;
}

// orca open: Orca 창이 이미 떠 있으면 아무것도 하지 않고, 꺼져 있으면 앞으로 띄우지 않고 켠다 — 앱이 꺼져 있으면
// repo add가 런타임에 닿지 못한다.
const ORCA_REGISTER = (path: string) => `orca open; orca repo add --path ${psPath(path)}`;

/** 리포를 받아 Orca에 등록하는 PowerShell 한 줄 — git·Orca가 없으면 설치 페이지를 열고, clone이 실패하면 등록하지 않는다. */
export function orcaCloneCommand(gitUrl: string, path: string): string {
  const url = `'${gitUrl.trim().replace(/'/g, "''")}'`;
  return ifInstalled(['git', 'orca'], `git clone ${url} ${psPath(path)}; if ($?) { ${ORCA_REGISTER(path)} }`);
}

/** 이미 받아 둔 폴더를 Orca에 등록만 하는 명령 — Orca가 없으면 설치 페이지를 연다. */
export function orcaAddCommand(path: string): string {
  return ifInstalled(['orca'], ORCA_REGISTER(path));
}
