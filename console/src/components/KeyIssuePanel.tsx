import { useState } from 'react';
import { api } from '../lib/api';
import type { ApiKeyIssued } from '../lib/types';

/** API 키 발급(관리자) — 사람 계정이 아니라 스크립트·CI 같은 도구가 쓰는 키. 원문은 발급 직후 한 번만 보인다. */
export default function KeyIssuePanel() {
  const [name, setName] = useState('');
  const [admin, setAdmin] = useState(false);
  const [issued, setIssued] = useState<ApiKeyIssued | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    setIssued(null);
    try {
      setIssued(await api.issueKey(name.trim(), admin));
      setName('');
      setAdmin(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2>API 키 발급</h2>
      <form className="row" onSubmit={submit}>
        <input
          placeholder="키 이름 (예: ci-bot)"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          style={{ width: 220 }}
        />
        <label className="row" style={{ gap: 6 }}>
          <input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} />
          admin 권한
        </label>
        <button type="submit" disabled={busy}>
          {busy ? '발급 중...' : '발급'}
        </button>
      </form>
      {issued && (
        <p style={{ color: 'var(--yellow)', fontSize: 13 }}>
          지금만 표시됩니다 — 안전한 곳에 보관하세요:{' '}
          <span className="mono" style={{ userSelect: 'all' }}>{issued.key}</span>
        </p>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
