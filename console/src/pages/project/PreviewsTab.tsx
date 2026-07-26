import { useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import Async from '../../components/Async';
import StatusPill from '../../components/StatusPill';
import { api } from '../../lib/api';
import { fmtDate } from '../../lib/format';
import { useApi } from '../../lib/hooks';
import type { ProjectContext } from '../ProjectDetail';

export default function PreviewsTab() {
  const { project } = useOutletContext<ProjectContext>();
  const state = useApi(() => api.listPreviews(project.id), [project.id]);
  const [branch, setBranch] = useState('');
  const [ttl, setTtl] = useState(60);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    setMessage('프리뷰 빌드 중... (수 분 걸릴 수 있습니다)');
    try {
      const p = await api.createPreview(project.id, branch.trim() || undefined, ttl);
      setMessage(`프리뷰 생성됨: ${p.url}`);
      state.reload();
    } catch (err) {
      setMessage('');
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: number) => {
    try {
      await api.deletePreview(id);
      state.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div className="panel">
      <h2>프리뷰</h2>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        지정 브랜치를 development 프로필로 빌드해 임시 도메인에 띄웁니다. TTL 만료 시 자동
        회수되며, 동시 5개까지 가능합니다.
      </p>
      <form className="row" onSubmit={create} style={{ marginBottom: 14 }}>
        <input
          className="mono"
          placeholder={`브랜치 (비우면 ${project.branch})`}
          value={branch}
          onChange={(e) => setBranch(e.target.value)}
          style={{ width: 260 }}
        />
        <select value={ttl} onChange={(e) => setTtl(Number(e.target.value))}>
          <option value={30}>TTL 30분</option>
          <option value={60}>TTL 60분</option>
          <option value={120}>TTL 2시간</option>
          <option value={480}>TTL 8시간</option>
        </select>
        <button type="submit" disabled={busy}>
          {busy ? '생성 중...' : '프리뷰 생성'}
        </button>
      </form>
      {message && <p style={{ color: 'var(--green)' }}>{message}</p>}
      {error && <p className="error">{error}</p>}
      <Async state={state} empty="프리뷰가 없습니다.">
        {(rows) => (
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>브랜치</th>
                <th>URL</th>
                <th>상태</th>
                <th>만료</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.id}>
                  <td className="mono">{p.id}</td>
                  <td className="mono">{p.branch}</td>
                  <td className="mono">
                    {p.url ? (
                      <a href={p.url} target="_blank" rel="noreferrer">
                        {p.url}
                      </a>
                    ) : (
                      '-'
                    )}
                  </td>
                  <td><StatusPill value={p.status} /></td>
                  <td className="mono">{fmtDate(p.expires_at)}</td>
                  <td>
                    {p.status === 'running' && (
                      <button className="danger small" onClick={() => remove(p.id)}>
                        삭제
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
    </div>
  );
}
