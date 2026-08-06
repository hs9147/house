import { useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import Async from '../../components/Async';
import StatusPill from '../../components/StatusPill';
import { api } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import type { ModuleSummary } from '../../lib/types';
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

  const unbind = async (m: ModuleSummary) => {
    if (!window.confirm(`'${m.agent_name}' 바인딩(${m.env_prefix})을 해제하시겠습니까?\n다음 배포부터 이 환경변수가 주입되지 않습니다.`)) return;
    setError('');
    try {
      await api.unbindModule(project.id, m.binding_id);
      bound.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div className="panel">
      <h2>바인딩된 모듈</h2>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        바인딩하면 다음 배포부터 규약된 환경변수가 자동 주입됩니다. 이 목록은 에이전트 기획의
        가용 모듈 제약(외부 빌드 guardrail)에도 그대로 쓰입니다.
      </p>
      <Async state={bound} empty="바인딩된 모듈이 없습니다.">
        {(rows) => (
          <table style={{ marginBottom: 16 }}>
            <thead>
              <tr>
                <th>모듈</th>
                <th>타입</th>
                <th>env 접두사</th>
                <th>능력</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.binding_id}>
                  <td>{m.agent_name}</td>
                  <td><StatusPill value={m.type} /></td>
                  <td className="mono">{m.env_prefix}</td>
                  <td className="mono">{m.skills.join(', ')}</td>
                  <td>
                    <button className="small danger" onClick={() => unbind(m)}>해제</button>
                  </td>
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
