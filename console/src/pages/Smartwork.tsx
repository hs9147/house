import { useEffect, useRef, useState } from 'react';
import Async from '../components/Async';
import Split from '../components/Split';
import { api } from '../lib/api';
import { getEmail } from '../lib/auth';
import { type AsyncState, useApi, usePolling } from '../lib/hooks';
import { parseCsv, renderMarkdown } from '../lib/markdown';
import { fetchInbox, redirectUri } from '../lib/msgraph';
import type {
  HealthInfo,
  PersonalStatus,
  SmartworkAgent,
  SmartworkAttachmentIn,
  SmartworkContext,
  SmartworkOrgChoice,
  SmartworkReport,
  SmartworkSession,
  SmartworkSessionMessage,
  SmartworkSessionSummary,
} from '../lib/types';

/**
 * 스마트워크 — 왼쪽 대화·설정(에이전트·부서 워크플로·내 업무 맥락), 오른쪽 화면(에이전트·보고서).
 *
 * 대화는 세션 단위다 — **세션 하나 = 업무 하나.** 소유자가 그 업무의 조직·워크플로를
 * 대화창에서 고르고, 필요하면 다른 사람(다른 부서여도)과 공유한다. 공유받은 사람은 읽고
 * 말할 수 있고, 개인 맥락(폴더·메일)은 각자 자기 것만 이 업무에 고른다.
 *
 * 오른쪽에 무엇을 띄울지는 서버가 정한다(모델이 show_agent·show_report 도구로 고른다).
 * 화면은 그 결과만 따른다 — 에이전트가 오면 보고서보다 **먼저** 에이전트를 연다.
 * 새 업무는 빈 화면이 아니라 **제안으로 시작한다**: 만들자마자 빈 내용으로 한 턴을 돌려,
 * 부서 워크플로와 개인 업무 맥락을 본 모델이 할 일을 버튼으로 띄운다.
 */

type View = 'agent' | 'report';
// 공유 세션은 다른 참여자가 보낸 말을 이 간격으로 다시 읽는다.
const SHARED_POLL_MS = 5000;

function sessionLabel(s: SmartworkSessionSummary): string {
  const task = s.workflow ?? s.organization;
  return `${s.title || '(새 업무)'}${task ? ` · ${task}` : ''}${s.members > 1 ? ` · 공유 ${s.members}명` : ''}`;
}

