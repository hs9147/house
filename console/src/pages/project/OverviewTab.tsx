import { useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import Async from '../../components/Async';
import { Confirm } from '../../components/Modal';
import StatusPill from '../../components/StatusPill';
import { api, ApiError } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import type { BuildProfile } from '../../lib/types';
import type { ProjectContext } from '../ProjectDetail';

type Action = { kind: 'deploy' | 'rollback' | 'stop'; profile: BuildProfile } | null;

export default function OverviewTab() {
  const { project } = useOutletContext<ProjectContext>();
  const state = useApi(() => api.projectStatus(project.id), [project.id]);
  const [gitSha, setGitSha] = useState('');
  const [action, setAction] = useState<Action>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const run = async () => {
    if (!action) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      if (action.kind === 'deploy') {
        setMessage(`${action.profile} 배포 중... (빌드에 수 분 걸릴 수 있습니다)`);
        const d = await api.deploy(project.id, action.profile, gitSha.trim() || undefined);
        setMessage(`배포 완료 — ${d.image_tag} (${d.status})`);
      } else if (action.kind === 'rollback') {
        const d = await api.rollback(project.id, action.profile);
        setMessage(`롤백 완료 — ${d.image_tag}`);
      } else {
        await api.stop(project.id, action.profile);
        setMessage(`${action.profile} 중지됨`);
      }
      state.reload();
    } catch (e) {
      const err = e as ApiError;
      setMessage('');
      setError(err.status === 409 ? '이미 배포가 진행 중입니다. 잠시 후 다시 시도하세요.' : err.message);
    } finally {
      setBusy(false);
      setAction(null);
    }
  };

  const labels: Record<string, string> = {
    deploy: '배포', rollback: '롤백', stop: '중지',
  };

  return (
    <>
      <div className="panel">
        <h2>실행 상태</h2>
        <Async state={state}>
          {(status) => (
            <table>
              <thead>
                <tr>
                  <th>프로필</th>
                  <th>상태</th>
                  <th>도메인</th>
                  <th style={{ width: 260 }}>동작</th>
                </tr>
              </thead>
              <tbody>
                {(['release', 'development'] as BuildProfile[]).map((profile) => (
                  <tr key={profile}>
                    <td><StatusPill value={profile} /></td>
                    <td><StatusPill value={status[profile] ?? 'unknown'} /></td>
                    <td className="mono">
                      {profile === 'release'
                        ? project.domain || `${project.name}.{기본도메인}`
                        : `${project.name}-dev.{기본도메인}`}
                    </td>
                    <td>
                      <div className="row">
                        <button
                          className="small"
                          disabled={busy}
                          onClick={() => setAction({ kind: 'deploy', profile })}
                        >
                          배포
                        </button>
                        <button
                          className="small secondary"
                          disabled={busy}
                          onClick={() => setAction({ kind: 'rollback', profile })}
                        >
                          롤백
                        </button>
                        <button
                          className="small danger"
                          disabled={busy}
                          onClick={() => setAction({ kind: 'stop', profile })}
                        >
                          중지
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Async>
        <div className="row" style={{ marginTop: 12 }}>
          <label className="field">
            특정 커밋 배포 (선택, git SHA)
            <input
              className="mono"
              value={gitSha}
              onChange={(e) => setGitSha(e.target.value)}
              placeholder="비우면 브랜치 최신"
              style={{ width: 320 }}
            />
          </label>
        </div>
        {message && <p style={{ color: 'var(--green)' }}>{message}</p>}
        {error && <p className="error">{error}</p>}
      </div>
      {action && (
        <Confirm
          title={`${action.profile} ${labels[action.kind]}`}
          message={`${project.name}에 ${action.profile} 프로필로 "${labels[action.kind]}"을(를) 실행합니다.`}
          confirmLabel={labels[action.kind]}
          danger={action.kind === 'stop'}
          busy={busy}
          onConfirm={run}
          onClose={() => !busy && setAction(null)}
        />
      )}
    </>
  );
}
