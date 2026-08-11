import { useState } from 'react';
import Modal from './Modal';
import { canOpenInVscode, vscodeCloneUri } from '../lib/vscode';
import type { ProjectOut } from '../lib/types';

/** VS Code를 열어 clone을 요청한다. clone은 사용자 PC에서 일어난다(lib/vscode.ts 참고). */
function openInVscode(gitUrl: string) {
  window.location.href = vscodeCloneUri(gitUrl);
}

/**
 * "VS Code로 작업" — 프로젝트 리포를 사용자 PC의 VS Code로 clone한다.
 *
 * project가 null이면(프로젝트 목록 화면처럼 선택 개념이 없는 곳) 고를 수 있게 목록을
 * 띄우고, 주어지면(기획 화면처럼 이미 선택돼 있는 곳) 바로 연다.
 */
export default function VscodeWorkButton({
  project, projects, disabledHint,
}: {
  project?: ProjectOut | null;
  projects?: ProjectOut[];
  disabledHint?: string;
}) {
  const [picking, setPicking] = useState(false);

  // 프로젝트가 지정된 경우: 열 수 있을 때만 활성화한다. git_url이 마스킹돼 내려오는
  // 사용자(비관리자·비소속)는 눌러도 실패하므로 이유를 툴팁으로 알린다.
  if (project !== undefined) {
    const ready = canOpenInVscode(project?.git_url);
    return (
      <button
        className="secondary"
        disabled={!ready}
        title={
          ready
            ? `${project!.name} 리포를 VS Code로 clone합니다`
            : disabledHint ?? '프로젝트를 먼저 선택하세요 (리포 주소를 볼 수 있는 프로젝트만 가능)'
        }
        onClick={() => openInVscode(project!.git_url)}
      >
        VS Code로 작업
      </button>
    );
  }

  const openable = (projects ?? []).filter((p) => canOpenInVscode(p.git_url));
  return (
    <>
      <button className="secondary" onClick={() => setPicking(true)}>VS Code로 작업</button>
      {picking && (
        <Modal title="VS Code로 작업할 프로젝트" onClose={() => setPicking(false)}>
          {openable.length === 0 ? (
            <p className="mutedtext" style={{ fontSize: 13 }}>
              열 수 있는 프로젝트가 없습니다. 리포 주소는 해당 조직 소속 사용자에게만
              보이므로, 소속되지 않은 프로젝트는 여기 나타나지 않습니다.
            </p>
          ) : (
            <>
              <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
                선택하면 VS Code가 열리며 clone 위치를 묻습니다. 처음 push할 때는
                비밀번호가 아니라 <b>Gitea 액세스 토큰</b>을 입력해야 합니다.
              </p>
              <div style={{ maxHeight: 320, overflow: 'auto' }}>
                {openable.map((p) => (
                  <button
                    key={p.id}
                    className="secondary"
                    style={{ display: 'block', width: '100%', textAlign: 'left', marginBottom: 6 }}
                    onClick={() => { openInVscode(p.git_url); setPicking(false); }}
                  >
                    {p.org_name ? `[${p.org_name}] ${p.name}` : p.name}
                    <span className="mono mutedtext" style={{ fontSize: 11, display: 'block' }}>
                      {p.git_url}
                    </span>
                  </button>
                ))}
              </div>
            </>
          )}
          <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}>
            <button className="secondary" onClick={() => setPicking(false)}>닫기</button>
          </div>
        </Modal>
      )}
    </>
  );
}
