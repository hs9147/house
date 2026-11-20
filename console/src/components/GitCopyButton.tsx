import { useState } from 'react';
import { copyText } from '../lib/clipboard';
import { hasUsableGitUrl } from '../lib/vscode';
import type { ProjectOut } from '../lib/types';

/**
 * "Git 주소 복사" — 이 프로젝트의 clone 주소를 클립보드에 넣는다. VS Code가 아닌 도구(Orca 등)의 clone 화면이나
 * 터미널의 git clone에 붙여 넣는 용도다.
 *
 * 예전에 이 버튼을 뺀 이유는 평문 http(사내 접속)에서 navigator.clipboard가 없어 복사가 안 됐기 때문인데,
 * lib/clipboard의 copyText가 옛 방식으로도 시도하고 **성공 여부를 돌려준다** — 실패하면 주소를 직접 고를 수 있게
 * 띄운다. 활성화 조건은 다른 리포 버튼과 같다(마스킹된 git_url이면 끈다).
 */
export default function GitCopyButton({ project }: { project: ProjectOut | null | undefined }) {
  const ready = hasUsableGitUrl(project?.git_url);
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    const url = project!.git_url.trim();
    if (await copyText(url)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } else {
      window.prompt('복사하지 못했습니다 — 아래 주소를 직접 복사하세요.', url);
    }
  };

  return (
    <button
      className="small secondary"
      disabled={!ready}
      title={ready ? `${project!.name} 리포의 clone 주소를 복사합니다` : '리포 주소를 볼 수 있는 프로젝트만 복사할 수 있습니다'}
      // 프로젝트 목록은 행 클릭이 상세 이동이라 전파를 막아야 한다(다른 리포 버튼과 동일).
      onClick={(e) => { e.stopPropagation(); void copy(); }}
    >
      {copied ? '복사됨' : 'Git 주소 복사'}
    </button>
  );
}
