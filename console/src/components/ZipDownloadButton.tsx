import { gitArchiveZipUrl, hasUsableGitUrl } from '../lib/vscode';
import type { ProjectOut } from '../lib/types';

/**
 * "↓ ZIP" — 이 프로젝트 리포를 zip으로 내려받는다.
 *
 * VscodeWorkButton과 짝이다 — git을 쓰지 않고 소스만 한 벌 받아 보려는 경우.
 * 압축은 Gitea가 만들어 바로 내려주므로 플랫폼 백엔드를 거치지 않는다. 브라우저에
 * Gitea 세션이 없으면 로그인 화면으로 먼저 간다(SSO면 한 번 누르면 된다).
 */
export default function ZipDownloadButton({ project }: { project: ProjectOut | null | undefined }) {
  const ready = hasUsableGitUrl(project?.git_url);
  return (
    <button
      className="small secondary"
      disabled={!ready}
      title={
        ready
          ? `${project!.name} 리포의 ${project!.branch} 브랜치를 zip으로 내려받습니다`
          : '리포 주소를 볼 수 있는 프로젝트만 내려받을 수 있습니다'
      }
      // 프로젝트 목록은 행 클릭이 상세 이동이라 전파를 막아야 한다(VS Code 버튼과 동일).
      onClick={(e) => {
        e.stopPropagation();
        window.location.href = gitArchiveZipUrl(project!.git_url, project!.branch);
      }}
    >
      ↓ ZIP
    </button>
  );
}
