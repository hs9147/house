import { useState } from 'react';
import Modal from './Modal';
import { copyText } from '../lib/clipboard';
import { hasUsableGitUrl, INSTALL_URLS, orcaAddCommand, orcaCloneCommand, orcaDefaultPath } from '../lib/vscode';
import type { ProjectOut } from '../lib/types';

/**
 * "Orca로 작업" — 이 프로젝트 리포를 사용자 PC에 받아 Orca에 등록하는 PowerShell 명령을 복사해 준다.
 *
 * VscodeWorkButton과 짝이지만 링크 한 번으로 끝나지 않는다: Orca에는 주소로 clone하는 링크가 없어서
 * (lib/vscode.ts 참고) clone은 git이, 등록은 `orca repo add`가 한다. 백엔드 호출은 없다.
 * 활성화 조건은 VS Code 버튼과 같다(마스킹된 git_url이면 끈다).
 */
export default function OrcaWorkButton({ project }: { project: ProjectOut | null | undefined }) {
  const ready = hasUsableGitUrl(project?.git_url);
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        className="small secondary"
        disabled={!ready}
        title={ready ? `${project!.name} 리포를 받아 Orca에 등록하는 명령을 복사합니다` : '리포 주소를 볼 수 있는 프로젝트만 열 수 있습니다'}
        // 프로젝트 목록은 행 클릭이 상세 이동이라 전파를 막아야 한다(VS Code 버튼과 동일).
        onClick={(e) => { e.stopPropagation(); setOpen(true); }}
      >
        Orca로 작업
      </button>
      {open && project && <OrcaWorkModal project={project} onClose={() => setOpen(false)} />}
    </>
  );
}

function OrcaWorkModal({ project, onClose }: { project: ProjectOut; onClose: () => void }) {
  const [path, setPath] = useState(orcaDefaultPath(project.name));
  const [note, setNote] = useState('');
  const clone = orcaCloneCommand(project.git_url, path);
  const add = orcaAddCommand(path);

  // 평문 http에서는 navigator.clipboard가 없다 — copyText가 되는 길로 복사하고 결과를 돌려준다.
  const copy = async (text: string) => {
    setNote((await copyText(text))
      ? '복사했습니다. PowerShell에 붙여 넣고 Enter를 누르세요.'
      : '복사하지 못했습니다 — 명령을 직접 선택해 복사하세요.');
  };

  return (
    // 목록 행 위에 뜨므로, 팝업 안의 클릭이 행 클릭(상세 이동)으로 번지지 않게 막는다.
    <div onClick={(e) => e.stopPropagation()}>
      <Modal title={`Orca로 작업 — ${project.name}`} onClose={onClose} wide>
        <label className="field">
          받을 위치 (이 PC)
          <input className="mono" style={{ width: '100%' }} value={path} onChange={(e) => setPath(e.target.value)} />
        </label>
        <p style={{ fontSize: 13, margin: '12px 0 4px' }}>처음이면 — 리포를 받아 Orca에 등록합니다</p>
        <div className="row" style={{ gap: 6, alignItems: 'flex-start' }}>
          <pre className="mono" style={{ flex: 1, margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{clone}</pre>
          <button className="small" onClick={() => void copy(clone)}>복사</button>
        </div>
        <p style={{ fontSize: 13, margin: '12px 0 4px' }}>이미 받아 두었으면 — 등록만 합니다</p>
        <div className="row" style={{ gap: 6, alignItems: 'flex-start' }}>
          <pre className="mono" style={{ flex: 1, margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{add}</pre>
          <button className="small secondary" onClick={() => void copy(add)}>복사</button>
        </div>
        <p className="mutedtext" style={{ fontSize: 12 }}>
          git과 Orca가 있어야 합니다(orca 명령은 Orca 설치 때 PATH에 들어갑니다). 없으면 명령이 아무것도 받지 않고
          설치 페이지를 엽니다 — 설치한 뒤 새 PowerShell 창에서 다시 붙여 넣으세요. 등록되면 Orca 사이드바에 이
          프로젝트가 나타나고, 첫 push 때 브라우저로 git 로그인을 합니다.
        </p>
        <p style={{ fontSize: 12, margin: '4px 0' }}>
          설치 페이지(새 창):{' '}
          <a href={INSTALL_URLS.orca} target="_blank" rel="noopener noreferrer">Orca</a>{' · '}
          <a href={INSTALL_URLS.git} target="_blank" rel="noopener noreferrer">git</a>
        </p>
        {note && <p className="mutedtext">{note}</p>}
      </Modal>
    </div>
  );
}
