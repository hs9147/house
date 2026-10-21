import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { MAIL_CHANNEL, offerMailChat, webMailOf } from '../lib/webmail';

/**
 * 로그인한 콘솔 탭에서 — 메일 받는 창(pages/MailReceive)과 같은 브라우저 안에서 주고받는다.
 *
 *   mail        → 이 사람의 개인 업무 맥락에 넣고 답한다. 탭이 여럿이면 모두 넣지만 서버가 같은 메일을
 *                 다시 쓰지 않는다(먼저 온 답을 받는 창이 쓴다).
 *   sessions    → 고를 수 있는 스마트워크 업무 목록
 *   chat-offer  → 대화를 맡겠다고 손든다(스마트워크를 연 탭인지 함께). 받는 창이 **한 탭만** 고른다 —
 *                 여러 탭이 같은 말을 보내면 대화가 겹친다.
 *   chat        → 고른 탭만 스마트워크로 가서 그 업무를 열고 메일을 붙여 보낸다(pages/Smartwork).
 */
export default function MailRelay() {
  const navigate = useNavigate();
  useEffect(() => {
    const me = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const channel = new BroadcastChannel(MAIL_CHANNEL);
    channel.onmessage = async (e: MessageEvent) => {
      const m = e.data;
      if (!m?.id || typeof m.gpax !== 'string') return;
      if (m.gpax === 'mail') {
        const mail = webMailOf(m.mail);
        if (!mail) return;
        try {
          const out = await api.saveWebMail([mail]);
          channel.postMessage({ gpax: 'mail-ack', id: m.id, ok: true, path: out.saved[0]?.path ?? '',
                                text: out.new ? '개인 업무 맥락에 넣었습니다.' : '이미 넣은 메일입니다.' });
        } catch (err) {
          channel.postMessage({ gpax: 'mail-ack', id: m.id, ok: false, text: (err as Error).message });
        }
      } else if (m.gpax === 'sessions') {
        try {
          const list = await api.listSessions();
          channel.postMessage({ gpax: 'sessions-reply', id: m.id, sessions: list.slice(0, 30).map((s) => ({
            id: s.id,
            label: `${s.title || '(새 업무)'}${s.workflow ?? s.organization ? ` · ${s.workflow ?? s.organization}` : ''}`,
          })) });
        } catch {
          /* 목록이 없으면 새 업무만 고를 수 있다 */
        }
      } else if (m.gpax === 'chat-offer') {
        channel.postMessage({ gpax: 'chat-claim', id: m.id, tab: me,
                              smartwork: location.hash.startsWith('#/smartwork'),
                              visible: document.visibilityState === 'visible' });
      } else if (m.gpax === 'chat' && m.tab === me) {
        const mail = webMailOf(m.mail);
        if (!mail) return;
        offerMailChat({ sessionId: typeof m.sessionId === 'number' ? m.sessionId : null,
                        text: String(m.text || ''), mail, path: String(m.path || '') });
        navigate('/smartwork');
        channel.postMessage({ gpax: 'chat-ack', id: m.id, ok: true });
      }
    };
    return () => channel.close();
  }, [navigate]);
  return null;
}
