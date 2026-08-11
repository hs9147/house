// 프로젝트 리포 버튼("→ VS Code", "↓ ZIP")이 쓰는 순수 로직.
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
 * 이 프로젝트의 git_url이 실제 주소인지 — 두 버튼의 활성화 조건.
 *
 * git_url은 비관리자·비소속 사용자에게는 안내 문구로 마스킹돼 내려온다
 * (api/projects.py의 GIT_URL_MASK). 그 문구를 그대로 넘기면 VS Code가 열리고도 실패하거나
 * 깨진 주소로 이동하므로, 실제 주소일 때만 버튼을 활성화한다. 문구 자체를 비교하지 않는
 * 이유는 백엔드 문구가 바뀌어도 이 판정이 따라 깨지지 않게 하기 위함이다.
 */
export function hasUsableGitUrl(gitUrl: string | null | undefined): boolean {
  return typeof gitUrl === 'string' && /^https?:\/\//i.test(gitUrl.trim());
}
