import { useEffect, useRef, useState } from 'react';
import { DEFAULT_MAIL_ASK, MAIL_CHANNEL, OUTLOOK_ORIGINS, type WebMail, webMailOf } from '../lib/webmail';

// 로그인한 콘솔 탭이 답할 때까지 기다리는 시간
const ACK_MS = 6000;
// 대화를 맡겠다는 탭들의 손을 모으는 시간 — 그중 하나만 고른다
const CLAIM_MS = 400;

const newId = () => `${Date.now()}-${Math.random().toString(36).slice(2)}`;

/**
 * 메일 받는 창 — 웹 Outlook의 「GPAX 메일」 북마크(lib/mailAgent.js)가 팝업으로 연다. 로그인 없이 열린다:
 * 다른 사이트가 연 창이라 콘솔의 로그인(sessionStorage)이 따라오지 않는다. 그래서 받은 메일을 같은
 * 브라우저의 로그인한 콘솔 탭으로 넘기고(BroadcastChannel), 그 탭이 저장한다(components/MailRelay).
 * 메일은 보낸 쪽이 웹 Outlook 주소이고 이 창을 연 그 창일 때만 받는다.
 *
 * 넣은 뒤에는 그 메일로 스마트워크 대화를 바로 이을 수 있다 — 새 업무나 기존 업무를 고르고 요청을 쓰면
 * 콘솔 탭 하나가 스마트워크로 가서 메일을 붙여 보낸다.
 */
