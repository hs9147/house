import { useEffect, useRef, useState } from 'react';
import Async from '../components/Async';
import Split from '../components/Split';
import { api } from '../lib/api';
import { getEmail } from '../lib/auth';
import { copyText } from '../lib/clipboard';
import { type AsyncState, useApi, usePolling } from '../lib/hooks';
import { canKeepFolders, forget, pickFolder, type PickedFolder, remember, reopenFolder } from '../lib/localfiles';
import { cancelTask, dismissTask, runTask, type TaskHandle, useTask } from '../lib/tasks';
import { parseCsv, renderMarkdown } from '../lib/markdown';
import type {
  HealthInfo,
  MailLogin,
  PersonalStatus,
  SmartworkAgent,
  SmartworkAttachmentIn,
  SmartworkContext,
  SmartworkFile,
  SmartworkOrgChoice,
  SmartworkReport,
  SmartworkSession,
  SmartworkSessionMessage,
  SmartworkSessionSummary,
  SmartworkWeb,
} from '../lib/types';
import { FileView, WebView } from './smartwork/viewers';

/**
 * 스마트워크 — 왼쪽 대화·설정(에이전트·부서 워크플로·내 업무 맥락), 오른쪽 화면(에이전트·보고서).
 *
 * 대화는 세션 단위다 — **세션 하나 = 업무 하나.** 소유자가 그 업무의 조직·워크플로를
 * 대화창에서 고르고, 필요하면 다른 사람(다른 부서여도)과 공유한다. 공유받은 사람은 읽고
 * 말할 수 있고, 개인 맥락(폴더·메일)은 각자 자기 것만 이 업무에 고른다.
 *
 * 오른쪽에 무엇을 띄울지는 서버가 정한다(모델이 show_agent·show_report 도구로 고른다).
 * 화면은 그 결과만 따른다 — 에이전트가 오면 보고서보다 **먼저** 에이전트를 연다.
 * 새 업무는 빈 화면이 아니라 **제안으로 시작한다**: 입력란에 기본 프롬프트가 음영으로 떠 있고, 그대로
 * 보내면 빈 내용으로 한 턴을 돌려 부서 워크플로와 개인 업무 맥락을 본 모델이 할 일을 버튼으로 띄운다.
 * 저절로 돌리지 않는다 — 맥락을 고르거나 직접 물을 틈을 준다. 글자를 치면 음영은 바로 사라진다(placeholder).
 */

type View = 'agent' | 'report' | 'web' | 'file';
// 기본 프롬프트를 사람이 읽는 말로 — 보내면 빈 내용으로 가고 서버가 여는 지시(OPENING_PROMPT)로 돌린다.
const OPENING_HINT = '부서 워크플로와 내 업무 맥락(최근 메일·문서)에서 진행할 업무를 찾아 다음 할 일을 제안해 줘';
// 공유 세션은 다른 참여자가 보낸 말을 이 간격으로 다시 읽는다.
const SHARED_POLL_MS = 5000;
// 답을 기다리는 동안 진행 과정을 읽는 간격.
const STEP_POLL_MS = 1500;

function sessionLabel(s: SmartworkSessionSummary): string {
  const task = s.workflow ?? s.organization;
  return `${s.title || '(새 업무)'}${task ? ` · ${task}` : ''}${s.members > 1 ? ` · 공유 ${s.members}명` : ''}`;
}

