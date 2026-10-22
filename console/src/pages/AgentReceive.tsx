import { useEffect, useRef, useState } from 'react';
import {
  AGENT_CHANNEL, DEFAULT_MAIL_ASK, DEFAULT_PAGE_ASK, type MailChat, OUTLOOK_ORIGINS, type WebMail, webMailOf,
} from '../lib/webmail';

// 로그인한 콘솔 탭이 답할 때까지 기다리는 시간
const ACK_MS = 8000;
// 결과 올리기는 파일이 많으면 오래 걸린다
const UPLOAD_MS = 120000;
// 맡겠다는 탭들의 손을 모으는 시간 — 그중 하나만 고른다
const CLAIM_MS = 400;
const MAX_LINES = 200;

type Source = { id: number; name: string; origin: string; queries: unknown[]; version: string };
const newId = () => `${Date.now()}-${Math.random().toString(36).slice(2)}`;

/**
 * 받는 창 — 통합 북마크 「GPAX」(lib/agentCode.ts)가 팝업으로 연다. 로그인 없이 열린다: 다른 사이트가 연
 * 창이라 콘솔의 로그인(sessionStorage)이 따라오지 않는다. 그래서 같은 브라우저의 로그인한 콘솔 탭 **하나**를
 * 골라(claim) 콘솔 API는 그 탭이 부르게 한다(BroadcastChannel → components/AgentRelay).
 *
 *   웹 Outlook   → 메일(mail)을 개인 업무 맥락에 넣고, 그 메일로 스마트워크 대화를 고른다.
 *   다른 사이트  → 북마크의 인사(hello)에 그 사이트로 등록한 출처와 수집 조건을 답한다(start). 북마크가 그
 *                  자리에서 스캔·녹화·봇을 돌리면 진행을 보이고 결과를 그 출처에 올린다. 「스마트워크로」가
 *                  넘긴 화면(page)으로 대화를 고를 수 있다(화면은 개인 맥락에 넣지 않고 그 요청에만 붙인다).
 *
 * 메일은 보낸 쪽이 웹 Outlook 주소일 때만, 나머지는 이 창을 연 그 창(opener)에서 온 것만 받는다 — 출처는
 * 브라우저가 알려 준 보낸 쪽 주소로 찾으므로 다른 사이트가 남의 출처에 결과를 올릴 수 없다.
 */
