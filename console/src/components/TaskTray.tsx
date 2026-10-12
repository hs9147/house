import { Link } from 'react-router-dom';
import { dismissTask, useTasks } from '../lib/tasks';

const STATE: Record<string, string> = { done: '완료', failed: '실패', cancelled: '취소됨' };

/**
 * 진행 중인 작업 — 어느 화면에서든 보인다(lib/tasks). 그 작업의 화면이어도 띄운다: 폴더
 * 동기화는 스마트워크의 「설정」 탭에 있어서, 「대화」 탭에 있으면 화면이 같아도 보이지 않는다.
 */
export default function TaskTray() {
  const shown = useTasks();
  if (shown.length === 0) return null;
  return (
    <div className="task-tray" role="status">
      {shown.map((t) => (
        <div key={t.key} className="task-tray-item">
          <div className="row" style={{ gap: 6 }}>
            <b style={{ fontSize: 12 }}>{t.label}</b>
            {t.state !== 'running' && <span className="status dim">{STATE[t.state]}</span>}
            <span className="spacer" />
            {t.to && <Link to={t.to} style={{ fontSize: 12 }}>보기</Link>}
            {t.state !== 'running' && (
              <button className="small secondary" aria-label="닫기" onClick={() => dismissTask(t.key)}>×</button>
            )}
          </div>
          {t.sent !== undefined && t.total ? (
            <div className="progress-bar"><div style={{ width: `${(t.sent / t.total) * 100}%` }} /></div>
          ) : null}
          <div className="mutedtext" style={{ fontSize: 11 }}>{t.error || t.progress}</div>
        </div>
      ))}
      {shown.some((t) => t.state === 'running') && (
        <div className="mutedtext" style={{ fontSize: 11 }}>
          다른 메뉴로 옮겨도 이어집니다. 이 창을 닫거나 새로 고치면 멈춥니다.
        </div>
      )}
    </div>
  );
}
