import { useEffect, useRef, useState } from 'react';
import Async from '../components/Async';
import Split from '../components/Split';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';
import { parseCsv, renderMarkdown } from '../lib/markdown';
import { fetchInbox, redirectUri } from '../lib/msgraph';
import type {
  HealthInfo,
  SmartworkAgent,
  SmartworkMessage,
  SmartworkReport,
  SmartworkSuggestion,
} from '../lib/types';

/**
 * 스마트워크 — 왼쪽 대화, 오른쪽 화면(대시보드·에이전트·보고서).
 *
 * 오른쪽에 무엇을 띄울지는 서버가 정한다(모델이 show_agent·show_report 도구로 고른다).
 * 화면은 그 결과만 따른다 — 에이전트가 오면 보고서보다 **먼저** 에이전트를 연다.
 * 대화는 빈 화면이 아니라 **제안으로 시작한다**: 들어오자마자 빈 대화로 한 턴을 돌려,
 * 부서 워크플로와 개인 업무 맥락을 본 모델이 할 일을 버튼으로 띄운다.
 */

type View = 'dashboard' | 'agent' | 'report';
type Shown = SmartworkMessage & { tools?: string[] };

export default function Smartwork() {
  const health = useApi(() => api.health());
  const agents = useApi(() => api.smartworkAgents());
  const workflows = useApi(() => api.smartworkWorkflows());
  const [messages, setMessages] = useState<Shown[]>([]);
  const [suggestions, setSuggestions] = useState<SmartworkSuggestion[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [agent, setAgent] = useState<SmartworkAgent | null>(null);
  const [report, setReport] = useState<SmartworkReport | null>(null);
  const [view, setView] = useState<View>('dashboard');
  const [collapsed, setCollapsed] = useState(false);
  const opened = useRef(false);

  const send = async (history: Shown[]) => {
    setMessages(history);
    setSuggestions([]);
    setBusy(true);
    setError(null);
    try {
      const out = await api.smartworkChat(history.map(({ role, content }) => ({ role, content })));
      setMessages([...history, { role: 'assistant', content: out.reply, tools: out.tools }]);
      setSuggestions(out.suggestions);
      if (out.report) {
        setReport(out.report);
        setView('report');
      }
      if (out.agent) {
        setAgent(out.agent);
        setView('agent');
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    // StrictMode가 개발 모드에서 effect를 두 번 부른다 — 여는 턴이 두 번 돌면 LLM을 두 번 부른다.
    if (opened.current) return;
    opened.current = true;
    void send([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submit = () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput('');
    void send([...messages, { role: 'user', content: text }]);
  };

  // 실패한 턴은 보낸 메시지까지 남겨 두었으니 같은 대화로 다시 보내면 된다.
  const canRetry = !busy && error && (messages.length === 0 || messages[messages.length - 1].role === 'user');

  const left = (
    <div className="panel">
      <h2>대화</h2>
      <div className="chat-thread">
        {messages.length === 0 && busy && (
          <p className="mutedtext">부서 워크플로와 업무 맥락을 살펴보고 진행할 업무를 고르는 중...</p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`chat-msg ${m.role}`}>
            {m.content}
            {m.tools && m.tools.length > 0 && (
              <div className="mutedtext mono" style={{ fontSize: 11, marginTop: 6 }}>
                도구: {m.tools.join(', ')}
              </div>
            )}
          </div>
        ))}
        {messages.length > 0 && busy && <p className="mutedtext">답변을 만드는 중...</p>}
      </div>
      {suggestions.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
          {suggestions.map((s, i) => (
            <button
              key={i}
              className="secondary"
              style={{ textAlign: 'left' }}
              title={s.prompt}
              disabled={busy}
              onClick={() => void send([...messages, { role: 'user', content: s.prompt }])}
            >
              {s.title}
              {(s.workflow || s.why) && (
                <div className="mutedtext" style={{ fontSize: 12 }}>
                  {[s.workflow && `워크플로: ${s.workflow}`, s.why].filter(Boolean).join(' · ')}
                </div>
              )}
            </button>
          ))}
        </div>
      )}
      {error && (
        <div className="row" style={{ marginBottom: 10 }}>
          <p className="error" style={{ margin: 0 }}>{error}</p>
          {canRetry && (
            <button className="small secondary" onClick={() => void send(messages)}>다시 시도</button>
          )}
        </div>
      )}
      <textarea
        rows={3}
        value={input}
        placeholder="무엇을 할까요? (Enter 보내기, Shift+Enter 줄바꿈)"
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <div className="row" style={{ marginTop: 8 }}>
        <button onClick={submit} disabled={busy || !input.trim()}>보내기</button>
        <span className="spacer" />
        <button
          className="small secondary"
          disabled={busy}
          onClick={() => {
            setAgent(null);
            setReport(null);
            setView('dashboard');
            void send([]);
          }}
        >
          새 대화
        </button>
      </div>
    </div>
  );

  const tabs: { key: View; label: string }[] = [
    { key: 'dashboard', label: '대시보드' },
    ...(agent ? [{ key: 'agent' as const, label: `에이전트 · ${agent.name}` }] : []),
    ...(report ? [{ key: 'report' as const, label: `보고서 · ${report.title}` }] : []),
  ];

  const right = (
    <>
      <div className="row viewtabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={view === t.key}
            className={view === t.key ? 'small' : 'small secondary'}
            onClick={() => setView(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      {view === 'agent' && agent ? (
        <AgentView agent={agent} health={health.data} />
      ) : view === 'report' && report ? (
        <ReportView report={report} />
      ) : (
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
          <PersonalPanel />
        </>
      )}
    </>
  );

  return (
    <Split left={left} right={right} leftLabel="대화" leftWidth={440}
      collapsed={collapsed} onToggle={setCollapsed} />
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
// 한 번에 올리는 묶음. 서버가 요청 안에서 변환을 끝내므로(원본을 남기지 않는다) 묶음이
// 크면 OCR·구형 Office 변환 시간이 쌓여 프록시 시간 제한에 걸린다.
const UPLOAD_BATCH_FILES = 5;
const UPLOAD_BATCH_BYTES = 10 * 1024 * 1024;

function isDocument(name: string): boolean {
  const lower = name.toLowerCase();
  return DOC_SUFFIXES.some((s) => lower.endsWith(s));
}

function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : '-';
}

function PersonalPanel() {
  const status = useApi(() => api.personalStatus());
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
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
    let done = 0;
    let skipped = 0;
    let failed = 0;
    while (done < todo.length) {
      const batch: typeof todo = [];
      let bytes = 0;
      for (const it of todo.slice(done)) {
        if (batch.length >= UPLOAD_BATCH_FILES || (batch.length > 0 && bytes + it.file.size > UPLOAD_BATCH_BYTES)) break;
        batch.push(it);
        bytes += it.file.size;
      }
      setProgress(`${folder}: ${done}/${todo.length} 변환 중...`);
      const out = await api.personalUpload(folder, batch);
      skipped += out.skipped.length;
      failed += out.failed.length;
      done += batch.length;
    }
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
      {progress && <p className="mutedtext">{progress}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
