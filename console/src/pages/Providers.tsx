import { useState } from 'react';
import Async from '../components/Async';
import StatusPill from '../components/StatusPill';
import { api } from '../lib/api';
import { isAdmin } from '../lib/auth';
import { useApi } from '../lib/hooks';

export default function Providers() {
  const state = useApi(() => api.listProviders());
  const admin = isAdmin();
  const [form, setForm] = useState({
    name: '', kind: 'external', base_url: '', api_key: '', model: '',
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await api.createProvider({ ...form, api_key: form.api_key || undefined });
      setForm({ name: '', kind: 'external', base_url: '', api_key: '', model: '' });
      state.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="panel">
        <h2>LLM 프로바이더</h2>
        <p className="mutedtext" style={{ fontSize: 12 }}>
          내부(internal)는 base_url에 <span className="mono">project://llm-프로젝트명</span>을
          쓰면 배포 도메인으로 자동 해석됩니다 — 소스가 사내망을 벗어나지 않는 모드.
        </p>
        <Async state={state} empty="등록된 프로바이더가 없습니다.">
          {(rows) => (
            <table>
              <thead>
                <tr>
                  <th>이름</th>
                  <th>구분</th>
                  <th>Endpoint</th>
                  <th>모델</th>
                  <th>API 키</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.id}>
                    <td>{p.name}</td>
                    <td>
                      <StatusPill value={p.kind === 'internal' ? 'release' : 'proposed'} />{' '}
                      {p.kind === 'internal' ? '내부' : '외부'}
                    </td>
                    <td className="mono">{p.base_url}</td>
                    <td className="mono">{p.model}</td>
                    <td>{p.has_api_key ? '설정됨' : '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Async>
      </div>
      {admin && (
        <div className="panel">
          <h2>프로바이더 등록 (admin)</h2>
          <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div className="row">
              <label className="field" style={{ flex: 1 }}>
                이름
                <input value={form.name} onChange={(e) => set('name', e.target.value)} required />
              </label>
              <label className="field">
                구분
                <select value={form.kind} onChange={(e) => set('kind', e.target.value)}>
                  <option value="external">external — 외부 API</option>
                  <option value="internal">internal — 사내 배포 LLM</option>
                </select>
              </label>
            </div>
            <label className="field">
              Endpoint
              <input
                className="mono"
                value={form.base_url}
                onChange={(e) => set('base_url', e.target.value)}
                placeholder={
                  form.kind === 'internal' ? 'project://llm-main' : 'https://api.anthropic.com'
                }
                required
              />
            </label>
            <div className="row">
              <label className="field" style={{ flex: 1 }}>
                모델
                <input
                  className="mono"
                  value={form.model}
                  onChange={(e) => set('model', e.target.value)}
                  required
                />
              </label>
              <label className="field" style={{ flex: 1 }}>
                API 키 (선택 — 암호화 저장)
                <input
                  type="password"
                  value={form.api_key}
                  onChange={(e) => set('api_key', e.target.value)}
                />
              </label>
            </div>
            {error && <p className="error">{error}</p>}
            <div className="row">
              <button type="submit" disabled={busy}>
                {busy ? '등록 중...' : '등록'}
              </button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}
