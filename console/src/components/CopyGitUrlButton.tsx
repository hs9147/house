import { useEffect, useState } from 'react';
import { hasUsableGitUrl } from '../lib/vscode';
import type { ProjectOut } from '../lib/types';

const SNACKBAR_MS = 2000;

/**
 * "복사" — 이 프로젝트의 git 주소를 클립보드에 넣고 스낵바로 알린다.
 *
 * navigator.clipboard는 보안 컨텍스트(https 또는 localhost)에서만 있다 — 평문 http로
 * 접속하면 아예 undefined다. 이 플랫폼은 사내에서 http로도 접근하므로, 조용히 아무 일도
 * 안 일어난 것처럼 보이지 않게 실패도 스낵바로 알린다.
 */
export default function CopyGitUrlButton({ project }: { project: ProjectOut | null | undefined }) {
  const ready = hasUsableGitUrl(project?.git_url);
  const [message, setMessage] = useState('');

  useEffect(() => {
    if (!message) return;
    const t = setTimeout(() => setMessage(''), SNACKBAR_MS);
    return () => clearTimeout(t);
  }, [message]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(project!.git_url);
      setMessage('Git 주소를 복사했습니다');
    } catch {
      setMessage('복사할 수 없습니다 — 주소를 직접 선택해 복사하세요');
    }
  };

  return (
    <>
      <button
        className="small secondary"
        disabled={!ready}
        title={ready ? `${project!.name}의 git 주소를 복사합니다` : '리포 주소를 볼 수 있는 프로젝트만 복사할 수 있습니다'}
        // 프로젝트 목록은 행 클릭이 상세 이동이라 전파를 막아야 한다(VS Code 버튼과 동일).
        onClick={(e) => { e.stopPropagation(); void copy(); }}
      >
        복사
      </button>
      {message && <div className="snackbar">{message}</div>}
    </>
  );
}
