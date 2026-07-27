import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { loginWithAccount } from '../lib/auth';

export default function Login() {
  const [email, setEmail] = useState('');
  const [key, setKey] = useState('');
  const [allowedDomain, setAllowedDomain] = useState<string>('');
  const [useApiKeyOnly, setUseApiKeyOnly] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    fetch('/paas/health')
      .then((res) => res.json())
      .then((data) => {
        if (data.allowed_email_domain) {
          setAllowedDomain(data.allowed_email_domain);
        }
      })
      .catch(() => {});
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');

    const cleanEmail = email.trim();
    const cleanKey = key.trim();

    if (!useApiKeyOnly && cleanEmail && allowedDomain) {
      const cleanAllowed = allowedDomain.replace(/^@/, '').toLowerCase();
      const userDomain = cleanEmail.split('@').pop()?.toLowerCase() ?? '';
      if (!cleanEmail.includes('@') || (userDomain !== cleanAllowed && !userDomain.endsWith('.' + cleanAllowed))) {
        setError(`@${cleanAllowed} 이메일 계정만 로그인 가능합니다.`);
        setBusy(false);
        return;
      }
    }

    try {
      const { admin } = await loginWithAccount(cleanEmail, cleanKey);
      navigate(admin ? '/' : '/projects');
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const domainHint = allowedDomain ? `@${allowedDomain.replace(/^@/, '')}` : '사내';

  return (
    <div className="login-wrap">
      <form className="panel login-box" onSubmit={submit} style={{ width: 360 }}>
        <h2 style={{ marginBottom: 4 }}>PaaS 계정 로그인</h2>
        <p className="dim" style={{ fontSize: 13, marginBottom: 16 }}>
          {useApiKeyOnly ? '관리자 API 키로 로그인합니다.' : `${domainHint} 사내 계정으로 로그인합니다.`}
        </p>

        {!useApiKeyOnly && (
          <label className="field" style={{ marginBottom: 12 }}>
            계정 이메일
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder={allowedDomain ? `user@${allowedDomain.replace(/^@/, '')}` : 'user@company.com'}
              required={!useApiKeyOnly}
              autoFocus
            />
          </label>
        )}

        <label className="field">
          {useApiKeyOnly ? 'API 키' : '인증 키 / 비밀번호'}
          <input
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={useApiKeyOnly ? 'paas_...' : '인증 키 또는 비밀번호 입력'}
            required
            autoFocus={useApiKeyOnly}
          />
        </label>

        {error && <p className="error" style={{ marginTop: 10 }}>{error}</p>}

        <div className="row" style={{ marginTop: 16, alignItems: 'center', justifyContent: 'space-between' }}>
          <button
            type="button"
            className="secondary text small"
            onClick={() => {
              setUseApiKeyOnly(!useApiKeyOnly);
              setError('');
            }}
          >
            {useApiKeyOnly ? '← 계정 로그인으로 돌아가기' : '관리자 API 키로 로그인'}
          </button>
          <button type="submit" disabled={busy || (!useApiKeyOnly && !email.trim()) || !key.trim()}>
            {busy ? '확인 중...' : '로그인'}
          </button>
        </div>
      </form>
    </div>
  );
}