export default function Smartwork() {
  const health = useApi(() => api.health());
  const agents = useApi(() => api.smartworkAgents());
  const workflows = useApi(() => api.smartworkWorkflows());
  const personal = useApi(() => api.personalStatus());
  const orgs = useApi(() => api.smartworkOrgs());
  const sessions = useApi(() => api.listSessions());
  const [session, setSession] = useState<SmartworkSession | null>(null);
  const [creating, setCreating] = useState(false);
  // 보냈는데 아직 답이 없는 말('' = 여는 턴). 서버는 답이 나온 뒤에 둘을 함께 남기므로
  // 실패하면 여기 남은 것을 그대로 다시 보낸다.
  const [pending, setPending] = useState<string | null>(null);
  const [input, setInput] = useState('');
  // 이번 요청에 붙일 참고 자료 — 보내기에 성공해야 비운다(실패하면 다시 시도에 그대로 실린다).
  const [files, setFiles] = useState<(SmartworkAttachmentIn & { size: number })[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sharing, setSharing] = useState(false);
  const [agent, setAgent] = useState<SmartworkAgent | null>(null);
  const [report, setReport] = useState<SmartworkReport | null>(null);
  const [view, setView] = useState<View>('agent');
  const [leftTab, setLeftTab] = useState<'chat' | 'settings'>('chat');
  const opened = useRef(false);
  // 폴링(usePolling)은 처음 받은 함수를 계속 부른다 — 지금 세션은 ref로 읽는다.
  const current = useRef<SmartworkSession | null>(null);
  current.current = session;

  const open = async (id: number) => {
    setError(null);
    setPending(null);
    setCreating(false);
    setSharing(false);
    try {
      const s = await api.getSession(id);
      setSession(s);
      // 연 업무의 화면은 그 대화가 마지막으로 띄운 에이전트·보고서다.
      const last = [...s.messages].reverse().find((m) => m.agent || m.report);
      setAgent(last?.agent ?? null);
      setReport(last?.report ?? null);
      setView(last?.agent ? 'agent' : 'report');
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    // 처음 들어오면 가장 최근 업무를 연다. 없으면 새 업무를 고르는 화면부터.
    // StrictMode가 개발 모드에서 effect를 두 번 부른다 — ref로 한 번만.
    if (opened.current || !sessions.data) return;
    opened.current = true;
    if (sessions.data.length > 0) void open(sessions.data[0].id);
    else setCreating(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions.data]);

  // 마지막으로 받은 메시지 뒤의 것만 읽어 붙인다 — 공유 세션 폴링과 내 턴 뒤에 쓴다.
  const catchUp = async () => {
    const s = current.current;
    if (!s) return;
    const after = s.messages.length > 0 ? s.messages[s.messages.length - 1].id : 0;
    const fresh = await api.getSession(s.id, after);
    setSession((cur) => {
      if (!cur || cur.id !== fresh.id) return cur;
      const seen = new Set(cur.messages.map((m) => m.id));
      return { ...fresh, messages: [...cur.messages, ...fresh.messages.filter((m) => !seen.has(m.id))] };
    });
  };
  usePolling(catchUp, SHARED_POLL_MS, !!session && session.members.length > 1 && !busy);

  const send = async (s: SmartworkSession, content: string, attachments: SmartworkAttachmentIn[] = []) => {
    setPending(content);
    setBusy(true);
    setError(null);
    try {
      const out = await api.sendSessionMessage(s.id, content, attachments);
      await catchUp();
      setPending(null);
      if (attachments.length > 0) setFiles([]);
      if (out.report) {
        setReport(out.report);
        setView('report');
      }
      if (out.agent) {
        setAgent(out.agent);
        setView('agent');
      }
      sessions.reload();  // 첫 요청이 제목이 된다
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const start = async (orgId: number | null, workflowId: number | null) => {
    setBusy(true);
    setError(null);
    let s: SmartworkSession;
    try {
      s = await api.createSession({ organization_id: orgId, workflow_id: workflowId });
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
      return;
    }
    current.current = s;
    setSession(s);
    setCreating(false);
    setSharing(false);
    setAgent(null);
    setReport(null);
    sessions.reload();
    await send(s, '');
  };

  const patch = async (changes: { organization_id: number | null; workflow_id: number | null }) => {
    if (!session) return;
    setError(null);
    try {
      setSession(await api.updateSession(session.id, changes));
      sessions.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const reloadSession = async () => {
    if (session) setSession(await api.getSession(session.id));
  };

  const leave = () => {
    setSession(null);
    setCreating(true);
    setAgent(null);
    setReport(null);
    sessions.reload();
  };

  const remove = async () => {
    if (!session || !window.confirm('이 업무(대화 전체)를 지울까요? 공유한 사람에게서도 사라집니다.')) return;
    try {
      await api.deleteSession(session.id);
      leave();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const submit = () => {
    const text = input.trim();
    if (!text || busy || !session) return;
    setInput('');
    void send(session, text, files.map(({ name, type, data }) => ({ name, type, data })));
  };

  const attach = async (list: File[]) => {
    setError(null);
    const total = [...files, ...list].reduce((sum, f) => sum + f.size, 0);
    if (files.length + list.length > MAX_ATTACHMENTS || total > MAX_ATTACHMENT_TOTAL) {
      setError(`첨부는 ${MAX_ATTACHMENTS}개, 합해서 ${mb(MAX_ATTACHMENT_TOTAL)}까지입니다.`);
      return;
    }
    const read = await Promise.all(list.map(async (f) => ({
      name: f.name || 'clipboard.png', type: f.type, size: f.size, data: await base64Of(f),
    })));
    setFiles((cur) => [...cur, ...read]);
  };

  const me = getEmail();
  const shared = (session?.members.length ?? 0) > 1;
  const messages = session?.messages ?? [];
  const last = messages.length > 0 ? messages[messages.length - 1] : null;
  const settled = !busy && pending === null && last?.role === 'assistant';
  const suggestions = settled ? last.suggestions ?? [] : [];
  const choices = settled ? last.choices ?? [] : [];

  const chat = (
    <div className="panel">
      <div className="row" style={{ marginBottom: 8 }}>
        <select
          style={{ flex: 1, minWidth: 0 }}
          value={session?.id ?? ''}
          disabled={busy}
          onChange={(e) => e.target.value && void open(Number(e.target.value))}
        >
          {!session && <option value="">업무 선택</option>}
          {(sessions.data ?? []).map((s) => <option key={s.id} value={s.id}>{sessionLabel(s)}</option>)}
        </select>
        <button className="small" disabled={busy} onClick={() => {
          setSession(null);
          setCreating(true);
          setError(null);
          setPending(null);
        }}>새 업무</button>
      </div>

      {creating && !session && (
        <NewSession orgs={orgs.data ?? []} busy={busy} onStart={(o, w) => void start(o, w)} />
      )}

      {session && (
        <>
          <div className="row" style={{ flexWrap: 'wrap', marginBottom: 6 }}>
            {session.is_owner ? (
              <TaskPicker orgs={orgs.data ?? []} orgId={session.organization_id}
                workflowId={session.workflow_id} disabled={busy}
                onChange={(o, w) => void patch({ organization_id: o, workflow_id: w })} />
            ) : (
              <span className="mutedtext" style={{ fontSize: 12 }}>
                업무: {session.organization ?? '조직 없음'} / {session.workflow ?? '워크플로 없음'} ·
                소유자 {session.owner}
              </span>
            )}
            <span className="spacer" />
            <button className="small secondary" onClick={() => setSharing(!sharing)}>
              공유{shared ? ` (${session.members.length})` : ''}
            </button>
            {session.is_owner && (
              <button className="small secondary" disabled={busy} onClick={() => void remove()}>삭제</button>
            )}
          </div>
          {sharing && (
            <SharePanel session={session} me={me}
              onChanged={() => void reloadSession().then(() => sessions.reload())} onLeft={leave} />
          )}
          <ContextPicker session={session} status={personal.data}
            onSaved={(ctx) => setSession((cur) => cur && { ...cur, my_context: ctx })}
            onError={setError} />

          <div className="chat-thread">
            {messages.map((m) => <Message key={m.id} message={m} showAuthor={shared} />)}
            {pending && <div className="chat-msg user">{pending}</div>}
            {busy && pending !== null && (
              <p className="mutedtext">
                {pending === ''
                  ? '부서 워크플로와 업무 맥락을 살펴보고 진행할 업무를 고르는 중...'
                  : '답변을 만드는 중...'}
              </p>
            )}
          </div>
          {choices.length > 0 && (
            <div className="row" style={{ flexWrap: 'wrap', gap: 6, marginBottom: 12 }}>
              {choices.map((c, i) => (
                <button key={i} className="small secondary" title={c.prompt}
                  onClick={() => void send(session, c.prompt)}>
                  {c.label}
                </button>
              ))}
            </div>
          )}
          {suggestions.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
              {suggestions.map((s, i) => (
                <button
                  key={i}
                  className="secondary"
                  style={{ textAlign: 'left' }}
                  title={s.prompt}
                  onClick={() => void send(session, s.prompt)}
                >
                  {s.title}
                  <div className="mutedtext" style={{ fontSize: 12 }}>
                    {[`대상: ${[s.target.kind, s.target.name].filter(Boolean).join(' ')}`
                        + (s.target.state ? ` (${s.target.state})` : ''),
                      s.workflow && `워크플로: ${s.workflow}`, s.why].filter(Boolean).join(' · ')}
                  </div>
                </button>
              ))}
            </div>
          )}
        </>
      )}
      {error && (
        <div className="row" style={{ marginBottom: 10 }}>
          <p className="error" style={{ margin: 0 }}>{error}</p>
          {!busy && session && pending !== null && (
            <button className="small secondary" onClick={() => void send(session, pending,
              files.map(({ name, type, data }) => ({ name, type, data })))}>다시 시도</button>
          )}
        </div>
      )}
      {session && (
        <>
          <textarea
            rows={3}
            value={input}
            placeholder="무엇을 할까요? (Enter 보내기, Shift+Enter 줄바꿈)"
            onChange={(e) => setInput(e.target.value)}
            onPaste={(e) => {
              // 화면 캡처처럼 글 없이 파일만 든 클립보드는 첨부로 받는다. 엑셀 셀을 복사하면
              // 글과 함께 그림도 실리는데, 그때는 글을 붙이는 쪽이 사람이 바란 것이다.
              const pasted = Array.from(e.clipboardData.files);
              if (pasted.length > 0 && !e.clipboardData.types.includes('text/plain')) {
                e.preventDefault();
                void attach(pasted);
              }
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              }
            }}
          />
          {files.length > 0 && (
            <div className="row" style={{ flexWrap: 'wrap', gap: 6, marginTop: 6 }}>
              {files.map((f, i) => (
                <span key={i} className="viewtab">
                  <span className="status dim">{f.name} · {mb(f.size)}</span>
                  <button className="small secondary" title="빼기" aria-label={`${f.name} 빼기`}
                    onClick={() => setFiles((cur) => cur.filter((_, j) => j !== i))}>×</button>
                </span>
              ))}
            </div>
          )}
          <div className="row" style={{ marginTop: 8 }}>
            <button onClick={submit} disabled={busy || !input.trim()}>보내기</button>
            <button className="secondary" disabled={busy} onClick={() => fileInput.current?.click()}>
              첨부
            </button>
            <input ref={fileInput} type="file" multiple hidden
              accept={[...DOC_SUFFIXES, ...ATTACH_IMAGE_TYPES].join(',')}
              onChange={(e) => {
                void attach(Array.from(e.target.files ?? []));
                e.target.value = '';
              }} />
            <span className="mutedtext" style={{ fontSize: 12 }}>문서·이미지, 화면 캡처는 붙여넣기</span>
          </div>
        </>
      )}
    </div>
  );

  const settings = (
    <>
      <div className="panel">
        <h2>에이전트</h2>
        <Async state={agents} empty="쓸 수 있는 에이전트가 없습니다 — 운영 중(release)인 앱이 에이전트가 됩니다.">
          {(list) => (
            <table>
              <thead><tr><th>이름</th><th>조직</th><th>종류</th><th /></tr></thead>
              <tbody>
                {list.map((a) => (
                  <tr key={a.name}>
                    <td>{a.name}</td>
                    <td>{a.org ?? '공용'}</td>
                    <td>{a.type}</td>
                    <td>
                      <button className="small secondary" onClick={() => {
                        setAgent(a);
                        setView('agent');
                      }}>열기</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Async>
      </div>
      <div className="panel">
        <h2>부서 워크플로</h2>
        <Async state={workflows} empty="소속 조직에 워크플로가 없습니다 — 업무 제안은 개인 업무 맥락으로만 합니다.">
          {(list) => (
            <table>
              <thead><tr><th>워크플로</th><th>조직</th><th>단계</th><th>대기 중</th></tr></thead>
              <tbody>
                {list.map((w) => (
                  <tr key={`${w.org}/${w.name}`}>
                    <td title={w.steps.join('\n')}>
                      {w.name}
                      {w.description && <div className="mutedtext" style={{ fontSize: 12 }}>{w.description}</div>}
                    </td>
                    <td>{w.org}</td>
                    <td>{w.steps.length}</td>
                    <td>
                      {w.waiting_runs > 0
                        ? <span className="status warn">{w.waiting_runs}건</span>
                        : <span className="mutedtext">-</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Async>
      </div>
      <PersonalPanel status={personal} />
    </>
  );

  const left = (
    <>
      <div className="row viewtabs" role="tablist">
        {([['chat', '대화'], ['settings', '설정']] as const).map(([key, label]) => (
          <button key={key} role="tab" aria-selected={leftTab === key}
            className={leftTab === key ? 'small' : 'small secondary'}
            onClick={() => setLeftTab(key)}>
            {label}
          </button>
        ))}
      </div>
      {leftTab === 'chat' ? chat : settings}
    </>
  );

  const tabs: { key: View; label: string }[] = [
    ...(agent ? [{ key: 'agent' as const, label: `에이전트 · ${agent.name}` }] : []),
    ...(report ? [{ key: 'report' as const, label: `보고서 · ${report.title}` }] : []),
  ];
  // 고른 탭이 비었으면 남은 쪽을 보인다 — 에이전트를 보고서보다 먼저.
  const shown: View | null = view === 'report' && report ? 'report' : agent ? 'agent' : report ? 'report' : null;

  const right = (
    <>
      {tabs.length > 0 && (
        <div className="row viewtabs" role="tablist">
          {tabs.map((t) => (
            <span key={t.key} className="viewtab">
              <button
                role="tab"
                aria-selected={shown === t.key}
                className={shown === t.key ? 'small' : 'small secondary'}
                onClick={() => setView(t.key)}
              >
                {t.label}
              </button>
              <button className="small secondary" title="닫기" aria-label={`${t.label} 닫기`}
                onClick={() => (t.key === 'agent' ? setAgent(null) : setReport(null))}>
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      {shown === 'agent' && agent ? (
        <AgentView agent={agent} health={health.data} />
      ) : shown === 'report' && report ? (
        <ReportView report={report} />
      ) : (
        <div className="panel">
          <p className="mutedtext">
            대화에서 에이전트나 보고서를 띄우면 여기에 나옵니다. 에이전트·부서 워크플로·내 업무
            맥락은 왼쪽 「설정」에 있습니다.
          </p>
        </div>
      )}
    </>
  );

  return (
    <Split left={left} right={right} leftWidth={440} />
  );
}

// --- 세션 ---

// 서버(services/smartwork.py)와 같은 상한 — 서버가 다시 거르지만, 다 읽어 보낸 뒤에 거절되면
// 기다린 시간이 아깝다.
const MAX_ATTACHMENTS = 5;
const MAX_ATTACHMENT_TOTAL = 20 * 1024 * 1024;
const ATTACH_IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];

function base64Of(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ''));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function Message({ message: m, showAuthor }: { message: SmartworkSessionMessage; showAuthor: boolean }) {
  return (
    <div className={`chat-msg ${m.role}`}>
      {showAuthor && m.role === 'user' && (
        <div className="mutedtext" style={{ fontSize: 11, marginBottom: 4 }}>{m.author}</div>
      )}
      {m.content}
      {m.attachments && m.attachments.length > 0 && (
        <div className="mutedtext" style={{ fontSize: 11, marginTop: 6 }}>
          첨부: {m.attachments.map((a) => a.name).join(', ')}
        </div>
      )}
      {m.tools && m.tools.length > 0 && (
        <div className="mutedtext mono" style={{ fontSize: 11, marginTop: 6 }}>
          도구: {m.tools.join(', ')}
        </div>
      )}
    </div>
  );
}

// 고를 수 있는 조직은 내 소속 조직, 워크플로는 그 조직의 것(서버도 같은 규칙으로 거른다).
function TaskPicker({ orgs, orgId, workflowId, disabled, onChange }: {
  orgs: SmartworkOrgChoice[];
  orgId: number | null;
  workflowId: number | null;
  disabled?: boolean;
  onChange: (orgId: number | null, workflowId: number | null) => void;
}) {
  const flows = orgs.find((o) => o.id === orgId)?.workflows ?? [];
  return (
    <div className="row">
      <select value={orgId ?? ''} disabled={disabled}
        onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null, null)}>
        <option value="">조직 선택 안 함</option>
        {orgs.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
      </select>
      <select value={workflowId ?? ''} disabled={disabled || orgId === null}
        onChange={(e) => onChange(orgId, e.target.value ? Number(e.target.value) : null)}>
        <option value="">워크플로 선택 안 함</option>
        {flows.map((w) => <option key={w.id} value={w.id} title={w.description}>{w.name}</option>)}
      </select>
    </div>
  );
}

function NewSession({ orgs, busy, onStart }: {
  orgs: SmartworkOrgChoice[];
  busy: boolean;
  onStart: (orgId: number | null, workflowId: number | null) => void;
}) {
  const [orgId, setOrgId] = useState<number | null>(null);
  const [workflowId, setWorkflowId] = useState<number | null>(null);
  return (
    <div className="chat-thread">
      <p className="mutedtext">
        새 업무 — 조직과 워크플로를 고르면 그 업무 안에서 제안하고 답합니다. 고르지 않으면
        소속 부서의 워크플로 전체를 보고 할 일을 제안합니다.
      </p>
      <TaskPicker orgs={orgs} orgId={orgId} workflowId={workflowId} disabled={busy}
        onChange={(o, w) => {
          setOrgId(o);
          setWorkflowId(w);
        }} />
      <div className="row" style={{ marginTop: 8 }}>
        <button disabled={busy} onClick={() => onStart(orgId, workflowId)}>시작</button>
      </div>
    </div>
  );
}

function SharePanel({ session, me, onChanged, onLeft }: {
  session: SmartworkSession;
  me: string;
  onChanged: () => void;
  onLeft: () => void;
}) {
  const [email, setEmail] = useState('');
  const [error, setError] = useState<string | null>(null);
  const act = async (fn: () => Promise<void>) => {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <div style={{ borderTop: '1px solid rgba(255,255,255,0.1)', padding: '8px 0', marginBottom: 8 }}>
      <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
        참여자는 이 업무의 대화를 <b>지금까지의 질문·답변까지</b> 읽고 말할 수 있습니다.
        조직·워크플로와 참여자는 소유자만 바꿉니다. 개인 맥락(폴더·메일)은 각자 자기 것만 씁니다.
      </p>
      <table style={{ marginBottom: 8 }}>
        <tbody>
          {session.members.map((m) => (
            <tr key={m.email}>
              <td>{m.email}{m.is_owner && <span className="status dim" style={{ marginLeft: 6 }}>소유자</span>}</td>
              <td>
                {!m.is_owner && (session.is_owner || m.email === me) && (
                  <button className="small secondary" onClick={() => void act(async () => {
                    await api.removeSessionMember(session.id, m.email);
                    if (m.email === me) onLeft();
                    else onChanged();
                  })}>{m.email === me ? '나가기' : '빼기'}</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {session.is_owner && (
        <div className="row">
          <input style={{ flex: 1, minWidth: 0 }} value={email} placeholder="공유할 사람 이메일(다른 부서도 됩니다)"
            onChange={(e) => setEmail(e.target.value)} />
          <button className="small" disabled={!email.trim()} onClick={() => void act(async () => {
            const target = email.trim();
            if (!window.confirm(`${target}에게 이 업무를 공유할까요? 지금까지의 대화와 답변이 보입니다.`)) return;
            await api.addSessionMember(session.id, target);
            setEmail('');
            onChanged();
          })}>공유</button>
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

// 내 개인 맥락 중 이 업무에 쓸 것 — 참여자마다 따로이고, 남의 선택은 보이지 않는다.
function ContextPicker({ session, status, onSaved, onError }: {
  session: SmartworkSession;
  status: PersonalStatus | null;
  onSaved: (ctx: SmartworkContext) => void;
  onError: (message: string) => void;
}) {
  if (!status?.consented) return null;
  const mailReady = status.mail.connected;
  if (status.folders.length === 0 && !mailReady) {
    return (
      <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 8px' }}>
        오른쪽 '내 업무 맥락'에서 폴더·메일을 올리면 이 업무에 고를 수 있습니다.
      </p>
    );
  }
  const ctx = session.my_context;
  const save = (next: SmartworkContext) => {
    api.setSessionContext(session.id, next).then(onSaved, (e: Error) => onError(e.message));
  };
  return (
    <div className="row" style={{ flexWrap: 'wrap', fontSize: 12, marginBottom: 8 }}>
      <span className="mutedtext">이 업무에 쓸 내 맥락(나만 씀):</span>
      {status.folders.map((f) => (
        <label key={f.name}>
          <input type="checkbox" checked={ctx.folders.includes(f.name)} onChange={(e) => save({
            ...ctx,
            folders: e.target.checked ? [...ctx.folders, f.name] : ctx.folders.filter((x) => x !== f.name),
          })} /> {f.name}
        </label>
      ))}
      {mailReady && (
        <label>
          <input type="checkbox" checked={ctx.mail} onChange={(e) => save({ ...ctx, mail: e.target.checked })} /> 메일
        </label>
      )}
    </div>
  );
}

// --- 에이전트 ---

function AgentView({ agent, health }: { agent: SmartworkAgent; health: HealthInfo | null }) {
  // 프로젝트 개요의 앱 주소와 같은 규칙(공개 주소의 스킴, 없으면 콘솔이 열린 스킴).
  const scheme = health?.public_scheme ?? window.location.protocol.replace(':', '');
  const url = health ? `${scheme}://${health.base_domain}${agent.path}` : agent.path;
  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>{agent.name}</h2>
        {agent.reason && <span className="mutedtext">{agent.reason}</span>}
        <span className="spacer" />
        <a href={url} target="_blank" rel="noreferrer">새 창으로 열기</a>
      </div>
      {/* 앱이 X-Frame-Options·frame-ancestors로 끼워 넣기를 막으면 빈 칸으로 보인다 —
          그때를 위해 새 창 링크를 함께 둔다. */}
      <iframe title={agent.name} src={url} className="smartwork-frame" />
    </div>
  );
}

// --- 보고서 ---

// HTML 보고서는 sandbox(스크립트·폼·같은 출처 모두 막음)로 띄우고, 외부 자원도 막는다 —
// 모델이 쓴 HTML이 콘솔의 키를 읽거나 바깥으로 무엇을 보내면 안 된다.
const REPORT_CSP =
  '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:">';

function ReportView({ report }: { report: SmartworkReport }) {
  let body;
  if (report.format === 'html') {
    body = <iframe title={report.title} sandbox="" srcDoc={REPORT_CSP + report.content}
      className="smartwork-frame" style={{ background: '#fff' }} />;
  } else if (report.format === 'csv') {
    const [head = [], ...rows] = parseCsv(report.content);
    body = (
      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead><tr>{head.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}
          </tbody>
        </table>
      </div>
    );
  } else {
    // renderMarkdown은 먼저 전부 이스케이프한다(lib/markdown.ts).
    body = <div className="report-md" dangerouslySetInnerHTML={{ __html: renderMarkdown(report.content) }} />;
  }
  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>{report.title}</h2>
        <span className="status dim">{report.format}</span>
      </div>
      {body}
    </div>
  );
}

// --- 개인 업무 맥락 ---

// 서버(services/personal.py DOC_SUFFIXES)와 같은 목록 — 서버가 다시 거르지만, 사진·설치
// 파일까지 목록에 실어 보낼 까닭이 없다.
const DOC_SUFFIXES = ['.pdf', '.docx', '.xlsx', '.pptx', '.hwpx', '.doc', '.xls', '.ppt', '.hwp',
  '.txt', '.md', '.csv', '.html', '.htm', '.json'];
function isDocument(name: string): boolean {
  const lower = name.toLowerCase();
  return DOC_SUFFIXES.some((s) => lower.endsWith(s));
}

function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : '-';
}

// 상태는 대화 쪽(이 업무에 쓸 내 맥락 고르기)과 같은 것을 본다 — 폴더를 올리면 거기에도 바로 보인다.
function PersonalPanel({ status }: { status: AsyncState<PersonalStatus> }) {
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  // 올라간 바이트 / 올릴 바이트 — 서버가 변환하는 동안은 100%에 머문다(그건 글로 알린다).
  const [bytes, setBytes] = useState<{ sent: number; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement | null>(null);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      setBytes(null);
      status.reload();
    }
  };

  const syncFolder = (files: FileList) => run(async () => {
    const all = Array.from(files);
    if (all.length === 0) return;
    // webkitRelativePath = "고른폴더/하위/파일" — 첫 마디가 폴더 이름이다.
    const folder = all[0].webkitRelativePath.split('/')[0];
    const items = all
      .filter((f) => isDocument(f.name) && f.size > 0)
      .map((f) => ({
        file: f,
        path: f.webkitRelativePath.split('/').slice(1).join('/'),
        mtime: f.lastModified / 1000,
      }));
    setProgress(`${folder}: 바뀐 파일을 찾는 중...`);
    const plan = await api.personalManifest(
      folder, items.map((it) => ({ path: it.path, size: it.file.size, mtime: it.mtime })));
    const needed = new Set(plan.needed);
    const todo = items.filter((it) => needed.has(it.path));
    // 하나씩, 앞 파일이 끝난 뒤에 다음 파일 — 서버가 요청 안에서 변환을 끝내므로(원본을
    // 남기지 않는다) 여러 개를 한꺼번에 보내면 OCR·구형 Office 변환이 몰려 시간 제한에 걸린다.
    let skipped = 0;
    let failed = 0;
    const total = todo.reduce((n, it) => n + it.file.size, 0);
    let done = 0;
    for (let i = 0; i < todo.length; i += 1) {
      const { path, file } = todo[i];
      const head = `${folder}: ${i + 1}/${todo.length}`;
      setProgress(`${head} 올리는 중 0% (${path})`);
      setBytes({ sent: done, total });
      const out = await api.personalUpload(folder, todo[i], (loaded, size) => {
        setBytes({ sent: done + loaded, total });
        setProgress(loaded < size
          ? `${head} 올리는 중 ${Math.floor((loaded / size) * 100)}% (${path})`
          : `${head} 변환 중... (${path})`);
      });
      done += file.size;
      if (out.status === 'skipped') skipped += 1;
      if (out.status === 'failed') failed += 1;
    }
    setBytes(null);
    setProgress(
      `${folder}: 문서 ${plan.files}개 — 새로 변환 ${todo.length - skipped - failed}, 그대로 ${plan.files - todo.length}` +
      `, 지움 ${plan.removed}${failed ? `, 변환 실패 ${failed}` : ''}${skipped ? `, 건너뜀 ${skipped}` : ''}`);
  });

  // 로그인·메일 읽기는 이 브라우저가 하고(lib/msgraph.ts), 서버에는 메일 내용만 보낸다.
  const syncMail = (clientId: string, tenant: string) => run(async () => {
    setProgress('Microsoft 로그인 창에서 로그인하세요...');
    const inbox = await fetchInbox(clientId, tenant);
    setProgress(`메일 ${inbox.messages.length}통을 올리는 중...`);
    const out = await api.mailSave(inbox.account, inbox.messages);
    setProgress(`메일 ${out.fetched}통 확인(${inbox.account}) — 새 메일 ${out.new}통`);
  });

  return (
    <div className="panel">
      <h2>내 업무 맥락</h2>
      <Async state={status}>
        {(s) => !s.consented ? (
          <>
            <p>
              내 PC의 문서 폴더와 아웃룩 메일을 대화의 근거로 씁니다. 서버는 <b>원본을 남기지
              않고</b> 추출한 텍스트·색인·온톨로지만 보관합니다. 사내 문서 저장소·온톨로지와{' '}
              <b>따로</b> 보관되고, <b>나만</b> 조회하고 대화에 쓸 수 있습니다(관리자도 볼 수
              없습니다). 동의를 철회하면 보관한 텍스트와 색인을 모두 지웁니다.
            </p>
            <button disabled={busy} onClick={() => void run(() => api.personalConsent())}>
              동의하고 시작
            </button>
          </>
        ) : (
          <>
            <p className="mutedtext" style={{ marginTop: 0 }}>
              동의 {when(s.consented_at)}
              {s.index && ` · 문서 ${s.index.total}개, 색인 ${s.index.indexed}`}
              {s.index && s.index.failed > 0 && `, 실패 ${s.index.failed}`}
            </p>

            <h3 style={{ fontSize: 14 }}>로컬 폴더</h3>
            <p className="mutedtext">
              폴더를 고르면 그 안의 문서(PDF·Office·한글·텍스트)만 올라가 텍스트·온톨로지로
              바뀌고, 원본은 변환 직후 서버에서 지워집니다. 같은 폴더를 다시 고르면 바뀐 파일만
              올리고, PC에서 지운 파일은 여기서도 지웁니다.
            </p>
            {s.folders.length > 0 && (
              <table style={{ marginBottom: 10 }}>
                <thead><tr><th>폴더</th><th>문서</th><th>동기화</th><th /></tr></thead>
                <tbody>
                  {s.folders.map((f) => (
                    <tr key={f.name}>
                      <td>{f.name}</td>
                      <td>{f.files}</td>
                      <td>{when(f.synced_at)}</td>
                      <td>
                        <button className="small secondary" disabled={busy} onClick={() => {
                          if (!window.confirm(`'${f.name}' 폴더의 텍스트와 색인을 지울까요? (PC의 원본은 그대로입니다)`)) return;
                          void run(() => api.personalRemoveFolder(f.name));
                        }}>제거</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <input
              type="file"
              multiple
              style={{ display: 'none' }}
              ref={(el) => {
                picker.current = el;
                if (el) el.setAttribute('webkitdirectory', '');
              }}
              onChange={(e) => {
                if (e.target.files) void syncFolder(e.target.files);
                e.target.value = '';  // 같은 폴더를 다시 골라도 onChange가 오게
              }}
            />
            <button disabled={busy} onClick={() => picker.current?.click()}>폴더 선택(추가·다시 동기화)</button>

            <h3 style={{ fontSize: 14, marginTop: 18 }}>아웃룩 메일</h3>
            {!s.mail.configured ? (
              <p className="mutedtext">
                <span className="status dim">미설정</span> 관리자가 Entra ID 앱을 등록하고(SPA
                리디렉션 URI <span className="mono">{redirectUri()}</span>)
                PAAS_MS_GRAPH_CLIENT_ID를 지정하면 쓸 수 있습니다.
              </p>
            ) : (
              <>
                <p className="mutedtext">
                  로그인과 메일 읽기는 이 브라우저에서 합니다 — 서버는 메일 토큰을 받지 않고,
                  받은편지함 최근 메일의 내용만 보관합니다. 로그인 창(팝업)이 뜹니다.
                </p>
                <div className="row">
                  {s.mail.connected && (
                    <>
                      <span className="status ok">가져옴</span>
                      <span>{s.mail.account}</span>
                      <span className="mutedtext">마지막 동기화 {when(s.mail.synced_at)}</span>
                    </>
                  )}
                  <button className="small" disabled={busy}
                    onClick={() => void syncMail(s.mail.client_id, s.mail.tenant)}>
                    {s.mail.connected ? '동기화' : '메일 가져오기'}
                  </button>
                  {s.mail.connected && (
                    <button className="small secondary" disabled={busy} onClick={() => {
                      if (!window.confirm('받아 둔 메일 사본을 지울까요?')) return;
                      void run(() => api.mailDisconnect());
                    }}>메일 지우기</button>
                  )}
                </div>
              </>
            )}

            <div className="row" style={{ marginTop: 18 }}>
              <span className="spacer" />
              <button className="small secondary" disabled={busy} onClick={() => {
                if (!window.confirm('동의를 철회할까요? 보관한 문서·메일 텍스트와 색인이 모두 지워집니다.')) return;
                void run(() => api.personalRevoke());
              }}>동의 철회</button>
            </div>
          </>
        )}
      </Async>
      {bytes && bytes.total > 0 && (
        <div className="progress-bar" title={`${mb(bytes.sent)} / ${mb(bytes.total)}`}>
          <div style={{ width: `${(bytes.sent / bytes.total) * 100}%` }} />
        </div>
      )}
      {progress && <p className="mutedtext">{progress}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function mb(n: number): string {
  return `${(n / (1024 * 1024)).toFixed(1)}MB`;
}
