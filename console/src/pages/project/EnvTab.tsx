import { useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import Async from '../../components/Async';
import { api } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import type { ProjectContext } from '../ProjectDetail';

export default function EnvTab() {
  const { project } = useOutletContext<ProjectContext>();
  const state = useApi(() => api.listEnv(project.id), [project.id]);
  const [key, setKey] = useState('');
  const [value, setValue] = useState('');
  const [isSecret, setIsSecret] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await api.setEnv(project.id, key.trim(), value, isSecret);
      setKey('');
      setValue('');
      state.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2>환경변수</h2>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        값은 암호화 저장되며 다시 열람할 수 없습니다(write-only). 같은 키로 저장하면 덮어씁니다.
        다음 배포부터 적용됩니다.
      </p>
      <Async state={state} empty="등록된 환경변수가 없습니다.">
        {(rows) => (
          <table style={{ marginBottom: 16 }}>
            <thead>
              <tr>
                <th>키</th>
                <th>값</th>
                <th>시크릿</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.key}>
                  <td className="mono">{r.key}</td>
                  <td className="mono">{r.value}</td>
                  <td>{r.is_secret ? '예' : '아니오'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
      <form className="row" onSubmit={submit}>
        <input
          className="mono"
          placeholder="KEY"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          required
          pattern="[A-Za-z_][A-Za-z0-9_]*"
          style={{ width: 180 }}
        />
        <input
          className="mono"
          placeholder="값"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          required
          style={{ flex: 1 }}
        />
        <label className="row" style={{ gap: 6 }}>
          <input
            type="checkbox"
            checked={isSecret}
            onChange={(e) => setIsSecret(e.target.checked)}
          />
          시크릿
        </label>
        <button type="submit" disabled={busy}>
          {busy ? '저장 중...' : '저장'}
        </button>
      </form>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
