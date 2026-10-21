import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';
import { isLoggedIn } from '../lib/auth';
import { MAIL_CHANNEL, OUTLOOK_ORIGINS, webMailOf } from '../lib/webmail';

// 로그인한 콘솔 탭이 답할 때까지 기다리는 시간
const ACK_MS = 6000;

/**
 * 메일 받는 창 — 웹 Outlook의 「GPAX 메일」 북마크(lib/mailAgent.js)가 팝업으로 연다. 로그인 없이 열린다:
 * 다른 사이트가 연 창이라 콘솔의 로그인(sessionStorage)이 따라오지 않는다. 그래서 받은 메일을 같은
 * 브라우저의 로그인한 콘솔 탭으로 넘기고(BroadcastChannel), 그 탭이 저장한다(components/MailRelay).
 * 메일은 보낸 쪽이 웹 Outlook 주소이고 이 창을 연 그 창일 때만 받는다.
 */
export default function MailReceive() {
  const [state, setState] = useState<{ kind: 'wait' | 'sending' | 'ok' | 'fail'; text: string }>({
    kind: 'wait', text: '웹 Outlook에서 메일을 받는 중…',
  });
  const pending = useRef('');

  useEffect(() => {
    const opener = window.opener as Window | null;
    if (!opener) {
      setState({ kind: 'fail', text: '웹 Outlook의 「GPAX 메일」 북마크가 여는 창입니다 — 웹 Outlook에서 메일을 연 채 북마크를 누르세요.' });
      return;
    }
    const channel = new BroadcastChannel(MAIL_CHANNEL);
    let timer = 0;
    const done = (ok: boolean, text: string) => {
      window.clearTimeout(timer);
      pending.current = '';
      setState({ kind: ok ? 'ok' : 'fail', text });
      if (ok) window.setTimeout(() => window.close(), 2500);
    };
    channel.onmessage = (e: MessageEvent) => {
      const m = e.data;
      if (m?.gpax === 'mail-ack' && m.id && m.id === pending.current) done(!!m.ok, String(m.text || ''));
    };
    const onMessage = (e: MessageEvent) => {
      if (!OUTLOOK_ORIGINS.includes(e.origin) || e.source !== opener || e.data?.gpax !== 'mail') return;
      const mail = webMailOf(e.data.mail);
      if (!mail) {
        done(false, '읽은 메일이 비어 있습니다 — 메일을 읽기 창에 연 채 다시 누르세요.');
        return;
      }
      setState({ kind: 'sending', text: `「${mail.subject || '(제목 없음)'}」을 콘솔로 넘기는 중…` });
      if (isLoggedIn()) {
        api.saveWebMail([mail]).then(
          (out) => done(true, out.new ? '개인 업무 맥락에 넣었습니다.' : '이미 넣은 메일입니다.'),
          (err: Error) => done(false, err.message));
        return;
      }
      pending.current = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      channel.postMessage({ gpax: 'mail', id: pending.current, mail });
      timer = window.setTimeout(() => done(false,
        '로그인한 콘솔 탭이 없습니다 — 이 브라우저의 다른 탭에서 GPAX 콘솔에 로그인해 두고 북마크를 다시 누르세요.'), ACK_MS);
    };
    window.addEventListener('message', onMessage);
    // 북마크는 이 인사를 받아야 메일을 보낸다 — 웹 Outlook 주소마다(다른 출처로는 가지 않는다).
    OUTLOOK_ORIGINS.forEach((origin) => {
      try {
        opener.postMessage({ gpax: 'ready' }, origin);
      } catch {
        /* 닫힌 창 */
      }
    });
    return () => {
      window.removeEventListener('message', onMessage);
      window.clearTimeout(timer);
      channel.close();
    };
  }, []);

  return (
    <div style={{ padding: 20, maxWidth: 480 }}>
      <h3 style={{ marginTop: 0 }}>GPAX 메일</h3>
      <p className={state.kind === 'fail' ? 'error' : state.kind === 'ok' ? '' : 'mutedtext'}>
        {state.kind === 'ok' && <span className="status ok" style={{ marginRight: 6 }}>완료</span>}
        {state.text}
      </p>
      {state.kind === 'ok' && <p className="mutedtext" style={{ fontSize: 12 }}>이 창은 곧 닫힙니다.</p>}
    </div>
  );
}
