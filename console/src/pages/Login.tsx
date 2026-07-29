import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { loginWithAccount } from '../lib/auth';

export default function Login() {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [key, setKey] = useState('');
  const [allowedDomain, setAllowedDomain] = useState<string>('');
  const [platformName, setPlatformName] = useState<string>('PaaS');
  const [useApiKeyOnly, setUseApiKeyOnly] = useState(false);
  const [error, setError] = useState('');
  const [successMsg, setSuccessMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    fetch('/paas/health')
      .then((res) => res.json())
      .then((data) => {
        if (data.allowed_email_domain) {
          setAllowedDomain(data.allowed_email_domain);
        }
        if (data.platform_name) {
          setPlatformName(data.platform_name);
        }
      })
      .catch(() => {});
  }, []);

  const validateDomain = (userEmail: string) => {
    if (!allowedDomain) return true;
    const cleanAllowed = allowedDomain.replace(/^@/, '').toLowerCase();
    const userDomain = userEmail.split('@').pop()?.toLowerCase() ?? '';
    return userEmail.includes('@') && (userDomain === cleanAllowed || userDomain.endsWith('.' + cleanAllowed));
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    setSuccessMsg('');

    const cleanEmail = email.trim();
    const cleanKey = key.trim();

    if (mode === 'register') {
      // 📝 신규 계정 등록 (회원가입)
      if (!validateDomain(cleanEmail)) {
        const cleanAllowed = allowedDomain.replace(/^@/, '');
        setError(`@${cleanAllowed} 이메일 계정만 등록 가능합니다.`);
        setBusy(false);
        return;
      }
      try {
        const regRes = await api.registerUser({
          email: cleanEmail,
          name: name.trim(),
          password: password.trim(),
        });
        // 회원가입 성공 시 자동 로그인
        const { admin } = await loginWithAccount(regRes.email, password.trim());
        navigate(admin ? '/' : '/projects');
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setBusy(false);
      }
      return;
    }

    // 🔑 기존 로그인
    if (!useApiKeyOnly && cleanEmail && allowedDomain) {
      if (!validateDomain(cleanEmail)) {
        const cleanAllowed = allowedDomain.replace(/^@/, '');
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
      <form className="panel login-box" onSubmit={submit} style={{ width: 380 }}>
        {/* 탭 전환 */}
        <div style={{ display: 'flex', borderBottom: '1px solid rgba(255,255,255,0.1)', marginBottom: 16 }}>
          <button
            type="button"
            className={`tab ${mode === 'login' ? 'active' : ''}`}
            onClick={() => {
              setMode('login');
              setError('');
              setSuccessMsg('');
            }}
            style={{ flex: 1, padding: '8px 0', background: 'none', border: 'none', color: mode === 'login' ? '#3b82f6' : '#888', borderBottom: mode === 'login' ? '2px solid #3b82f6' : '2px solid transparent', cursor: 'pointer', fontWeight: mode === 'login' ? 'bold' : 'normal' }}
          >
            로그인
          </button>
          <button
            type="button"
            className={`tab ${mode === 'register' ? 'active' : ''}`}
            onClick={() => {
              setMode('register');
              setError('');
              setSuccessMsg('');
            }}
            style={{ flex: 1, padding: '8px 0', background: 'none', border: 'none', color: mode === 'register' ? '#3b82f6' : '#888', borderBottom: mode === 'register' ? '2px solid #3b82f6' : '2px solid transparent', cursor: 'pointer', fontWeight: mode === 'register' ? 'bold' : 'normal' }}
          >
            계정 등록
          </button>
        </div>

        <h2 style={{ marginBottom: 4, fontSize: 20 }}>
          {mode === 'register' ? `${platformName} 신규 계정 등록` : `${platformName} 계정 로그인`}
        </h2>
        <p className="dim" style={{ fontSize: 13, marginBottom: 16 }}>
          {mode === 'register'
            ? `${domainHint} 계정 정보로 신규 등록합니다.`
            : useApiKeyOnly
              ? '관리자 API 키로 로그인합니다.'
              : `${domainHint} 사내 계정으로 로그인합니다.`}
        </p>

        {mode === 'register' ? (
          <>
            <label className="field" style={{ marginBottom: 12 }}>
              이름 / 닉네임
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="홍길동"
                required
                autoFocus
              />
            </label>
            <label className="field" style={{ marginBottom: 12 }}>
              계정 이메일
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder={allowedDomain ? `user@${allowedDomain.replace(/^@/, '')}` : 'user@company.com'}
                required
              />
            </label>
            <label className="field" style={{ marginBottom: 12 }}>
              비밀번호
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="4자 이상 입력"
                required
              />
            </label>
          </>
        ) : (
          <>
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
              {useApiKeyOnly ? 'API 키' : '비밀번호'}
              <input
                type="password"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder={useApiKeyOnly ? 'paas_...' : '비밀번호 입력'}
                required
                autoFocus={useApiKeyOnly}
              />
            </label>
          </>
        )}

        {error && <p className="error" style={{ marginTop: 10, fontSize: 13 }}>{error}</p>}
        {successMsg && <p className="success" style={{ marginTop: 10, fontSize: 13, color: '#10b981' }}>{successMsg}</p>}

        <div className="row" style={{ marginTop: 18, alignItems: 'center', justifyContent: 'space-between' }}>
          {mode === 'login' ? (
            <button
              type="button"
              className="secondary text small"
              onClick={() => {
                setUseApiKeyOnly(!useApiKeyOnly);
                setError('');
              }}
            >
              {useApiKeyOnly ? '← 계정 로그인' : '관리자 키 로그인'}
            </button>
          ) : (
            <span />
          )}
          <button
            type="submit"
            disabled={
              busy ||
              (mode === 'register' && (!email.trim() || !name.trim() || !password.trim())) ||
              (mode === 'login' && !useApiKeyOnly && !email.trim()) ||
              (mode === 'login' && !key.trim())
            }
          >
            {busy ? '처리 중...' : mode === 'register' ? '계정 등록 완료' : '로그인'}
          </button>
        </div>
      </form>
    </div>
  );
}