export default function AgentReceive() {
  const [state, setState] = useState<{ kind: 'wait' | 'busy' | 'ok' | 'fail'; text: string }>({
    kind: 'wait', text: '콘솔 탭을 찾는 중…',
  });
  const [site, setSite] = useState('');
  const [choices, setChoices] = useState<Source[]>([]);
  const [picked, setPicked] = useState(0);
  const [lines, setLines] = useState<string[]>([]);
  const [item, setItem] = useState<{ kind: MailChat['kind']; mail: WebMail; path: string; from: string } | null>(null);
  const [sessions, setSessions] = useState<{ id: number; label: string }[]>([]);
  const [target, setTarget] = useState(0);   // 0 = 새 업무
  const [ask, setAsk] = useState('');
  const [chat, setChat] = useState<{ kind: '' | 'sending' | 'done' | 'fail'; text: string }>({ kind: '', text: '' });
  const channel = useRef<BroadcastChannel | null>(null);
  const waiting = useRef<Record<string, (m: Record<string, unknown>) => void>>({});
  const tab = useRef('');
  const link = useRef<{ nonce: string; origin: string; source: Source | null } | null>(null);

  // 고른 탭에 묻고 답을 기다린다 — 답이 없으면 null
  const call = (msg: Record<string, unknown>, reply: string, ms = ACK_MS) =>
    new Promise<Record<string, unknown> | null>((resolve) => {
      const id = newId();
      const timer = window.setTimeout(() => { delete waiting.current[id]; resolve(null); }, ms);
      waiting.current[id] = (m) => {
        if (m.gpax !== reply && m.gpax !== `${msg.gpax}-ack`) return;
        window.clearTimeout(timer);
        delete waiting.current[id];
        resolve(m);
      };
      channel.current?.postMessage({ ...msg, id, tab: tab.current });
    });
  const push = (t: string) => setLines((prev) => [...prev, t].slice(-MAX_LINES));
  const loadSessions = async () => {
    const list = await call({ gpax: 'sessions' }, 'sessions-reply');
    if (list && Array.isArray(list.sessions)) setSessions(list.sessions as { id: number; label: string }[]);
  };

  // 북마크에 이 사이트의 출처를 알려 통로를 연다 — 등록하지 않은 사이트면 출처 없이(스마트워크로만)
  const start = (source: Source | null) => {
    const opener = window.opener as Window | null;
    if (!opener || !link.current) return;
    link.current.source = source;
    opener.postMessage({ gpax: 'start', nonce: link.current.nonce, source }, link.current.origin);
    setChoices([]);
    setState({ kind: 'ok', text: source
      ? `「${source.name}」과 이었습니다 — 사이트 화면의 GPAX 창에서 할 일을 고르세요.`
      : '등록하지 않은 사이트와 이었습니다 — 사이트 화면의 GPAX 창에서 「스마트워크로」를 누르세요.' });
  };

  useEffect(() => {
    const opener = window.opener as Window | null;
    if (!opener) {
      setState({ kind: 'fail', text: '「GPAX」 북마크가 여는 창입니다 — 웹 Outlook이나 업무 사이트에서 북마크를 누르세요.' });
      return;
    }
    const ch = new BroadcastChannel(AGENT_CHANNEL);
    channel.current = ch;
    ch.onmessage = (e: MessageEvent) => {
      const m = e.data;
      if (m?.id && waiting.current[m.id]) waiting.current[m.id](m);
    };

    const onMessage = async (e: MessageEvent) => {
      if (e.source !== opener || !e.data || typeof e.data !== 'object') return;
      const m = e.data as Record<string, unknown>;
      if (m.gpax === 'mail' && OUTLOOK_ORIGINS.includes(e.origin)) {
        const mail = webMailOf(m.mail);
        if (!mail) {
          setState({ kind: 'fail', text: '읽은 메일이 비어 있습니다 — 메일을 읽기 창에 연 채 다시 누르세요.' });
          return;
        }
        setState({ kind: 'busy', text: `「${mail.subject || '(제목 없음)'}」을 콘솔로 넘기는 중…` });
        const ack = await call({ gpax: 'mail', mail }, 'mail-ack');
        if (!ack) {
          setState({ kind: 'fail', text: '콘솔 탭이 답하지 않습니다 — 북마크를 다시 누르세요.' });
          return;
        }
        setState({ kind: ack.ok ? 'ok' : 'fail', text: String(ack.text || '') });
        if (ack.ok) {
          setItem({ kind: 'mail', mail, path: String(ack.path || ''), from: e.origin });
          void loadSessions();
        }
      } else if (m.gpax === 'hello' && !link.current) {
        link.current = { nonce: newId(), origin: e.origin, source: null };
        setSite(e.origin);
        setState({ kind: 'busy', text: `${e.origin} — 등록한 출처를 찾는 중…` });
        const found = await call({ gpax: 'source', origin: e.origin }, 'source-reply');
        const list = (found && Array.isArray(found.sources) ? found.sources : []) as Source[];
        if (list.length > 1) {
          setChoices(list);
          setPicked(list[0].id);
          setState({ kind: 'wait', text: '이 사이트로 등록한 출처가 여럿입니다 — 이을 출처를 고르세요.' });
        } else {
          start(list[0] ?? null);
        }
      } else if (link.current && m.nonce === link.current.nonce && e.origin === link.current.origin) {
        if (m.gpax === 'log' && typeof m.text === 'string') push(m.text.slice(0, 300));
        if (m.gpax === 'result' && link.current.source) {
          const kind = m.kind === 'record' ? 'record' : 'scan';
          setState({ kind: 'busy', text: '결과를 콘솔로 올리는 중…' });
          const ack = await call({ gpax: 'upload', kind, sourceId: link.current.source.id, payload: m.payload },
                                 'upload-ack', UPLOAD_MS);
          setState(ack ? { kind: ack.ok ? 'ok' : 'fail', text: String(ack.text || '') }
                       : { kind: 'fail', text: '콘솔 탭이 답하지 않습니다 — 콘솔 탭을 열어 둔 채 다시 하세요.' });
        }
        if (m.gpax === 'page') {
          const page = webMailOf(m.item);
          if (page) {
            setItem({ kind: 'page', mail: page, path: '', from: e.origin });
            setChat({ kind: '', text: '' });
            void loadSessions();
          }
        }
      }
    };
    window.addEventListener('message', onMessage);

    // 콘솔 탭 하나를 고른 뒤에 북마크에 「준비됨」을 알린다 — 스마트워크를 연 탭, 보이는 탭 순으로
    void (async () => {
      const id = newId();
      const claims: { tab: string; smartwork: boolean; visible: boolean }[] = [];
      waiting.current[id] = (m) => {
        if (m.gpax === 'claim') claims.push(m as unknown as typeof claims[number]);
      };
      ch.postMessage({ gpax: 'claim-offer', id });
      await new Promise((resolve) => window.setTimeout(resolve, CLAIM_MS));
      delete waiting.current[id];
      const pick = [...claims].sort((a, b) => Number(b.smartwork) - Number(a.smartwork) || Number(b.visible) - Number(a.visible))[0];
      if (!pick) {
        setState({ kind: 'fail', text: '로그인한 콘솔 탭이 없습니다 — 이 브라우저의 다른 탭에서 GPAX 콘솔에 로그인해 두고 북마크를 다시 누르세요.' });
        return;
      }
      tab.current = pick.tab;
      setState({ kind: 'wait', text: '사이트와 잇는 중…' });
      // 「준비됨」에는 아무것도 싣지 않는다 — 이 창을 연 사이트가 어디든 같은 말이다.
      opener.postMessage({ gpax: 'ready' }, '*');
    })();
    return () => {
      window.removeEventListener('message', onMessage);
      ch.close();
      channel.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startChat = async () => {
    if (!item) return;
    setChat({ kind: 'sending', text: '콘솔 탭에 넘기는 중…' });
    const ack = await call({ gpax: 'chat', kind: item.kind, sessionId: target || null, mail: item.mail, path: item.path,
                             from: item.from, text: ask.trim() || (item.kind === 'page' ? DEFAULT_PAGE_ASK : DEFAULT_MAIL_ASK) },
                           'chat-ack');
    if (!ack?.ok) {
      setChat({ kind: 'fail', text: String(ack?.text || '콘솔 탭이 답하지 않습니다 — 다시 누르세요.') });
      return;
    }
    setChat({ kind: 'done', text: '콘솔 탭의 스마트워크에서 대화가 이어집니다 — 그 탭으로 가 보세요.' });
    if (item.kind === 'mail') window.setTimeout(() => window.close(), 3000);
  };

  return (
    <div style={{ padding: 20, maxWidth: 560 }}>
      <h3 style={{ marginTop: 0 }}>GPAX</h3>
      {site && <div className="mono mutedtext" style={{ fontSize: 11 }}>사이트 {site}</div>}
      <p className={state.kind === 'fail' ? 'error' : state.kind === 'ok' ? '' : 'mutedtext'}>
        {state.kind === 'ok' && <span className="status ok" style={{ marginRight: 6 }}>완료</span>}
        {state.text}
      </p>
      {choices.length > 1 && (
        <div className="row" style={{ gap: 8 }}>
          <select value={picked} onChange={(e) => setPicked(Number(e.target.value))} aria-label="이을 출처">
            {choices.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
          <button onClick={() => start(choices.find((s) => s.id === picked) ?? null)}>잇기</button>
        </div>
      )}
      {item && chat.kind !== 'done' && (
        <div style={{ borderTop: '1px solid var(--border)', paddingTop: 10, marginTop: 8 }}>
          <div style={{ fontSize: 13, marginBottom: 6 }}>
            {item.kind === 'page' ? `이 화면 「${item.mail.subject || '(제목 없음)'}」으로` : '이 메일로'} 스마트워크 대화
          </div>
          <select value={target} onChange={(e) => setTarget(Number(e.target.value))} style={{ width: '100%' }}
                  aria-label="이어 갈 업무">
            <option value={0}>새 업무</option>
            {sessions.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
          </select>
          <textarea rows={3} value={ask} placeholder={item.kind === 'page' ? DEFAULT_PAGE_ASK : DEFAULT_MAIL_ASK}
                    style={{ width: '100%', marginTop: 6 }} onChange={(e) => setAsk(e.target.value)} />
          <div className="row" style={{ gap: 8, marginTop: 6 }}>
            <button disabled={chat.kind === 'sending'} onClick={() => void startChat()}>대화 시작</button>
            <button className="secondary" onClick={() => window.close()}>닫기</button>
          </div>
          <p className="mutedtext" style={{ fontSize: 12 }}>
            {item.kind === 'page'
              ? '화면 글자는 이 요청의 첨부로만 붙습니다(개인 업무 맥락에는 넣지 않습니다).'
              : '메일은 이 요청의 첨부로 붙고, 그 업무의 맥락에 「메일」이 켜져 다음 대화에서도 찾아 씁니다.'}
          </p>
        </div>
      )}
      {chat.text && <p className={chat.kind === 'fail' ? 'error' : 'mutedtext'}>{chat.text}</p>}
      {lines.length > 0 && (
        <pre className="mono" style={{ fontSize: 11, maxHeight: 260, overflow: 'auto', whiteSpace: 'pre-wrap' }}>
          {lines.join('\n')}
        </pre>
      )}
    </div>
  );
}