export default function MailReceive() {
  const [state, setState] = useState<{ kind: 'wait' | 'sending' | 'ok' | 'fail'; text: string }>({
    kind: 'wait', text: '웹 Outlook에서 메일을 받는 중…',
  });
  const [sessions, setSessions] = useState<{ id: number; label: string }[]>([]);
  const [target, setTarget] = useState(0);   // 0 = 새 업무
  const [ask, setAsk] = useState('');
  const [chat, setChat] = useState<{ kind: '' | 'sending' | 'done' | 'fail'; text: string }>({ kind: '', text: '' });
  const channel = useRef<BroadcastChannel | null>(null);
  const waiting = useRef<Record<string, (m: Record<string, unknown>) => void>>({});
  const mail = useRef<WebMail | null>(null);
  const path = useRef('');

  // 채널로 묻고 답을 기다린다 — 답이 여럿이면 첫 답(또는 모은 답)을 쓴다.
  const ask1 = (msg: Record<string, unknown>, reply: string, ms: number) =>
    new Promise<Record<string, unknown> | null>((resolve) => {
      const id = newId();
      const timer = window.setTimeout(() => { delete waiting.current[id]; resolve(null); }, ms);
      waiting.current[id] = (m) => {
        if (m.gpax !== reply) return;
        window.clearTimeout(timer);
        delete waiting.current[id];
        resolve(m);
      };
      channel.current?.postMessage({ ...msg, id });
    });

  useEffect(() => {
    const opener = window.opener as Window | null;
    if (!opener) {
      setState({ kind: 'fail', text: '웹 Outlook의 「GPAX 메일」 북마크가 여는 창입니다 — 웹 Outlook에서 메일을 연 채 북마크를 누르세요.' });
      return;
    }
    const ch = new BroadcastChannel(MAIL_CHANNEL);
    channel.current = ch;
    ch.onmessage = (e: MessageEvent) => {
      const m = e.data;
      if (m?.id && waiting.current[m.id]) waiting.current[m.id](m);
    };
    const onMessage = async (e: MessageEvent) => {
      if (!OUTLOOK_ORIGINS.includes(e.origin) || e.source !== opener || e.data?.gpax !== 'mail') return;
      const got = webMailOf(e.data.mail);
      if (!got) {
        setState({ kind: 'fail', text: '읽은 메일이 비어 있습니다 — 메일을 읽기 창에 연 채 다시 누르세요.' });
        return;
      }
      mail.current = got;
      setState({ kind: 'sending', text: `「${got.subject || '(제목 없음)'}」을 콘솔로 넘기는 중…` });
      const ack = await ask1({ gpax: 'mail', mail: got }, 'mail-ack', ACK_MS);
      if (!ack) {
        setState({ kind: 'fail', text: '로그인한 콘솔 탭이 없습니다 — 이 브라우저의 다른 탭에서 GPAX 콘솔에 로그인해 두고 북마크를 다시 누르세요.' });
        return;
      }
      path.current = String(ack.path || '');
      setState({ kind: ack.ok ? 'ok' : 'fail', text: String(ack.text || '') });
      if (ack.ok) {
        const list = await ask1({ gpax: 'sessions' }, 'sessions-reply', ACK_MS);
        if (list && Array.isArray(list.sessions)) setSessions(list.sessions as { id: number; label: string }[]);
      }
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
      ch.close();
      channel.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 대화를 맡을 탭 하나 — 스마트워크를 연 탭, 보이는 탭 순으로
  const startChat = async () => {
    if (!mail.current || !channel.current) return;
    setChat({ kind: 'sending', text: '콘솔 탭을 찾는 중…' });
    const id = newId();
    const claims: { tab: string; smartwork: boolean; visible: boolean }[] = [];
    waiting.current[id] = (m) => {
      if (m.gpax === 'chat-claim') claims.push(m as unknown as typeof claims[number]);
    };
    channel.current.postMessage({ gpax: 'chat-offer', id });
    await new Promise((resolve) => window.setTimeout(resolve, CLAIM_MS));
    delete waiting.current[id];
    const pick = [...claims].sort((a, b) => Number(b.smartwork) - Number(a.smartwork) || Number(b.visible) - Number(a.visible))[0];
    if (!pick) {
      setChat({ kind: 'fail', text: '로그인한 콘솔 탭이 없습니다 — 콘솔 탭을 열어 두고 다시 누르세요.' });
      return;
    }
    const ack = await ask1({ gpax: 'chat', tab: pick.tab, sessionId: target || null, text: ask.trim() || DEFAULT_MAIL_ASK,
                             mail: mail.current, path: path.current }, 'chat-ack', ACK_MS);
    if (!ack) {
      setChat({ kind: 'fail', text: '콘솔 탭이 답하지 않습니다 — 다시 누르세요.' });
      return;
    }
    setChat({ kind: 'done', text: '콘솔 탭의 스마트워크에서 대화가 이어집니다 — 그 탭으로 가 보세요.' });
    window.setTimeout(() => window.close(), 3000);
  };

  return (
    <div style={{ padding: 20, maxWidth: 520 }}>
      <h3 style={{ marginTop: 0 }}>GPAX 메일</h3>
      <p className={state.kind === 'fail' ? 'error' : state.kind === 'ok' ? '' : 'mutedtext'}>
        {state.kind === 'ok' && <span className="status ok" style={{ marginRight: 6 }}>완료</span>}
        {state.text}
      </p>
      {state.kind === 'ok' && chat.kind !== 'done' && (
        <div style={{ borderTop: '1px solid var(--border)', paddingTop: 10 }}>
          <div style={{ fontSize: 13, marginBottom: 6 }}>이 메일로 스마트워크 대화</div>
          <select value={target} onChange={(e) => setTarget(Number(e.target.value))} style={{ width: '100%' }}
                  aria-label="이어 갈 업무">
            <option value={0}>새 업무</option>
            {sessions.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
          </select>
          <textarea rows={3} value={ask} placeholder={DEFAULT_MAIL_ASK} style={{ width: '100%', marginTop: 6 }}
                    onChange={(e) => setAsk(e.target.value)} />
          <div className="row" style={{ gap: 8, marginTop: 6 }}>
            <button disabled={chat.kind === 'sending'} onClick={() => void startChat()}>대화 시작</button>
            <button className="secondary" onClick={() => window.close()}>닫기</button>
          </div>
          <p className="mutedtext" style={{ fontSize: 12 }}>
            메일은 이 요청의 첨부로 붙고, 그 업무의 맥락에 「메일」이 켜져 다음 대화에서도 찾아 씁니다.
          </p>
        </div>
      )}
      {chat.text && <p className={chat.kind === 'fail' ? 'error' : 'mutedtext'}>{chat.text}</p>}
    </div>
  );
}