export default function Smartwork() {
  const health = useApi(() => api.health());
  const agents = useApi(() => api.smartworkAgents());
  const workflows = useApi(() => api.smartworkWorkflows());
  const personal = useApi(() => api.personalStatus());
  const providers = useApi(() => api.listProviders());
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
  // 이번 대화를 돌릴 모델 — 0 = 기본 프로바이더(서버가 고른다). 다시 시도에도 그대로 실린다.
  const [providerId, setProviderId] = useState(0);
  const [busyHere, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sharing, setSharing] = useState(false);
  const [agent, setAgent] = useState<SmartworkAgent | null>(null);
  const [report, setReport] = useState<SmartworkReport | null>(null);
  const [web, setWeb] = useState<SmartworkWeb | null>(null);
  const [file, setFile] = useState<SmartworkFile | null>(null);
  const [view, setView] = useState<View>('agent');
  const [leftTab, setLeftTab] = useState<'chat' | 'settings'>('chat');
  const opened = useRef(false);
  // 폴링(usePolling)은 처음 받은 함수를 계속 부른다 — 지금 세션은 ref로 읽는다.
  const current = useRef<SmartworkSession | null>(null);
  current.current = session;
  // 답을 기다리는 턴은 화면 밖(lib/tasks)에 있다 — 다른 메뉴에 다녀와도 그 턴이 아직 도는지
  // 알아야 같은 말을 다시 보내지 않는다. busy는 이 화면이 보낸 것과 그 턴 둘 다다.
  const chatKey = session ? `chat:${session.id}` : '';
  const chatTask = useTask(chatKey);
  const chatRunning = chatTask?.state === 'running';
  const busy = busyHere || chatRunning;
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  // 답을 만드는 동안 거친 단계 — 기다리는 동안 짧은 간격으로 읽어 대화에 차례로 보인다(빈 화면에
  // "답변을 만드는 중"만 수십 초 떠 있으면 멈춘 줄 안다). 답이 오면 그 답의 「진행 과정」으로 남는다.
  const [liveSteps, setLiveSteps] = useState<string[]>([]);
  usePolling(async () => {
    const s = current.current;
    if (!s) return;
    const p = await api.sessionProgress(s.id);
    if (current.current?.id === s.id) setLiveSteps(p.steps.map((x) => x.label));
  }, STEP_POLL_MS, chatRunning && !!session);
  useEffect(() => {
    if (!chatRunning) setLiveSteps([]);
  }, [chatRunning]);

  const open = async (id: number) => {
    setError(null);
    setPending(null);
    setCreating(false);
    setSharing(false);
    try {
      const s = await api.getSession(id);
      setSession(s);
      // 연 업무의 화면은 그 대화가 마지막으로 띄운 에이전트·보고서·웹·파일이다(종류마다 가장 최근 것).
      const latest = <K extends 'agent' | 'report' | 'web' | 'file'>(k: K) =>
        [...s.messages].reverse().find((m) => m[k])?.[k] ?? null;
      setAgent(latest('agent'));
      setReport(latest('report'));
      setWeb(latest('web'));
      setFile(latest('file'));
      const last = [...s.messages].reverse().find((m) => m.agent || m.report || m.web || m.file);
      setView(last?.agent ? 'agent' : last?.file ? 'file' : last?.web ? 'web' : 'report');
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

  // 다른 화면에 가 있는 사이에 끝난 턴 — 서버는 답을 남겼으니 읽어 붙이고 치운다.
  // 이 화면이 보낸 턴(busyHere)은 send가 직접 마무리한다.
  useEffect(() => {
    if (!chatTask || chatTask.state === 'running' || busyHere) return;
    if (chatTask.state === 'failed') setError(chatTask.error ?? '답변을 받지 못했습니다.');
    else void catchUp();
    dismissTask(chatTask.key);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatTask?.state, busyHere]);

  const send = async (s: SmartworkSession, content: string, attachments: SmartworkAttachmentIn[] = []) => {
    setPending(content);
    setBusy(true);
    setError(null);
    try {
      const out = await runTask(`chat:${s.id}`, { label: '스마트워크 답변', to: '/smartwork' },
        (h) => {
          h.set({ progress: content ? '답변을 만드는 중...' : '업무를 살펴보고 제안을 고르는 중...' });
          return api.sendSessionMessage(s.id, content, attachments, chosenProvider || null);
        });
      // 화면을 떠났다면 끝난 표시를 트레이에 남긴다(돌아오면 위의 effect가 읽어 붙이고 치운다).
      if (!mounted.current) return;
      dismissTask(`chat:${s.id}`);
      await catchUp();
      setPending(null);
      if (attachments.length > 0) setFiles([]);
      if (out.report) {
        setReport(out.report);
        setView('report');
      }
      if (out.web) {
        setWeb(out.web);
        setView('web');
      }
      if (out.file) {
        setFile(out.file);
        setView('file');
      }
      if (out.agent) {
        setAgent(out.agent);
        setView('agent');
      }
      sessions.reload();  // 첫 요청이 제목이 된다
    } catch (e) {
      if (mounted.current) {
        dismissTask(`chat:${s.id}`);
        setError((e as Error).message);
      }
    } finally {
      setBusy(false);
    }
  };

  const start = async (orgId: number | null, workflowId: number | null,
                       context: SmartworkContext) => {
    setBusy(true);
    setError(null);
    let s: SmartworkSession;
    try {
      s = await api.createSession({ organization_id: orgId, workflow_id: workflowId });
      // 맥락은 **여는 턴 전에** 저장한다 — 첫 제안은 최근 메일·문서에서 진행할 업무 단위를
      // 찾아 내놓는 것이어서, 나중에 고르면 그 첫 제안이 "대상을 못 찾았다"로 끝난다.
      if (context.folders.length > 0 || context.mail) {
        s = { ...s, my_context: await api.setSessionContext(s.id, context) };
      }
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
    setWeb(null);
    setFile(null);
    sessions.reload();
    // 여는 턴은 저절로 돌리지 않는다 — 입력란의 기본 프롬프트를 보내기로 돌린다.
    setBusy(false);
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
    setWeb(null);
    setFile(null);
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

  // 공유받은 사람의 「삭제」 자리 — 대화는 소유자의 것이라 지우지 못하고, 내 목록에서만 빠진다.
  const quit = async () => {
    if (!session || !window.confirm('이 업무에서 나갈까요? 내 목록에서만 빠지고, 다른 참여자의 대화는 그대로입니다.')) return;
    try {
      await api.removeSessionMember(session.id, me);
      leave();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // 전역 프로바이더와 이 업무의 조직 것만 — 서버도 같은 규칙으로 막는다.
  const usable = (providers.data ?? []).filter(
    (p) => p.organization_id == null || p.organization_id === session?.organization_id);
  // 다른 조직의 업무로 옮겨 고른 모델을 쓸 수 없게 되면 기본으로 돌아간다.
  const chosenProvider = usable.some((p) => p.id === providerId) ? providerId : 0;
  const submit = () => {
    const text = input.trim();
    if (busy || !session) return;
    if (!text) {
      if (opening && files.length === 0) void send(session, '');
      return;
    }
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
  // 아직 아무 말도 없는 업무 — 입력란에 기본 프롬프트를 음영으로 보이고, 비운 채 보내면 그것으로 시작한다.
  const opening = !!session && messages.length === 0 && pending === null && !chatRunning;

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
        {/* 업무를 고르는 줄에 둔다 — 공유 버튼 옆에 두었더니 있는 줄을 몰랐다. */}
        {session && (session.is_owner ? (
          <button className="small secondary" disabled={busy} onClick={() => void remove()}>삭제</button>
        ) : (
          <button className="small secondary" disabled={busy} onClick={() => void quit()}>나가기</button>
        ))}
      </div>

      {creating && !session && (
        <NewSession orgs={orgs.data ?? []} personal={personal.data} busy={busy}
          onStart={(o, w, ctx) => void start(o, w, ctx)} />
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
            {liveSteps.map((label, i) => (
              <div key={i} className="chat-msg assistant chat-step">{label}</div>
            ))}
            {chatRunning && !busyHere && (
              <p className="mutedtext">답변을 만드는 중... (다른 화면에 다녀온 사이에도 이어집니다)</p>
            )}
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
            placeholder={opening
              ? `${OPENING_HINT} — 보내기를 누르면 이대로 시작, 직접 물으려면 입력하세요`
              : '무엇을 할까요? (Enter 보내기, Shift+Enter 줄바꿈)'}
            style={opening && !input ? { background: 'var(--bg-subtle, rgba(148, 163, 184, 0.12))' } : undefined}
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
            <button onClick={submit} disabled={busy || (!input.trim() && !(opening && files.length === 0))}>보내기</button>
            <select value={chosenProvider} disabled={busy} title="이 대화에 쓸 LLM"
              onChange={(e) => setProviderId(Number(e.target.value))}>
              <option value={0}>LLM: 기본</option>
              {usable.map((p) => (
                <option key={p.id} value={p.id}>{p.name} · {p.model}{p.is_default ? ' (기본)' : ''}</option>
              ))}
            </select>
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
              <thead><tr><th>이름</th><th>용도</th><th>조직</th><th /></tr></thead>
              <tbody>
                {list.map((a) => (
                  <tr key={a.name}>
                    <td>{a.name}</td>
                    {/* 대화가 이 앱을 고르는 근거와 같은 글이다 — 빗나갔으면 프로젝트 개요에서 다시 검토. */}
                    <td style={{ fontSize: 12 }} title={a.does_not?.length ? `하지 않는 일: ${a.does_not.join(' · ')}` : undefined}>
                      {a.purpose || <span className="mutedtext">검토 전</span>}
                    </td>
                    <td>{a.org ?? '공용'}</td>
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
    ...(web ? [{ key: 'web' as const, label: `웹 · ${web.url ? hostOf(web.url) : '새 주소'}` }] : []),
    ...(session && file ? [{ key: 'file' as const, label: `파일 · ${file.name}` }] : []),
  ];
  // 고른 탭이 비었으면 남은 쪽을 보인다 — 에이전트를 먼저.
  const shown: View | null = tabs.some((t) => t.key === view) ? view : tabs[0]?.key ?? null;
  const close = (key: View) => {
    if (key === 'agent') setAgent(null);
    if (key === 'report') setReport(null);
    if (key === 'web') setWeb(null);
    if (key === 'file') setFile(null);
  };

  const right = (
    <>
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
              onClick={() => close(t.key)}>
              ×
            </button>
          </span>
        ))}
      </div>
      {shown === 'agent' && agent ? (
        <AgentView agent={agent} health={health.data} />
      ) : shown === 'report' && report ? (
        <ReportView report={report} />
      ) : shown === 'web' && web ? (
        <WebView web={web} />
      ) : shown === 'file' && file ? (
        <FileView key={`${file.source}:${file.folder}/${file.path ?? file.name}`} file={file} />
      ) : (
        <div className="panel">
          <p className="mutedtext">
            대화에서 에이전트·보고서·웹 페이지·파일을 띄우면 여기에 나옵니다. 에이전트·부서
            워크플로·내 업무 맥락은 왼쪽 「설정」에 있습니다.
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
      {m.role === 'assistant' && m.steps && m.steps.length > 0 && (
        <details className="chat-steps">
          <summary>진행 과정 {m.steps.length}단계</summary>
          <ol>{m.steps.map((label, i) => <li key={i}>{label}</li>)}</ol>
        </details>
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
      {m.role === 'assistant' && <Rating message={m} />}
    </div>
  );
}

/**
 * 답 하나에 좋음·아쉬움 — **품질을 재는 유일한 입력이다.**
 *
 * 지연과 토큰은 서버가 저절로 세지만(작업 로그 · 대시보드), 그 답이 도움이 됐는지는
 * 누른 사람만 안다. 모델에게 자기 답을 채점하게 하면 측정이 아니라 자기 보고가 된다.
 *
 * 아쉬움은 누르는 **즉시** 보낸다. 사유를 적어야 점수가 들어가게 하면 대부분 안 적고
 * 떠나므로 아무것도 안 남는다 — 점수를 먼저 남기고, 사유는 덮어쓰기로 더한다.
 */
function Rating({ message: m }: { message: SmartworkSessionMessage }) {
  const [score, setScore] = useState(m.my_rating ?? 0);
  const [reason, setReason] = useState('');
  // 보낸 사유 — 점수는 「아쉬움」을 누를 때 이미 바뀌어 있어서, 이것이 없으면 사유를 보내도 화면이 그대로다.
  const [sent, setSent] = useState('');
  const [sending, setSending] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);

  const send = async (next: number, text: string) => {
    setFailed(null);
    setScore(next);  // 눌린 것은 바로 보인다 — 왕복을 기다리면 두 번 누른다
    setSending(true);
    try {
      const out = await api.rateAnswer(m.id, next, text);
      setSent(out.reason);
    } catch (e) {
      setScore(m.my_rating ?? 0);
      setFailed((e as Error).message);
    } finally {
      setSending(false);
    }
  };
  const draft = reason.trim();

  return (
    <div style={{ marginTop: 8 }}>
      <div className="row" style={{ gap: 6, fontSize: 11 }}>
        <button className={`small ${score > 0 ? '' : 'secondary'}`}
          onClick={() => send(1, '')}>좋음</button>
        <button className={`small ${score < 0 ? '' : 'secondary'}`}
          onClick={() => send(-1, reason)}>아쉬움</button>
        {score !== 0 && <span className="mutedtext">평가해 주셔서 고맙습니다</span>}
      </div>
      {score < 0 && (
        <div className="row" style={{ gap: 6, marginTop: 6 }}>
          <input value={reason} placeholder="무엇이 아쉬웠나요? (선택)"
            style={{ flex: 1 }}
            onChange={(e) => setReason(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && draft && draft !== sent && !sending && send(-1, reason)} />
          <button className="secondary small" disabled={!draft || draft === sent || sending}
            onClick={() => send(-1, reason)}>{sending ? '보내는 중…' : draft && draft === sent ? '보냄' : '보내기'}</button>
        </div>
      )}
      {score < 0 && sent && (
        <div className="mutedtext" style={{ fontSize: 11, marginTop: 4 }}>
          사유를 보냈습니다 — 관리자의 평가 탭에 남습니다. 고쳐 쓰면 다시 보낼 수 있습니다.
        </div>
      )}
      {failed && <div className="mutedtext" style={{ fontSize: 11 }}>평가 실패: {failed}</div>}
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

/**
 * 새 업무 — 조직·워크플로와 **내 업무 맥락을 함께** 고르고 시작한다.
 *
 * 맥락을 나중에 고를 수 있게만 두었더니 첫 제안이 늘 헛돌았다: 여는 턴은 최근 메일·문서에서
 * 진행할 업무 단위(과제·계약·구매요청 한 건)를 찾는 일인데, 그때 맥락이 없으면 찾을 것이
 * 없어 "대상을 못 찾았다"로 끝난다. 그래서 시작 전에 묻는다.
 */
function NewSession({ orgs, personal, busy, onStart }: {
  orgs: SmartworkOrgChoice[];
  personal: PersonalStatus | null;
  busy: boolean;
  onStart: (orgId: number | null, workflowId: number | null, context: SmartworkContext) => void;
}) {
  const [orgId, setOrgId] = useState<number | null>(null);
  const [workflowId, setWorkflowId] = useState<number | null>(null);
  const [context, setContext] = useState<SmartworkContext>({ folders: [], mail: false });
  const folders = personal?.consented ? personal.folders : [];
  const mailReady = !!personal?.consented && personal.mail.connected;
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
      {(folders.length > 0 || mailReady) && (
        <>
          <p className="mutedtext" style={{ fontSize: 12, margin: '10px 0 4px' }}>
            이 업무에 쓸 내 맥락(나만 씀) — 고르면 최근 메일·문서에서 진행할 건을 찾아
            제안합니다.
          </p>
          <div className="row" style={{ flexWrap: 'wrap', fontSize: 12 }}>
            {folders.map((f) => (
              <label key={f.name}>
                <input type="checkbox" disabled={busy} checked={context.folders.includes(f.name)}
                  onChange={(e) => setContext((c) => ({
                    ...c,
                    folders: e.target.checked ? [...c.folders, f.name]
                      : c.folders.filter((x) => x !== f.name),
                  }))} /> {f.name}
              </label>
            ))}
            {mailReady && (
              <label>
                <input type="checkbox" disabled={busy} checked={context.mail}
                  onChange={(e) => setContext((c) => ({ ...c, mail: e.target.checked }))} /> 메일
              </label>
            )}
          </div>
        </>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        <button disabled={busy} onClick={() => onStart(orgId, workflowId, context)}>시작</button>
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
  '.txt', '.md', '.csv', '.html', '.htm', '.json', '.eml'];
function isDocument(name: string): boolean {
  const lower = name.toLowerCase();
  return DOC_SUFFIXES.some((s) => lower.endsWith(s));
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : '-';
}

// 상태는 대화 쪽(이 업무에 쓸 내 맥락 고르기)과 같은 것을 본다 — 폴더를 올리면 거기에도 바로 보인다.
// 동기화·메일 로그인은 화면 밖에서 돈다(lib/tasks) — 대화/설정 탭을 바꾸거나 다른 메뉴로 가도
// 이어지고, 돌아오면 진행이 그대로 보인다. 폴더 동기화는 한 번에 하나다(같은 폴더를 두 루프가
// 함께 올리지 않게). 창을 닫아 끊겨도 다시 고르면 남은 파일만 올라간다(manifest가 비교한다).
const FOLDER_TASK = 'personal:folders';
const MAIL_TASK = 'personal:mail';
const TASK_SCREEN = '/smartwork';

function PersonalPanel({ status }: { status: AsyncState<PersonalStatus> }) {
  const [busyHere, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const folderTask = useTask(FOLDER_TASK);
  const mailTask = useTask(MAIL_TASK);
  const busy = busyHere || folderTask?.state === 'running' || mailTask?.state === 'running';
  // 진행·결과·실패 사유는 작업 트레이(components/TaskTray)가 보인다 — 여기서 또 그리지 않는다.
  // 메일 로그인의 주소·코드만은 여기 둔다: 트레이에 없고, 승인하려면 보여야 한다.
  const mailLogin = mailTask?.state === 'running' ? (mailTask.data as MailLogin | undefined) ?? null : null;
  const picker = useRef<HTMLInputElement | null>(null);
  // 행의 「동기화」가 폴더를 다시 고르게 됐을 때 그 폴더 이름 — 전에 고른 폴더를 다시 읽을 수
  // 없을 때만 고른다(lib/localfiles.reopenFolder: 평문 http·다른 브라우저·폴더가 없어졌을 때).
  // 다른 폴더를 고르면 그 행을 고치려던 뜻과 달라지므로 그 자리에서 멈춘다.
  const resyncTarget = useRef<string | null>(null);
  const [codeCopied, setCodeCopied] = useState(false);

  // 화면 밖에서 끝난 작업도 상태(폴더 목록·메일 연결)에 반영한다 — 돌아왔을 때 다시 읽는다.
  useEffect(() => {
    if (folderTask && folderTask.state !== 'running') status.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [folderTask?.state]);
  useEffect(() => {
    if (mailTask && mailTask.state !== 'running') status.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mailTask?.state]);

  // 짧은 일(동의·폴더 제거·메일 지우기)은 이 화면에서 기다린다.
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      status.reload();
    }
  };

  // 긴 일(동기화·메일 로그인)은 화면 밖에 건다 — 실패는 작업에 남고 진행 표시로 보인다.
  const background = (key: string, label: string, fn: (h: TaskHandle) => Promise<void>) => {
    setError(null);
    dismissTask(FOLDER_TASK);
    dismissTask(MAIL_TASK);
    runTask(key, { label, to: TASK_SCREEN }, fn).catch(() => { /* 사유는 작업에 남는다 */ });
  };

  const startSync = (picked: PickedFolder, target: string | null) => {
    if (target && picked.folder !== target) {
      setError(`'${target}'를 다시 동기화하려는데 '${picked.folder}'를 골랐습니다 — 같은 폴더를 고르세요.`);
      return;
    }
    background(FOLDER_TASK, `폴더 동기화 · ${picked.folder}`, (h) => uploadFolder(picked, h));
  };

  // 평문 http에서 고른 폴더(input webkitdirectory) — 이 브라우저는 폴더를 다시 읽을 수 없다.
  const syncPicked = (files: FileList) => {
    const all = Array.from(files);
    if (all.length === 0) return;
    // webkitRelativePath = "고른폴더/하위/파일" — 첫 마디가 폴더 이름이다.
    const folder = all[0].webkitRelativePath.split('/')[0];
    const target = resyncTarget.current;
    resyncTarget.current = null;
    const entries = all.map((f) => ({ file: f, path: f.webkitRelativePath.split('/').slice(1).join('/') }));
    startSync({ folder, read: async () => entries }, target);
  };

  // 폴더를 고른다 — 고를 수 있는 브라우저면 핸들을 남겨 다음에는 다시 고르지 않는다.
  const choose = async (target: string | null) => {
    if (!canKeepFolders) {
      resyncTarget.current = target;
      picker.current?.click();
      return;
    }
    try {
      const picked = await pickFolder();
      if (picked) startSync(picked, target);
    } catch (e) {
      // 권한을 묻느라 버튼을 누른 동작이 지나가면 브라우저가 고르기 창을 막는다 — 한 번 더 누르면 된다.
      setError(`폴더 고르기를 열지 못했습니다 — 한 번 더 누르세요. (${(e as Error).message})`);
    }
  };

  // 행의 「동기화」 — 전에 고른 폴더를 그대로 다시 읽는다. 없어졌을 때만 고른다.
  const resync = async (folder: string) => {
    setError(null);
    const again = await reopenFolder(folder);
    if (again) startSync(again, folder);
    else await choose(folder);
  };

  const uploadFolder = async (picked: PickedFolder, h: TaskHandle) => {
    const { folder } = picked;
    const setProgress = (progress: string) => h.set({ progress });
    const setBytes = (b: { sent: number; total: number } | null) =>
      h.set({ sent: b?.sent, total: b?.total });
    setProgress(`${folder}: 폴더를 읽는 중...`);
    const items = (await picked.read())
      .filter((it) => isDocument(it.file.name) && it.file.size > 0)
      .map((it) => ({ ...it, mtime: it.file.lastModified / 1000 }));
    // 고른 폴더의 파일을 이 브라우저에 남긴다 — 파일 뷰어가 폴더를 다시 고르게 하지 않고 연다.
    void remember(folder, items);
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
  };

  // 서버가 Device Code 로그인을 시작하면 사람은 아래 주소에서 코드를 넣고 승인한다. 승인이
  // 끝나면 서버가 그 자리에서 메일을 받아 보관한다(토큰은 저장하지 않는다).
  // 다른 탭에 가 있으면 브라우저가 타이머를 늦춘다(오래 가려져 있으면 1분에 한 번) — 확인이
  // 늦어질 뿐 멈추지는 않는다. 승인은 서버가 기다려 주지 않으므로 코드 만료 전에 끝내야 한다.
  const syncMail = () => background(MAIL_TASK, '메일 가져오기', async (h) => {
    h.set({ progress: '로그인 주소를 받는 중...' });
    const login = await api.mailLogin();
    setCodeCopied(false);
    h.set({ data: login, progress: '브라우저에서 코드를 넣고 승인하세요 — 승인하면 자동으로 이어집니다.' });
    const deadline = Date.now() + login.expires_in * 1000;
    while (!h.cancelled() && Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, login.interval * 1000));
      if (h.cancelled()) break;
      const out = await api.mailLoginPoll();
      if (out.status === 'pending') continue;
      h.set({ data: undefined, progress: `메일 ${out.fetched}통 확인(${out.account}) — 새 메일 ${out.new}통` });
      return;
    }
    h.set({ data: undefined, progress: h.cancelled() ? '메일 가져오기를 취소했습니다.' : '' });
    if (!h.cancelled()) throw new Error('승인 시간이 지났습니다 — 다시 시도하세요.');
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
              바뀌고, 원본은 변환 직후 서버에서 지워집니다. 다시 동기화하면 바뀐 파일만 올리고,
              PC에서 지운 파일은 여기서도 지웁니다. 아래 표의 「동기화」는 전에 고른 폴더를 그대로
              다시 읽고, 폴더가 없어졌을 때만 다시 고르게 합니다.
              {!canKeepFolders && ' (이 주소는 https가 아니라 브라우저가 폴더를 기억하지 못해, 동기화할 때마다 폴더를 고릅니다.)'}
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
                      <td style={{ whiteSpace: 'nowrap' }}>
                        <button className="small" disabled={busy}
                          onClick={() => void resync(f.name)}>동기화</button>{' '}
                        <button className="small secondary" disabled={busy} onClick={() => {
                          if (!window.confirm(`'${f.name}' 폴더의 텍스트와 색인을 지울까요? (PC의 원본은 그대로입니다)`)) return;
                          void run(async () => {
                            await api.personalRemoveFolder(f.name);
                            await forget(f.name);
                          });
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
                if (e.target.files) syncPicked(e.target.files);
                e.target.value = '';  // 같은 폴더를 다시 골라도 onChange가 오게
              }}
            />
            <button disabled={busy} onClick={() => void choose(null)}>폴더 선택(추가)</button>


            <h3 style={{ fontSize: 14, marginTop: 18 }}>아웃룩 메일</h3>
            {!s.mail.configured ? (
              <p className="mutedtext">
                <span className="status dim">미설정</span> 관리자가 Entra ID 앱을 등록하고
                (공용 클라이언트 흐름 허용) PAAS_MS_GRAPH_CLIENT_ID를 지정하면 쓸 수 있습니다.
                그 전에는 웹 Outlook에서 메일을 .eml로 내려받아 위 「폴더 선택」으로 올리세요 —
                문서와 같이 텍스트만 보관합니다.
              </p>
            ) : (
              <>
                <p className="mutedtext">
                  버튼을 누르면 Microsoft 로그인 주소와 코드가 나옵니다 — 이 브라우저에서 열어
                  코드를 넣고 승인하세요. 서버는 토큰을 저장하지 않고, 받은편지함 최근 메일의
                  내용만 보관합니다. 로그인이 막히면(조건부 액세스 등) 웹 Outlook에서 메일을
                  .eml로 내려받아 위 「폴더 선택」으로 올려도 됩니다 — 문서와 같이 텍스트만 보관합니다.
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
                    onClick={() => void syncMail()}>
                    {s.mail.connected ? '동기화' : '메일 가져오기'}
                  </button>
                  {s.mail.connected && (
                    <button className="small secondary" disabled={busy} onClick={() => {
                      if (!window.confirm('받아 둔 메일 사본을 지울까요?')) return;
                      void run(() => api.mailDisconnect());
                    }}>메일 지우기</button>
                  )}
                </div>
                {mailLogin && (
                  <div className="row" style={{ marginTop: 8, flexWrap: 'wrap', gap: 8 }}>
                    <a href={mailLogin.verification_url} target="_blank" rel="noopener noreferrer">
                      {mailLogin.verification_url}
                    </a>
                    <span>코드 <b className="mono">{mailLogin.user_code}</b></span>
                    <button className="small secondary" onClick={() => {
                      void copyText(mailLogin.user_code).then((ok) => {
                        if (ok) setCodeCopied(true);
                        else window.alert('복사하지 못했습니다 — 코드를 직접 선택해 복사하세요.');
                      });
                    }}>{codeCopied ? '복사됨' : '복사'}</button>
                    <span className="mutedtext">승인하면 자동으로 이어집니다.</span>
                    <button className="small secondary" onClick={() => cancelTask(MAIL_TASK)}>취소</button>
                  </div>
                )}
              </>
            )}

            <div className="row" style={{ marginTop: 18 }}>
              <span className="spacer" />
              <button className="small secondary" disabled={busy} onClick={() => {
                if (!window.confirm('동의를 철회할까요? 보관한 문서·메일 텍스트와 색인이 모두 지워집니다.')) return;
                void run(async () => {
                  await api.personalRevoke();
                  await forget();
                });
              }}>동의 철회</button>
            </div>
          </>
        )}
      </Async>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function mb(n: number): string {
  return `${(n / (1024 * 1024)).toFixed(1)}MB`;
}
