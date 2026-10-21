import { useEffect } from 'react';
import { api } from '../lib/api';
import { MAIL_CHANNEL, webMailOf } from '../lib/webmail';

/**
 * 로그인한 콘솔 탭에서 — 메일 받는 창(pages/MailReceive)이 넘긴 웹 Outlook 메일을 이 사람의 개인 업무
 * 맥락에 넣고 답한다. 탭이 여럿이면 모두 넣지만 서버가 같은 메일을 다시 쓰지 않는다(먼저 온 답을 받는 창이 쓴다).
 */
export default function MailRelay() {
  useEffect(() => {
    const channel = new BroadcastChannel(MAIL_CHANNEL);
    channel.onmessage = async (e: MessageEvent) => {
      const m = e.data;
      if (m?.gpax !== 'mail' || !m.id) return;
      const mail = webMailOf(m.mail);
      if (!mail) return;
      try {
        const out = await api.saveWebMail([mail]);
        channel.postMessage({ gpax: 'mail-ack', id: m.id, ok: true,
                              text: out.new ? '개인 업무 맥락에 넣었습니다.' : '이미 넣은 메일입니다.' });
      } catch (err) {
        channel.postMessage({ gpax: 'mail-ack', id: m.id, ok: false, text: (err as Error).message });
      }
    };
    return () => channel.close();
  }, []);
  return null;
}
