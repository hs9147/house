import { canOpenInVscode, vscodeCloneUri } from '../lib/vscode';
import type { ProjectOut } from '../lib/types';

/**
 * "VS Code" — 이 프로젝트 리포를 사용자 PC의 VS Code로 clone한다.
 *
 * clone은 서버가 아니라 사용자 PC에서 일어난다(lib/vscode.ts 참고) — VS Code가 등록해 둔
 * vscode: URI 핸들러를 여는 것이라 백엔드 호출이 없다.
 *
 * git_url은 비관리자·비소속 사용자에게 안내 문구로 마스킹돼 내려오므로(GIT_URL_MASK),
 * 실제 주소일 때만 활성화한다 — 마스킹 문구를 clone에 넘기면 VS Code만 열리고 실패한다.
 */
export default function VscodeWorkButton({ project }: { project: ProjectOut | null | undefined }) {
  const ready = canOpenInVscode(project?.git_url);
  return (
    <button
      className="small secondary"
      disabled={!ready}
      title={
        ready
          ? `${project!.name} 리포를 VS Code로 clone합니다 (첫 push 때 브라우저로 로그인)`
          : '리포 주소를 볼 수 있는 프로젝트만 열 수 있습니다'
      }
      // 프로젝트 목록은 행 클릭이 상세 이동이라 전파를 막아야 한다(삭제 버튼과 동일).
      onClick={(e) => { e.stopPropagation(); openInVscode(project!.git_url); }}
    >
      VS Code
    </button>
  );
}

function openInVscode(gitUrl: string) {
  window.location.href = vscodeCloneUri(gitUrl);
}
