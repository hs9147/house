import { useEffect, useRef, useState } from 'react';
import { api, hostSsoRetries, SSO_LOGIN_EVENT, SSO_RETRY_EVENT, type SsoLogin, type SsoRetry } from '../lib/api';
import Modal from './Modal';

// device code는 10분이면 만료된다 — 그 뒤로는 물어봐도 끝나지 않는다.
const POLL_MS = 5000;
const POLL_LIMIT_MS = 10 * 60 * 1000;

/**
 * SSO 토큰이 만료돼 LLM 호출이 실패하면 서버가 이미 로그인을 시작해 두었다(app/api/llm.py
 * provider_error). 남은 일은 사람이 '허용'을 누르는 것뿐이라 그 주소를 **이 브라우저에서**
 * 바로 열고, 승인이 끝났는지도 여기서 확인한다 — 관리자 화면으로 갈 필요가 없다.
 *
 * 만료로 실패한 요청(실패를 삼킨 응답 포함)은 붙잡아 두었다가(lib/api holdForRetry), 승인이 확인되면 팝업으로
 * 「다시 실행」을 묻는다 — 다시 실행한 결과는 원래 화면의 호출로 돌아가 그 화면이 스스로 다시 그려진다.
 * 닫으면 원래의 실패(또는 원래 응답)로 끝난다.
 */
export default function SsoLoginNotice() {
  const [login, setLogin] = useState<SsoLogin | null>(null);
  const [done, setDone] = useState(false);
  const [retries, setRetries] = useState<SsoRetry[]>([]);

  const opened = useRef('');

  // 이 화면이 떠 있는 동안만 실패한 요청을 붙잡는다(없으면 원래 호출이 끝나지 않는다)
  useEffect(() => {
    const unhost = hostSsoRetries();
    const onRetry = (event: Event) => {
      const entry = (event as CustomEvent<SsoRetry>).detail;
      setRetries((cur) => [...cur, entry]);
    };
    window.addEventListener(SSO_RETRY_EVENT, onRetry);
    return () => {
      window.removeEventListener(SSO_RETRY_EVENT, onRetry);
      unhost();
    };
  }, []);

  // 안내 없이 붙잡힌 요청(로그인 정보를 읽지 못한 머리글 등)은 기다리게 두지 않고 원래대로 끝낸다
  useEffect(() => {
    if (login || retries.length === 0) return;
    const cur = retries;
    setRetries([]);
    cur.forEach((r) => r.dismiss());
  }, [login, retries]);

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

  // 붙잡은 요청을 한 번씩만 끝낸다 — 상태 갱신 함수 안에서 부르면 개발 모드에서 두 번 불릴 수 있다
  const settleAll = (run: boolean) => {
    const cur = retries;
    setRetries([]);
    cur.forEach((r) => (run ? r.retry() : r.dismiss()));
  };
  if (!login) return null;
  const close = () => {
    // 승인하지 않고 닫았으면 다음 실패 때 다시 열어야 한다. 붙잡아 둔 요청은 원래대로 끝낸다.
    opened.current = '';
    settleAll(false);
    setLogin(null);
    setDone(false);
  };
  if (done) {
    const finish = (run: boolean) => {
      settleAll(run);
      opened.current = '';
      setLogin(null);
      setDone(false);
    };
    if (retries.length) {
      return (
        <Modal title="AWS SSO 로그인이 끝났습니다" onClose={() => finish(false)}>
          <p style={{ marginTop: 0 }}>로그인이 만료돼 실패한 작업 {retries.length}건을 다시 실행할까요?</p>
          <ul className="mono" style={{ fontSize: 12, paddingLeft: 18 }}>
            {retries.map((r) => <li key={r.id}>{r.label}</li>)}
          </ul>
          <p className="mutedtext" style={{ fontSize: 12 }}>
            다시 실행하면 같은 요청을 다시 보내고, 결과는 원래 화면에 그대로 나옵니다.
          </p>
          <div className="row" style={{ gap: 8, justifyContent: 'flex-end' }}>
            <button className="secondary" onClick={() => finish(false)}>닫기</button>
            <button onClick={() => finish(true)}>다시 실행</button>
          </div>
        </Modal>
      );
    }
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
        {retries.length > 0 && <> 확인되면 실패한 작업 {retries.length}건을 다시 실행할지 묻습니다.</>}
      </div>
      <a href={login.verification_url} target="_blank" rel="noopener noreferrer">
        탭이 열리지 않았으면 여기를 누르세요
      </a>{' '}
      <button className="secondary small" onClick={close}>닫기</button>
    </div>
  );
}
