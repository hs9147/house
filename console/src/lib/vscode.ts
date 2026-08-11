// "VS Code로 작업" 버튼이 쓰는 순수 로직.
//
// clone은 서버가 아니라 사용자 PC에서 일어난다 — VS Code가 등록해 둔 vscode: URI
// 핸들러를 열면 VS Code가 뜨면서 clone 위치를 묻는다. 그래서 백엔드 API가 필요 없다.

/** VS Code에 clone을 요청하는 URI. VS Code의 내장 git 확장이 이 형식을 처리한다. */
export function vscodeCloneUri(gitUrl: string): string {
  return `vscode://vscode.git/clone?url=${encodeURIComponent(gitUrl)}`;
}

/**
 * 이 프로젝트를 VS Code로 열 수 있는지.
 *
 * git_url은 비관리자·비소속 사용자에게는 안내 문구로 마스킹돼 내려온다
 * (api/projects.py의 GIT_URL_MASK). 그 문구를 그대로 clone에 넘기면 VS Code가 열리고도
 * 실패하므로, 실제 주소일 때만 버튼을 활성화한다. 문구 자체를 비교하지 않는 이유는
 * 백엔드 문구가 바뀌어도 이 판정이 따라 깨지지 않게 하기 위함이다.
 */
export function canOpenInVscode(gitUrl: string | null | undefined): boolean {
  return typeof gitUrl === 'string' && /^https?:\/\//i.test(gitUrl.trim());
}
