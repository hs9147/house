import { useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import Async from '../../components/Async';
import StatusPill from '../../components/StatusPill';
import { api } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import type { ProjectContext } from '../ProjectDetail';

export default function ModulesTab() {
  const { project } = useOutletContext<ProjectContext>();
  const bound = useApi(() => api.projectModules(project.id), [project.id]);
  const registry = useApi(() => api.listModules());
  const [moduleId, setModuleId] = useState('');
  const [prefix, setPrefix] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [injected, setInjected] = useState<string[]>([]);

  const bind = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const res = await api.bindModule(project.id, Number(moduleId), prefix.trim());
      setInjected(res.injected_env);
      setPrefix('');
      bound.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2>바인딩된 모듈</h2>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        바인딩하면 다음 배포부터 규약된 환경변수가 자동 주입됩니다. 이 목록은 LLM 채팅
        컨텍스트에도 제공됩니다.
      </p>
      <Async state={bound} empty="바인딩된 모듈이 없습니다.">
        {(rows) => (
          <table style={{ marginBottom: 16 }}>
            <thead>
              <tr>
                <th>모듈</th>
                <th>타입</th>
                <th>주입 환경변수</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.name}>
                  <td>{m.name}</td>
                  <td><StatusPill value={m.type} /></td>
                  <td className="mono">{m.env.join(', ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
      <form className="row" onSubmit={bind}>
        <select value={moduleId} onChange={(e) => setModuleId(e.target.value)} required>
          <option value="">모듈 선택...</option>
          {(registry.data ?? []).map((m) => (
            <option key={m.id} value={m.id}>
              {m.name} ({m.type})
            </option>
          ))}
        </select>
        <input
          className="mono"
          placeholder="ENV_PREFIX (예: PAY)"
          value={prefix}
          onChange={(e) => setPrefix(e.target.value.toUpperCase())}
          pattern="[A-Z][A-Z0-9_]{0,24}"
          required
          style={{ width: 200 }}
        />
        <button type="submit" disabled={busy || !moduleId}>
          {busy ? '바인딩 중...' : '바인딩'}
        </button>
      </form>
      {injected.length > 0 && (
        <p style={{ color: 'var(--green)', fontSize: 13 }}>
          주입 예정: <span className="mono">{injected.join(', ')}</span>
        </p>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
