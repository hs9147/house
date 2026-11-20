import { gitRepoWebUrl, hasUsableGitUrl } from '../lib/vscode';
import type { ProjectOut } from '../lib/types';

/**
 * "Git 조회" — 이 프로젝트 리포를 Gitea 웹 화면에서 새 창으로 연다.
 *
 * 대개 하려던 일은 "리포를 열어 보는 것"이라 주소 복사보다 앞에 둔다. 주소가 필요한 경우(다른
 * 도구의 clone 화면, 터미널의 git clone)는 오른쪽의 GitCopyButton이 맡는다.
 *
 * 새 창으로 여는 이유: 콘솔 작업 흐름(목록 → 상세)을 리포 화면으로 덮지 않는다.
 * 브라우저에 Gitea 세션이 없으면 Gitea 로그인 화면이 먼저 뜬다(SSO면 한 번이면 된다).
 */
export default function GitBrowseButton({ project }: { project: ProjectOut | null | undefined }) {
  const ready = hasUsableGitUrl(project?.git_url);
  return (
    <button
      className="small secondary"
      disabled={!ready}
      title={
        ready
          ? `${project!.name} 리포를 새 창에서 엽니다`
          : '리포 주소를 볼 수 있는 프로젝트만 열 수 있습니다'
      }
      // 프로젝트 목록은 행 클릭이 상세 이동이라 전파를 막아야 한다(VS Code 버튼과 동일).
      onClick={(e) => {
        e.stopPropagation();
        // noopener: 새 창이 window.opener로 콘솔 창을 건드리지 못하게 한다.
        window.open(gitRepoWebUrl(project!.git_url), '_blank', 'noopener,noreferrer');
      }}
    >
      Git 조회
    </button>
  );
}
