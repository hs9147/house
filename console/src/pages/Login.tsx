import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { login } from '../lib/auth';

export default function Login() {
  const [key, setKey] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const { admin } = await login(key.trim());
      navigate(admin ? '/' : '/projects');
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <form className="panel login-box" onSubmit={submit}>
        <h2>PaaS 콘솔 로그인</h2>
        <label className="field">
          API 키
          <input
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="paas_..."
            autoFocus
          />
        </label>
        {error && <p className="error">{error}</p>}
        <div className="row" style={{ marginTop: 14 }}>
          <button type="submit" disabled={busy || !key.trim()}>
            {busy ? '확인 중...' : '로그인'}
          </button>
        </div>
      </form>
    </div>
  );
}
