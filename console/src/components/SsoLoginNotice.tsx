import { useEffect, useRef, useState } from 'react';
import { api, SSO_LOGIN_EVENT, type SsoLogin } from '../lib/api';

// device code는 10분이면 만료된다 — 그 뒤로는 물어봐도 끝나지 않는다.
const POLL_MS = 5000;
const POLL_LIMIT_MS = 10 * 60 * 1000;

/**
 * SSO 토큰이 만료돼 LLM 호출이 실패하면 서버가 이미 로그인을 시작해 두었다(app/api/llm.py
 * provider_error). 남은 일은 사람이 '허용'을 누르는 것뿐이라 그 주소를 **이 브라우저에서**
 * 바로 열고, 승인이 끝났는지도 여기서 확인한다 — 관리자 화면으로 갈 필요가 없다.
 */
export default function SsoLoginNotice() {
  const [login, setLogin] = useState<SsoLogin | null>(null);
  const [done, setDone] = useState(false);

  const opened = useRef('');

  useEffect(() => {
    const onLogin = (event: Event) => {
      const next = (event as CustomEvent<SsoLogin>).detail;
      // 같은 만료로 여러 요청이 동시에 실패한다 — 서버가 같은 주소를 주므로 창은 한 번만 연다.
      if (opened.current === next.verification_url) return;
      opened.current = next.verification_url;
      window.open(next.verification_url, '_blank', 'noopener');
      setLogin(next);
      setDone(false);
    };
    window.addEventListener(SSO_LOGIN_EVENT, onLogin);
    return () => window.removeEventListener(SSO_LOGIN_EVENT, onLogin);
  }, []);

  useEffect(() => {
    if (!login || done) return undefined;
    const started = Date.now();
    const timer = window.setInterval(async () => {
      if (Date.now() - started > POLL_LIMIT_MS) {
        window.clearInterval(timer);
        return;
      }
      try {
        if ((await api.awsLoginStatus(login.profile)).ok) {
          window.clearInterval(timer);
          setDone(true);
        }
      } catch {
        /* 다음 차례에 다시 묻는다 */
      }
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [login, done]);

  if (!login) return null;
  const close = () => {
    // 승인하지 않고 닫았으면 다음 실패 때 다시 열어야 한다.
    opened.current = '';
    setLogin(null);
    setDone(false);
  };
  if (done) {
    return (
      <div className="panel" style={{ borderColor: '#22c55e', marginBottom: 12 }}>
        <strong style={{ color: '#22c55e' }}>AWS SSO 로그인이 끝났습니다</strong>{' '}
        <span className="mutedtext">— 방금 실패한 작업을 다시 시도하세요.</span>{' '}
        <button className="secondary small" onClick={close}>닫기</button>
      </div>
    );
  }
  return (
    <div className="panel" style={{ borderColor: '#f59e0b', marginBottom: 12 }}>
      <strong style={{ color: '#f59e0b' }}>
        AWS SSO 토큰이 만료돼 서버가 다시 로그인을 시작했습니다 — 새 탭에서 '허용'을 눌러 주세요
      </strong>
      <div className="mutedtext" style={{ fontSize: 12, margin: '6px 0' }}>
        프로필 <span className="mono">{login.profile}</span>
        {!login.code_autofilled && login.user_code && (
          <> · 코드 <span className="mono">{login.user_code}</span></>
        )}
        {' '}· 승인하면 이 화면이 알아서 확인합니다.
      </div>
      <a href={login.verification_url} target="_blank" rel="noopener noreferrer">
        탭이 열리지 않았으면 여기를 누르세요
      </a>{' '}
      <button className="secondary small" onClick={close}>닫기</button>
    </div>
  );
}
