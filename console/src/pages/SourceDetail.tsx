import { useEffect, useId, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import Async from '../components/Async';
import GpaxBookmark from '../components/GpaxBookmark';
import Split from '../components/Split';
import Tabs from '../components/Tabs';
import { agentCode } from '../lib/agentCode';
import { api } from '../lib/api';
import { useApi, usePolling } from '../lib/hooks';
import type {
  SourceCapabilities, SourceCategory, SourceChange, SourceFile, SourceMenuItem, SourceOut, SourcePage,
  OrgOut, SourceQueryMethod, SourceQueryScreen, SourceSaveResult, SourceSaveTarget, SourceTarget, StorageStore,
} from '../lib/types';
import { kindLabel, SourceStatus } from './Sources';

const PAGE_KIND: Record<string, string> = {
  list: '목록', detail: '상세', form: '입력·검색', dashboard: '대시보드', doc: '문서',
  login: '로그인', other: '기타',
};

// services/infosource.CATEGORIES
const CATEGORY: Record<SourceCategory, string> = {
  pages: '조회 정보 정리', documents: '문서 파일', sheets: '표 파일', slides: '발표 파일',
};
const CATEGORY_ORDER: SourceCategory[] = ['pages', 'documents', 'sheets', 'slides'];

const CHANGE: Record<SourceChange, { label: string; cls: string }> = {
  new: { label: '새로', cls: 'info' }, changed: { label: '바뀜', cls: 'warn' }, same: { label: '그대로', cls: 'dim' },
};

function ChangeBadge({ value }: { value?: SourceChange }) {
  // 그대로인 것은 표시하지 않는다 — 바뀐 것만 눈에 띄게.
  if (!value || value === 'same') return null;
  return <span className={`status ${CHANGE[value].cls}`} style={{ marginLeft: 6 }}>{CHANGE[value].label}</span>;
}

/**
 * 정보 출처 상세 — 왼쪽은 사람이 할 일(스캔·저장 결정), 오른쪽은 스캔이 읽은 것.
 *
 * 저장 위치는 유형(조회 정보 정리·문서·표·발표 파일)마다 제안을 **고칠 수 있는 입력값**으로 채워
 * 둔다. 그대로 누르면 제안대로, 고치면 고친 대로 저장된다 — 새 저장소면 서버가 폴더를 만들고
 * 소속 부서를 지정해 DB에 등록한다. 같은 자리에 다시 저장하면 바뀐 것만 덮어쓴다.
 */
export default function SourceDetail() {
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const [row, setRow] = useState<SourceOut | null>(null);
  const [error, setError] = useState('');
  const caps = useApi(() => api.sourceCapabilities(), []);
  const first = useApi(() => api.getSource(id), [id]);

  const live = useLive(row, setRow);

  useEffect(() => { if (first.data) setRow(first.data); }, [first.data]);
  usePolling(async () => setRow(await api.getSource(id)), 2000, row?.status === 'scanning');

  const rescan = async () => {
    // 서버 스캔은 결과를 통째로 바꾼다 — SSO 사이트면 로그인 화면만 읽은 결과가 브라우저 스캔(찾은 길·조회 화면)을 덮는다.
    if (row?.scan?.via === 'browser' && !window.confirm(
      '지난 스캔은 브라우저(북마크)로 읽은 것입니다. 서버에서 스캔하면 그 결과(메뉴 탐색이 찾은 길, 조건을 고를 '
      + '화면)를 서버가 읽은 것으로 바꿉니다 — 로그인이 필요한 사이트면 로그인 화면만 남습니다. 계속할까요?')) return;
    setError('');
    try {
      setRow(await api.scanSource(id));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!row) {
    return (
      <Async state={first}>{() => null}</Async>
    );
  }

  const left = (
    <>
      <div className="panel">
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          <button className="small secondary" onClick={() => navigate('/sources')}>← 목록</button>
          <h2 style={{ margin: 0, flex: 1 }}>{row.name}</h2>
          <SourceStatus value={row.status} />
        </div>
        <p className="mono" style={{ fontSize: 11, wordBreak: 'break-all' }}>
          {kindLabel(row.kind)} · {row.url}
        </p>
        {row.note && <p className="mutedtext" style={{ fontSize: 12 }}>{row.note}</p>}
        <Means caps={caps.data} kind={row.kind} />
        {/* 웹사이트는 스캔이 ② 카드에 있다(브라우저 스캔이 주된 길, 서버 스캔은 보조). API·MCP는 여기서. */}
        {row.kind !== 'web' && (
          <>
            <div className="row" style={{ gap: 8, marginTop: 8 }}>
              <button className="small" disabled={row.status === 'scanning'} onClick={rescan}>
                {row.status === 'scanning' ? '스캔 중…' : row.scan ? '다시 스캔' : '스캔'}
              </button>
            </div>
            <ScanProgress id={row.id} running={row.status === 'scanning'} />
          </>
        )}
        {row.status === 'failed' && row.error && <p className="error">{row.error}</p>}
        {error && <p className="error">{error}</p>}
      </div>
      {row.kind === 'web' && <BookmarkPanel row={row} onChange={setRow} />}
      {row.kind === 'web' && <ScanPanel row={row} live={live} onServerScan={rescan} />}
      {row.kind === 'web' && <RecordPanel row={row} live={live} />}
      {row.kind === 'web' && <QueryPanel row={row} onChange={setRow} />}
      <StoredHeaders row={row} onChange={setRow} />
      {/* 유형별 제안 전에 스캔한 출처(proposal에 targets가 없다)는 다시 스캔해야 저장할 수 있다. */}
      {row.scan && row.proposal?.targets && row.status !== 'scanning' && (
        // 다시 스캔하면 제안이 바뀐다 — 입력값도 새 제안으로 다시 채운다.
        <SavePanel key={row.scanned_at ?? ''} row={row} onSaved={(r) => setRow(r.source)} />
      )}
    </>
  );

  return <Split left={left} right={<ScanView row={row} />} leftLabel="작업" />;
}

function Means({ caps, kind }: { caps: SourceCapabilities | null; kind: SourceOut['kind'] }) {
  if (!caps) return null;
  const parts =
    kind === 'web'
      ? [
          `HTTP로 같은 출처 안을 최대 ${caps.limits.pages}쪽·깊이 ${caps.limits.depth}까지(메뉴 먼저, robots.txt 준수)`,
          caps.browser
            ? `브라우저로 첫 화면과 메뉴 ${caps.limits.shots}곳을 렌더링·캡처(접근성 트리 포함)`
            : '브라우저(Playwright)가 없어 렌더링·캡처는 건너뜁니다',
        ]
      : kind === 'api'
        ? ['OpenAPI 문서를 찾아 엔드포인트를 읽고, 없으면 응답의 모양(키·타입)만 봅니다']
        : ['tools/list로 도구 목록을 읽습니다(도구를 호출하지는 않습니다)'];
  parts.push(caps.llm ? 'LLM이 메뉴·정보를 정리하고 저장소를 추천합니다' : 'LLM 프로바이더가 없어 수집한 구조만 정리합니다');
  return (
    <ul className="mutedtext" style={{ fontSize: 11, margin: '4px 0', paddingLeft: 18 }}>
      {parts.map((p) => <li key={p}>{p}</li>)}
    </ul>
  );
}

/**
 * 스캔 진행 — 스캔하는 동안 서버가 남기는 기록을 이어 받아 보인다(services/infosource.progress).
 * 끝난 뒤에도 마지막 기록은 남겨 둔다 — 무엇을 받고 어디서 막혔는지 바로 읽게.
 */
function ScanProgress({ id, running }: { id: number; running: boolean }) {
  const [steps, setSteps] = useState<{ at: string; text: string }[]>([]);
  const seen = useRef(0);
  const box = useRef<HTMLDivElement>(null);

  usePolling(async () => {
    const out = await api.sourceProgress(id, seen.current);
    if (out.total < seen.current) {   // 새 스캔이 시작됐다 — 처음부터 다시
      seen.current = 0;
      setSteps([]);
      return;
    }
    seen.current = out.total;
    if (out.steps.length) setSteps((prev) => [...prev, ...out.steps].slice(-200));
  }, 1000, running);

  useEffect(() => { box.current?.scrollTo({ top: box.current.scrollHeight }); }, [steps]);

  if (!steps.length) return null;
  return (
    <div ref={box} className="mono" style={{
      maxHeight: 180, overflow: 'auto', fontSize: 11, marginTop: 8, padding: '4px 6px',
      background: 'var(--bg-subtle, #0b1220)', borderRadius: 4,
    }}>
      {steps.map((s, i) => (
        <div key={i}>{new Date(s.at).toLocaleTimeString()} {s.text}</div>
      ))}
    </div>
  );
}

/**
 * 수집 조건 — 사이트에서 모으는 것은 **조회 결과 파일과 첨부파일**이다. 화면마다 조회 조건을 정해
 * 두면 북마크가 그 화면에 채우고 조회한 뒤, 결과 파일(엑셀 — 없으면 결과 표를 CSV로)과 행마다
 * 상세의 첨부를 받는다. 조건은 LLM이 스캔한 조회 칸을 보고 제안하고, 사람이 고쳐 저장한다 —
 * 저장한 조건이 북마크에 실린다.
 */
function QueryPanel({ row, onChange }: { row: SourceOut; onChange: (r: SourceOut) => void }) {
  const state = useApi(() => api.sourceQueries(row.id), [row.id, row.scanned_at, row.queries_version]);
  const [screens, setScreens] = useState<SourceQueryScreen[] | null>(null);
  const [notes, setNotes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);

  useEffect(() => { if (state.data) setScreens(state.data.screens); }, [state.data]);

  const candidates = state.data?.candidates ?? [];
  const fieldsOf = (url: string) => candidates.find((c) => c.url === url)?.fields ?? [];
  const edit = (next: SourceQueryScreen[]) => { setScreens(next); setSaved(false); };
  const setScreen = (i: number, s: SourceQueryScreen) => edit((screens ?? []).map((x, j) => (j === i ? s : x)));

  const suggest = async () => {
    setBusy(true);
    setError('');
    try {
      const out = await api.suggestSourceQueries(row.id);
      edit(out.screens);
      setNotes(out.notes);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!screens) return;
    setBusy(true);
    setError('');
    try {
      const out = await api.saveSourceQueries(row.id, screens.map(({ why: _why, ...s }) => s));
      setScreens(out.screens);
      setSaved(true);
      // 북마크 코드가 조건을 싣는다 — 출처를 다시 읽어 북마크를 새로 만든다.
      onChange(await api.getSource(row.id));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const unused = candidates.filter((c) => !(screens ?? []).some((s) => s.url === c.url));
  // 지난 수집에서 막힌 화면 — 고칠 길을 물을 수 있다(그 자리에 실제로 있던 것 중에서 고른다)
  const blocked = (row.scan?.collected ?? []).filter((c) => c.repair);
  const [fixes, setFixes] = useState<Record<string, Awaited<ReturnType<typeof api.repairSourceQuery>>>>({});
  const askRepair = async (url: string) => {
    setBusy(true);
    setError('');
    try {
      const out = await api.repairSourceQuery(row.id, url);
      setFixes((prev) => ({ ...prev, [url]: out }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>수집 조건</h3>
      <p style={{ fontSize: 12, margin: '4px 0' }}>
        <b>무엇을 받을지</b> 정하는 곳입니다. 스캔은 사이트의 메뉴와 화면 구조만 읽습니다 — 실제 업무 자료(조회
        결과와 첨부파일)는 여기 적힌 화면만, 적힌 조건으로 조회해서 받습니다. ② 스캔·수집과 ③ 봇 실행이 이 목록대로
        돌고, 저장하면 다음 실행부터 바뀐 조건이 쓰입니다. 조건이 없으면 스캔만 합니다.
      </p>
      <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
        화면마다 — 여는 길, 조회 조건, 조회·엑셀 버튼, 상세를 여는 열. 북마크가 그 화면을 조회해 <b>결과 파일</b>(엑셀
        버튼, 없으면 결과 표를 CSV로)과 <b>행마다 상세의 첨부파일</b>을 받습니다. 날짜는 <span className="mono">@today</span>·
        <span className="mono">@today-7d</span>·<span className="mono">@today-1m</span>처럼 쓰면 북마크를 누른 날로
        바뀝니다. 기간처럼 칸이 둘이면 같은 칸 이름을 두 번(시작, 끝 순서) 넣습니다.
      </p>
      <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
        화면을 여는 길: ① 그 화면을 열어 둔 채 북마크를 누르면 그 자리에서, ② 「녹화」나 메뉴 탐색이 찾은 길이
        있으면 메뉴를 다시 눌러서, ③ 아니면 주소로 엽니다. 「LLM 분석」은 화면마다 어느 길로 열지·조회 버튼·상세를
        여는 열·엑셀 버튼·조건을 지난 스캔과 수집 기록에서 골라 줍니다(저장 전에 고칠 수 있다). 메뉴로만 열리는
        화면(주소로 열면 다른 화면이 뜬다)은 ③의 「녹화 시작」으로 메뉴 → 조건 → 조회 → 첫 행의 상세 → 첨부를 한 번
        보여 주면 여기에 바로 더해집니다.
      </p>
      <Async state={state}>
        {() => (
          <>
            {(screens ?? []).length === 0 && (
              <p className="mutedtext" style={{ fontSize: 12 }}>
                {candidates.length
                  ? '아직 조건이 없습니다 — 「LLM 분석」으로 받거나 아래에서 화면을 더하세요.'
                  : '조회 칸이 보인 화면이 없습니다 — 조회 화면을 연 상태로 브라우저 스캔을 하세요.'}
              </p>
            )}
            {(screens ?? []).map((s, i) => {
              const fields = fieldsOf(s.url);
              return (
                <div key={s.url} style={{ border: '1px solid var(--border)', borderRadius: 4, padding: 6, marginBottom: 6 }}>
                  <div className="row" style={{ gap: 6, alignItems: 'center' }}>
                    <input value={s.screen} style={{ flex: 1 }} aria-label="화면 이름"
                      onChange={(e) => setScreen(i, { ...s, screen: e.target.value })} />
                    <button className="small secondary" onClick={() => edit((screens ?? []).filter((_, j) => j !== i))}>빼기</button>
                  </div>
                  <div className="mono mutedtext" style={{ fontSize: 10, wordBreak: 'break-all' }}>{s.url}</div>
                  <div className="mutedtext" style={{ fontSize: 11 }}>
                    {s.path?.length
                      ? `가는 길: ${s.path.map((p) => p.target.text || p.target.tag).join(' → ')}`
                      : '가는 길 없음 — 열어 둔 화면이나 주소로 엽니다'}
                    {s.detail && ` · 상세는 ${s.detail.col + 1}번째 칸`}
                    {s.hints?.search && ` · 조회 버튼 「${s.hints.search}」`}
                    {s.hints?.export && ` · 엑셀 「${s.hints.export}」`}
                  </div>
                  {s.plan && (
                    <div style={{ fontSize: 11 }}>
                      <span className={`status ${s.plan.method === 'record' ? 'warn' : 'info'}`}>{METHOD[s.plan.method]}</span>
                      {s.plan.reason && <span className="mutedtext"> {s.plan.reason}</span>}
                      {s.plan.method === 'record' && <span> — ③의 「녹화 시작」으로 보여 주세요</span>}
                      {s.plan.risks.length > 0 && <div className="mutedtext">주의: {s.plan.risks.join(' · ')}</div>}
                    </div>
                  )}
                  {s.why && <div className="mutedtext" style={{ fontSize: 11 }}>{s.why}</div>}
                  {blocked.filter((b) => b.url === s.url).map((b) => {
                    const fix = fixes[s.url];
                    return (
                      <div key={b.url} style={{ fontSize: 11, margin: '4px 0', padding: 4, border: '1px dashed var(--border)', borderRadius: 4 }}>
                        <span className="status warn">막힘</span> {b.notes[0] ?? '지난 수집에서 막혔습니다'}
                        {!fix ? (
                          <button className="small secondary" style={{ marginLeft: 6 }} disabled={busy}
                            onClick={() => void askRepair(s.url)}>고칠 길 제안</button>
                        ) : (
                          <div style={{ marginTop: 4 }}>
                            제안({fix.via === 'llm' ? 'LLM' : '비슷한 글자'}): <b>{fix.change}</b>
                            {fix.reason && <span className="mutedtext"> — {fix.reason}</span>}
                            <button className="small" style={{ marginLeft: 6 }}
                              onClick={() => setScreen(i, { ...fix.screen, why: s.why })}>적용</button>
                            <span className="mutedtext"> (적용한 뒤 저장)</span>
                          </div>
                        )}
                      </div>
                    );
                  })}
                  {s.conditions.map((c, k) => (
                    <div key={k} className="row" style={{ gap: 4, marginTop: 4 }}>
                      {fields.length ? (
                        <select value={c.field} aria-label="칸"
                          onChange={(e) => setScreen(i, { ...s, conditions: s.conditions.map((x, m) => (m === k ? { ...x, field: e.target.value } : x)) })}>
                          {!fields.includes(c.field) && <option value={c.field}>{c.field}</option>}
                          {fields.map((f) => <option key={f} value={f}>{f}</option>)}
                        </select>
                      ) : (
                        <input value={c.field} placeholder="칸 이름" aria-label="칸"
                          onChange={(e) => setScreen(i, { ...s, conditions: s.conditions.map((x, m) => (m === k ? { ...x, field: e.target.value } : x)) })} />
                      )}
                      <input value={c.value} placeholder="값 (예: @today-1m)" aria-label="값" style={{ flex: 1 }}
                        onChange={(e) => setScreen(i, { ...s, conditions: s.conditions.map((x, m) => (m === k ? { ...x, value: e.target.value } : x)) })} />
                      <button className="small secondary"
                        onClick={() => setScreen(i, { ...s, conditions: s.conditions.filter((_, m) => m !== k) })}>×</button>
                    </div>
                  ))}
                  <button className="small secondary" style={{ marginTop: 4 }}
                    onClick={() => setScreen(i, { ...s, conditions: [...s.conditions, { field: fields[0] ?? '', value: '' }] })}>
                    조건 더하기
                  </button>
                </div>
              );
            })}
            {unused.length > 0 && (
              <select value="" aria-label="화면 더하기" style={{ marginBottom: 6 }}
                onChange={(e) => {
                  const c = candidates.find((x) => x.url === e.target.value);
                  if (c) {
                    edit([...(screens ?? []), {
                      screen: c.title || c.url, url: c.url, conditions: [],
                      ...(c.path ? { path: c.path, start: c.start, frame: c.frame } : {}),
                    }]);
                  }
                }}>
                <option value="">+ 화면 더하기</option>
                {unused.map((c) => <option key={c.url} value={c.url}>{c.title || c.url} ({c.fields.join(', ')})</option>)}
              </select>
            )}
            <div className="row" style={{ gap: 8 }}>
              <button className="small secondary" disabled={busy || !candidates.length} onClick={suggest}>
                {busy ? '…' : 'LLM 분석(방법·조건)'}
              </button>
              <button className="small" disabled={busy || screens === null} onClick={save}>저장</button>
              {saved && <span className="mutedtext" style={{ fontSize: 11 }}>
                저장했습니다 — 다음 스캔·수집·봇 실행부터 새 조건으로 돕니다(콘솔 없이 쓸 때는 북마크를 다시 끌어다 놓으세요).
              </span>}
            </div>
            {notes.map((n) => <p key={n} className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>{n}</p>)}
          </>
        )}
      </Async>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

const METHOD: Record<SourceQueryMethod, string> = {
  open: '열어 둔 화면', path: '메뉴 길', url: '주소', record: '녹화 필요',
};


// 창에 보내는 표 — http 콘솔에서는 crypto.randomUUID가 없다(보안 맥락에서만).
const nonceOf = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('');
const MAX_LIVE_LINES = 300;

type LiveMode = 'record' | 'scan' | 'replay';

/**
 * 브라우저 세션(북마크와 콘솔 사이의 실시간 통로) — ② 스캔·수집과 ③ 녹화·봇 실행이 함께 쓴다.
 *
 * 콘솔이 업무 사이트를 새 탭으로 열고, 그 탭에서 북마크를 한 번 누르면 북마크가 이 창(opener)에 인사한다.
 * 콘솔이 표(nonce)와 할 일·수집 조건을 답하면 북마크가 바로 시작하고, 진행 기록을 실시간으로, 결과를 파일
 * 대신 이리로 보낸다 — 콘솔이 받아서 올린다. 메시지는 보낸 출처(업무 사이트)와 창(연 탭)을 둘 다 확인한다.
 * 녹화를 끝낸 탭은 연결이 살아 있어 북마크를 다시 누르지 않고 봇을 돌린다. 새 탭은 창 기능(width 등) 없이
 * 연다 — 팝업 창에는 북마크바가 없다.
 */
function useLive(row: SourceOut | null, onChange: (r: SourceOut) => void) {
  const site = row ? new URL(row.url).origin : '';
  const win = useRef<Window | null>(null);
  const nonce = useRef('');
  const latest = useRef(row);
  latest.current = row;
  // 봇이 돌 화면 — 녹화 직후면 방금 녹화한 화면, 아니면 수집 조건 전부
  const only = useRef<SourceQueryScreen[]>([]);
  const [mode, setMode] = useState<LiveMode | null>(null);
  const [state, setState] = useState<'' | 'waiting' | 'linked' | 'uploading' | 'done' | 'lost'>('');
  const [lines, setLines] = useState<string[]>([]);
  const [explore, setExplore] = useState(true);
  const [recorded, setRecorded] = useState<SourceQueryScreen | null>(null);
  const [error, setError] = useState('');
  const push = (t: string) => setLines((prev) => [...prev, `${new Date().toTimeString().slice(0, 8)} ${t}`].slice(-MAX_LIVE_LINES));

  const receive = async (kind: 'record' | 'scan', payload: unknown) => {
    const id = latest.current?.id;
    if (!id) return;
    setState('uploading');
    setError('');
    const file = new File([JSON.stringify(payload)], `gpax-${kind}-${id}.json`, { type: 'application/json' });
    try {
      if (kind === 'record') {
        const out = await api.uploadSourceRecording(id, file);
        // 같은 주소의 화면이 이미 있으면 서버가 주소에 #이름을 붙인다 — 이름과 주소 앞부분으로 찾는다.
        const recipe = (payload as { recipe?: { screen?: string; url?: string } }).recipe ?? {};
        const base = (recipe.url ?? '').split('#')[0];
        setRecorded(out.screens.find((q) => q.screen === recipe.screen && q.url.split('#')[0] === base)
          ?? out.screens.find((q) => q.url === recipe.url) ?? null);
        push(`녹화를 수집 조건에 올렸습니다 — 화면 ${out.screens.length}개. 「녹화대로 봇 실행」으로 같은 일을 시켜 보세요`);
        onChange(await api.getSource(id));
      } else {
        onChange(await api.uploadBrowserScan(id, file));
        push('결과를 올렸습니다 — 오른쪽에서 읽은 것·받은 파일을, 아래 맨 끝에서 저장을');
      }
      setState('done');
    } catch (e) {
      setError((e as Error).message);
      setState('linked');
    }
  };

  useEffect(() => {
    if (!mode || !row) return;
    const onMessage = (e: MessageEvent) => {
      const m = e.data;
      if (!m || typeof m !== 'object' || e.origin !== site || !win.current || e.source !== win.current) return;
      if (m.gpax === 'hello') {
        // 옛 출처별 북마크는 출처 id로, 통합 북마크는 id 없이 인사한다 — 이 탭은 이 출처로 연 것이다.
        if (m.source !== undefined && m.source !== row.id) return;
        // 북마크를 다시 누를 때마다(화면을 옮긴 뒤 등) 새 표 — 옛 북마크의 메시지는 받지 않는다.
        nonce.current = nonceOf();
        const now = latest.current;
        win.current.postMessage({
          gpax: 'start', nonce: nonce.current, mode, explore,
          source: now && { id: now.id, name: now.name, origin: site, queries: now.queries ?? [],
                           version: now.queries_version ?? '' },
          queries: mode === 'replay' ? only.current : latest.current?.queries ?? [],
          version: latest.current?.queries_version ?? '',
        }, site);
        setState('linked');
        push(`창의 북마크와 연결했습니다 — ${LIVE_LABEL[mode]}을 시작합니다`);
        return;
      }
      if (!nonce.current || m.nonce !== nonce.current) return;
      if (m.gpax === 'log' && typeof m.text === 'string') push(m.text.slice(0, 300));
      if (m.gpax === 'result' && (m.kind === 'record' || m.kind === 'scan')) void receive(m.kind, m.payload);
    };
    window.addEventListener('message', onMessage);
    const watch = window.setInterval(() => {
      if (win.current?.closed) {
        win.current = null;
        nonce.current = '';
        setState((st) => (st === 'done' || st === 'uploading' ? st : 'lost'));
      }
    }, 1000);
    return () => {
      window.removeEventListener('message', onMessage);
      window.clearInterval(watch);
    };
    // receive·push는 매번 새로 만들어지지만 row.id·site가 같으면 하는 일이 같다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, explore, site, row?.id]);

  const open = (next: LiveMode) => {
    if (!row) return;
    setError('');
    setLines([]);
    nonce.current = '';
    if (next !== 'replay') setRecorded(null);
    const w = window.open(row.url, `gpax-source-${row.id}`);
    if (!w) {
      setError('새 탭이 막혔습니다 — 브라우저의 팝업 차단을 이 콘솔에 대해 풀어 주세요.');
      return;
    }
    win.current = w;
    setMode(next);
    setState('waiting');
    push(`${row.url}을 새 탭으로 열었습니다 — 로그인한 뒤 그 탭에서 북마크 「GPAX 스캔 · ${row.name}」을 누르세요`);
  };
  // 봇 — 녹화를 끝낸 탭이 살아 있으면 그 탭에서 바로, 아니면 새 탭을 열고 북마크를 기다린다.
  const runBot = () => {
    only.current = recorded ? [recorded] : latest.current?.queries ?? [];
    setError('');
    if (win.current && !win.current.closed && nonce.current) {
      setMode('replay');
      setState('linked');
      win.current.postMessage({ gpax: 'replay', nonce: nonce.current, queries: only.current,
                                version: latest.current?.queries_version ?? '' }, site);
      push(`봇 실행 — ${only.current.map((q) => q.screen).join(', ')}`);
    } else {
      open('replay');
    }
  };
  const send = (gpax: 'end' | 'stop') => {
    if (win.current && nonce.current) win.current.postMessage({ gpax, nonce: nonce.current }, site);
  };
  const busy = state === 'waiting' || state === 'linked' || state === 'uploading';
  return { mode, state, lines, explore, setExplore, recorded, error, busy, open, runBot, send };
}

type Live = ReturnType<typeof useLive>;
const LIVE_LABEL: Record<LiveMode, string> = { scan: '스캔·수집', record: '녹화', replay: '봇 실행' };

// 그 카드의 일(modes)이 돌 때만 — 상태, 녹화 끝·멈추기, 실시간 기록
function LiveStatus({ live, modes }: { live: Live; modes: LiveMode[] }) {
  if (!live.mode || !modes.includes(live.mode)) return null;
  const { mode, state } = live;
  return (
    <>
      <div className="row" style={{ gap: 8, alignItems: 'center', marginTop: 6 }}>
        {state === 'linked' && mode === 'record' && <button className="small" onClick={() => live.send('end')}>녹화 끝</button>}
        {state === 'linked' && mode !== 'record' && (
          <button className="small secondary" onClick={() => live.send('stop')}>멈추기</button>
        )}
        {state && <span className="mutedtext" style={{ fontSize: 12 }}>
          {LIVE_LABEL[mode]} · {{
            waiting: '탭에서 북마크를 누르기를 기다립니다…', linked: '연결됨', uploading: '올리는 중…', done: '끝',
            lost: '탭이 닫혔거나 사이트가 창 연결을 끊었습니다',
          }[state]}
        </span>}
      </div>
      {state === 'waiting' && (
        <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
          북마크를 눌렀는데 여기에 아무것도 오지 않으면 옛 북마크입니다 — ①에서 다시 끌어다 놓으세요. 탭의 북마크 창에
          「콘솔과 연결됨」이 뜨지 않으면 사이트가 창 연결을 끊은 것입니다 — ①의 「콘솔 없이 쓸 때」를 보세요.
        </p>
      )}
      {mode === 'record' && state === 'linked' && (
        <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
          탭에서 메뉴를 눌러 조회 화면을 열고, 조건을 넣고, 조회 → 첫 행의 상세 → 첨부를 누른 뒤 「녹화 끝」(여기나
          탭의 창). 화면 전체가 새로 열리면 녹화가 끊기니 그때는 북마크를 다시 누르세요.
        </p>
      )}
      {live.lines.length > 0 && (
        <pre className="mono" style={{ fontSize: 11, maxHeight: 200, overflow: 'auto', margin: '6px 0 0', whiteSpace: 'pre-wrap' }}>
          {live.lines.join('\n')}
        </pre>
      )}
      {live.error && <p className="error">{live.error}</p>}
    </>
  );
}

/**
 * ① 북마크 추가 — SSO 뒤의 사이트는 서버가 로그인할 수 없으니 로그인한 탭에서 북마크릿이 읽는다. 북마크는
 * 하나(통합 「GPAX」 — 스마트워크의 웹 Outlook 메일도 같은 것)이고 한 번만 끌어다 놓는다. 출처와 수집 조건은
 * 누를 때 콘솔이 보낸다. 예전 출처별 북마크가 내려받은 파일은 여기서 올린다.
 */
function BookmarkPanel({ row, onChange }: { row: SourceOut; onChange: (r: SourceOut) => void }) {
  const [copied, setCopied] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');

  const copy = () => {
    // http에서는 navigator.clipboard가 없다 — 고른 글을 복사하는 옛 방식으로.
    const area = document.createElement('textarea');
    area.value = agentCode();
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    setCopied(ok);
    if (!ok) setError('복사하지 못했습니다 — 북마크로 끌어다 놓아 쓰세요.');
  };

  // 내려받은 파일 — 스캔·녹화를 한 곳에서 받는다. 맨 앞의 agent로 가른다(파일 전체를 읽지 않는다).
  const upload = async (file: File | undefined) => {
    if (!file) return;
    setUploading(true);
    setError('');
    try {
      if ((await file.slice(0, 200).text()).includes('"gpax-record/')) {
        await api.uploadSourceRecording(row.id, file);
        onChange(await api.getSource(row.id));
      } else {
        onChange(await api.uploadBrowserScan(row.id, file));
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>① 북마크 추가</h3>
      <p style={{ fontSize: 12, margin: '4px 0' }}>
        <GpaxBookmark /> 을 북마크바로 한 번 끌어다 놓습니다(또는{' '}
        <button className="small secondary" onClick={copy}>{copied ? '복사됨' : '코드 복사'}</button>
        {' '}후 개발자 도구 Console에 붙여 넣기). 모든 출처·사이트에 같은 북마크 하나입니다.
      </p>
      <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
        사내 SSO로 로그인하는 사이트는 서버가 들어갈 수 없어, 로그인한 내 브라우저에서 이 북마크가 읽습니다 — 세션은
        브라우저 밖으로 나가지 않고, 입력값·쿠키는 읽지 않습니다(입력칸은 수집 조건을 채울 때만 씁니다). ②·③의 시작
        버튼이 연 탭에서 누르면 그 일을 바로 시작합니다. 사이트를 직접 열고 누르면 작은 GPAX 창이 열려 이 출처를 찾아
        잇고, 사이트 화면의 GPAX 창에서 스캔·수집·녹화·봇 실행·스마트워크로를 고릅니다 — 어느 쪽이든 수집 조건은
        콘솔이 지금 것을 보내므로 조건을 고쳐도 다시 끌어다 놓지 않습니다. 이 브라우저의 다른 탭에 콘솔이 로그인돼
        있어야 합니다.
      </p>
      <details style={{ fontSize: 12 }}>
        <summary style={{ cursor: 'pointer' }}>예전 북마크가 내려받은 파일 올리기</summary>
        <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
          예전 출처별 북마크(「GPAX 스캔 · 이름」)를 콘솔 없이 누르면 결과가{' '}
          <span className="mono">gpax-scan-{row.id}-*.json</span>·<span className="mono">gpax-record-{row.id}-*.json</span>으로
          내려받아집니다. 그 파일을 여기에 올립니다(녹화는 수집 조건에 더해진다):{' '}
          <input type="file" accept=".json,application/json" disabled={uploading || row.status === 'scanning'}
                 onChange={(e) => { void upload(e.target.files?.[0]); e.target.value = ''; }} />
          {uploading && ' 올리는 중…'}
        </p>
      </details>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

/** ② 스캔·수집 — 사이트를 읽고(메뉴·화면·가는 길) 수집 조건이 있으면 그대로 받는다. 서버 스캔은 보조. */
function ScanPanel({ row, live, onServerScan }: { row: SourceOut; live: Live; onServerScan: () => void }) {
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>② 스캔·수집</h3>
      <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <button className="small secondary" disabled={live.busy} onClick={() => live.open('scan')}>스캔·수집 시작(새 탭)</button>
        <label style={{ fontSize: 12 }}>
          <input type="checkbox" checked={live.explore} disabled={live.busy}
                 onChange={(e) => live.setExplore(e.target.checked)} /> 메뉴 탐색
        </label>
        {/* 서버 스캔은 로그인 없이 열리는 사이트용 보조 버튼이다 */}
        <button className="small secondary" disabled={row.status === 'scanning'} onClick={onServerScan}>
          {row.status === 'scanning' ? '스캔 중…' : `서버에서 ${row.scan ? '다시 스캔' : '스캔'}`}
        </button>
      </div>
      <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
        새 탭에서 로그인하고 북마크를 누르면 바로 시작합니다. 메뉴와 열린 화면을 읽고(메뉴 탐색을 켜면 스크립트로
        여는 메뉴를 숨긴 틀에서 눌러 조회 화면과 가는 길을 찾는다), 아래 수집 조건이 있으면 화면마다 조회해 결과
        파일과 행마다 상세의 첨부를 받습니다. 진행은 여기와 탭 오른쪽 위 창(머리글을 끌어 옮길 수 있다)에 실시간으로
        보이고, 결과는 바로 올라갑니다. 「서버에서 스캔」은 로그인 없이 열리는 사이트만 — 서버가 읽은 것으로 결과를
        바꿉니다.
      </p>
      <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
        조회·상세·첨부·엑셀 버튼만 누르고, 저장·삭제·승인 같은 버튼은 누르지 않으며 사이트가 묻는 창에는 '아니오'로
        답합니다. 메뉴의 GET 링크는 25곳까지 더 열어 보고(로그아웃·삭제는 건너뜀), 파일은 60개(하나 10MB, 모두
        50MB)까지, 상세는 기본 20행(탭의 창에서 바꾸거나 멈출 수 있다)까지 엽니다.
      </p>
      <LiveStatus live={live} modes={['scan']} />
      <ScanProgress id={row.id} running={row.status === 'scanning'} />
    </div>
  );
}

/**
 * ③ 녹화·봇 실행 — 메뉴로만 열리는 화면은 사람이 한 번 보여 주고(녹화 → 수집 조건에 더해진다), 봇이 그대로
 * 해 본다. 봇은 화면을 다시 읽지 않고 녹화한 길로 처음부터 열어 조건 → 조회 → 상세 → 첨부를 받는다 — 결과는
 * 스캔 결과의 받은 파일·수집 결과로 바뀌고 화면 목록은 그대로다.
 */
function RecordPanel({ row, live }: { row: SourceOut; live: Live }) {
  const target = live.recorded ? `녹화대로 봇 실행 — ${live.recorded.screen}` : '봇 실행(수집 조건 전부)';
  const ready = !!live.recorded || !!row.queries?.length;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>③ 녹화·봇 실행</h3>
      <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <button className="small secondary" disabled={live.busy} onClick={() => live.open('record')}>녹화 시작(새 탭)</button>
        <button className="small secondary" disabled={live.busy || !ready || !row.scan} onClick={live.runBot}>{target}</button>
      </div>
      <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
        녹화: 메뉴로만 열리는 화면(주소로 열면 다른 화면이 뜬다)을 메뉴 → 조건 → 조회 → 첫 행의 상세 → 첨부 순서로
        한 번 보여 주면 아래 수집 조건에 그 화면이 더해집니다. 봇 실행: 녹화한 길대로 처음부터 화면을 열어 같은 일을
        합니다 — 녹화 직후에는 방금 녹화한 화면만, 아니면 수집 조건 전부. 녹화를 끝낸 탭이 열려 있으면 그 탭에서
        바로 돌고, 닫혔으면 새 탭에서 북마크를 누르면 됩니다. 받은 파일은 스캔 결과로 올라가고 화면 목록은 그대로입니다.
        {!row.scan && ' (스캔·수집을 한 번 한 뒤에 돌릴 수 있습니다.)'}
      </p>
      <LiveStatus live={live} modes={['record', 'replay']} />
    </div>
  );
}

// 요청 헤더를 직접 넣는 방식은 없앴다(웹사이트는 북마크가 로그인한 브라우저에서 읽는다). 예전에 넣어 둔
// 헤더가 남은 출처만 — 이름을 보이고 지울 수 있게 한다(값은 다시 보여 주지 않는다).
function StoredHeaders({ row, onChange }: { row: SourceOut; onChange: (r: SourceOut) => void }) {
  const [error, setError] = useState('');
  if (!row.headers.length) return null;
  const clear = async () => {
    if (!window.confirm('저장된 요청 헤더를 모두 지웁니까?')) return;
    setError('');
    try {
      onChange(await api.updateSource(row.id, { headers: '' }));
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <div className="panel">
      <p style={{ fontSize: 12, margin: 0 }}>
        예전에 넣어 둔 요청 헤더 {row.headers.map((h) => <code key={h} style={{ marginRight: 6 }}>{h}</code>)}가
        서버 스캔에 붙습니다.{' '}
        <button className="small secondary" onClick={() => void clear()}>지우기</button>
      </p>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

interface Pick {
  mode: 'existing' | 'new' | 'skip';
  existing: string;
  name: string;
  path: string;
  organizationId: string;
}

function pickOf(t: SourceTarget): Pick {
  return t.mode === 'existing'
    ? { mode: 'existing', existing: t.store, name: '', path: '', organizationId: '' }
    : { mode: 'new', existing: '', name: t.store, path: t.path ?? '', organizationId: '' };
}

function SavePanel({ row, onSaved }: { row: SourceOut; onSaved: (r: SourceSaveResult) => void }) {
  const proposal = row.proposal!.targets;
  const categories = CATEGORY_ORDER.filter((c) => proposal[c]);
  const stores = useApi(() => api.listStorageStores(), []);
  const orgs = useApi<OrgOut[]>(() => api.listOrgs(), []);
  const [picks, setPicks] = useState<Partial<Record<SourceCategory, Pick>>>(
    () => Object.fromEntries(categories.map((c) => [c, pickOf(proposal[c]!)])));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<SourceSaveResult | null>(null);

  const writable = (stores.data ?? []).filter((s: StorageStore) => !s.read_only);
  const set = (c: SourceCategory, patch: Partial<Pick>) =>
    setPicks((cur) => ({ ...cur, [c]: { ...cur[c]!, ...patch } }));
  const files = row.scan?.files ?? [];
  const sizeOf = (c: SourceCategory) =>
    c === 'pages' ? `화면 ${row.scan?.pages?.length ?? 0}장` : `${files.filter((f) => f.category === c).length}개`;

  const targets: Partial<Record<SourceCategory, SourceSaveTarget>> = {};
  categories.forEach((c) => {
    const p = picks[c]!;
    targets[c] = p.mode === 'skip' ? { mode: 'skip' }
      : p.mode === 'existing' ? { mode: 'existing', store: p.existing }
        : { mode: 'new', store: p.name.trim(), path: p.path.trim(),
            organization_id: Number(p.organizationId) };
  });
  const chosen = categories.filter((c) => targets[c]!.mode !== 'skip');
  const ready = chosen.length > 0 && chosen.every((c) => {
    const t = targets[c]!;
    return t.store && (t.mode !== 'new' || (t.path && t.organization_id));
  });

  const save = async () => {
    // 같은 이름의 새 저장소는 한 번만 만든다(서버도 그렇다) — 폴더는 먼저 적은 유형의 것.
    const created = new Map<string, string>();
    chosen.forEach((c) => {
      const t = targets[c]!;
      if (t.mode === 'new' && !created.has(t.store!)) created.set(t.store!, t.path!);
    });
    if (created.size && !window.confirm(
      `새 저장소를 만듭니다.\n${[...created].map(([n, p]) => `${n} — ${p}`).join('\n')}\n\n` +
      '폴더를 만들고 선택한 부서의 저장소로 DB에 등록합니다.')) return;
    setBusy(true);
    setError('');
    try {
      const res = await api.saveSource(row.id, targets);
      setResult(res);
      onSaved(res);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>저장 위치</h3>
      <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
        유형마다 따로 고릅니다. 같은 자리에 다시 저장하면 바뀐 파일만 덮어쓰고, 사이트에서 없어진 파일은
        그 저장소의 휴지통(.trash)으로 옮깁니다. 저장 안 함으로 둔 유형은 지난번 것을 건드리지 않습니다.
      </p>
      {categories.map((c) => (
        <TargetPicker key={c} label={`${CATEGORY[c]} · ${sizeOf(c)}`} proposal={proposal[c]!}
                      pick={picks[c]!} writable={writable} orgs={orgs.data ?? []}
                      onChange={(patch) => set(c, patch)} />
      ))}
      <div className="row" style={{ gap: 8, marginTop: 10 }}>
        <button className="small" disabled={busy || !ready} onClick={save}>
          {busy ? '저장 중…' : row.status === 'saved' ? '다시 저장' : '저장'}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {result && (
        <div style={{ fontSize: 12, marginTop: 8 }}>
          <p style={{ margin: '4px 0' }}>
            {result.files.length}개를 썼습니다 · 내용이 같아 그대로 둔 것 {result.same.length}개
            {result.removed.length > 0 && <> · 휴지통으로 옮긴 것 {result.removed.length}개</>}.
            색인이 돌면 문서 검색·온톨로지에 나타납니다.
          </p>
          {result.stores.filter((st) => st.created).map((st) => (
            <p key={st.store} style={{ margin: '4px 0' }}>
              새 저장소 <b>{st.store}</b>를 만들었습니다(<span className="mono">{st.root}</span>).
            </p>
          ))}
          {result.removed.length > 0 && (
            <ul className="mono" style={{ fontSize: 11, margin: '4px 0', paddingLeft: 18 }}>
              {result.removed.map((r) => <li key={r}>{r}</li>)}
            </ul>
          )}
        </div>
      )}
      {!result && row.status === 'saved' && row.target_store && (
        <p className="mutedtext" style={{ fontSize: 11 }}>
          마지막 저장: {row.target_store}
          {row.saved_at && ` · ${new Date(row.saved_at).toLocaleString('ko-KR')}`}
        </p>
      )}
    </div>
  );
}

function TargetPicker({ label, proposal, pick, writable, orgs, onChange }: {
  label: string; proposal: SourceTarget; pick: Pick; writable: StorageStore[]; orgs: OrgOut[];
  onChange: (patch: Partial<Pick>) => void;
}) {
  // 유형마다 라디오 묶음이 따로다.
  const group = useId();
  return (
    <div style={{ borderTop: '1px solid var(--border)', paddingTop: 8, marginTop: 8, fontSize: 12 }}>
      <b>{label}</b>
      <p style={{ margin: '4px 0' }}>
        제안: {proposal.mode === 'existing' ? `기존 저장소 ${proposal.store}` : `새 저장소 ${proposal.store}`}
        <br /><span className="mutedtext">{proposal.reason}</span>
      </p>
      {(proposal.evidence ?? []).length > 0 && (
        <details style={{ margin: '4px 0' }}>
          <summary className="mutedtext" style={{ fontSize: 11 }}>저장소마다 걸린 문서</summary>
          <table style={{ fontSize: 11 }}>
            <thead><tr><th>저장소</th><th>걸린 문서</th><th>걸린 키워드</th></tr></thead>
            <tbody>
              {proposal.evidence.map((e) => (
                <tr key={e.store}>
                  <td className="mono">{e.store}{e.read_only && ' (읽기 전용)'}</td>
                  <td className="mono">{e.hits}</td>
                  <td>{e.keywords.join(', ') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
      <label style={{ display: 'block', marginBottom: 4 }}>
        <input type="radio" name={group} checked={pick.mode === 'existing'}
               onChange={() => onChange({ mode: 'existing' })} />
        {' '}기존 저장소에
        {pick.mode === 'existing' && (
          <select value={pick.existing} style={{ marginLeft: 8 }}
                  onChange={(e) => onChange({ existing: e.target.value })}>
            <option value="">선택</option>
            {writable.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
          </select>
        )}
      </label>
      <label style={{ display: 'block', marginBottom: 4 }}>
        <input type="radio" name={group} checked={pick.mode === 'new'} onChange={() => onChange({ mode: 'new' })} />
        {' '}새 저장소를 만들어
      </label>
      {pick.mode === 'new' && (
        <div style={{ marginLeft: 22, marginBottom: 4 }}>
          <select value={pick.organizationId} onChange={(e) => onChange({ organizationId: e.target.value })}>
            <option value="">소속 부서 선택</option>
            {orgs.map((org) => <option key={org.id} value={org.id}>{org.name}</option>)}
          </select>
          <input className="mono" placeholder="이름 — 소문자·숫자·하이픈" value={pick.name}
                 style={{ width: '100%' }} onChange={(e) => onChange({ name: e.target.value })} />
          <input className="mono" placeholder="폴더(절대 경로)" value={pick.path}
                 style={{ width: '100%', marginTop: 6 }} onChange={(e) => onChange({ path: e.target.value })} />
        </div>
      )}
      <label style={{ display: 'block' }}>
        <input type="radio" name={group} checked={pick.mode === 'skip'} onChange={() => onChange({ mode: 'skip' })} />
        {' '}저장 안 함
      </label>
    </div>
  );
}

function ScanView({ row }: { row: SourceOut }) {
  const scan = row.scan;
  if (row.status === 'scanning') {
    return <div className="panel"><p className="mutedtext">스캔 중입니다 — 끝나면 이 자리에 결과가 나옵니다.</p></div>;
  }
  if (!scan) {
    return <div className="panel"><p className="mutedtext">아직 스캔하지 않았습니다.</p></div>;
  }
  const notes = <Notes row={row} />;
  const tabs =
    row.kind === 'web'
      ? [
          { key: 'menu', label: '메뉴 구성', content: <MenuTree items={scan.menu ?? []} /> },
          { key: 'pages', label: '조회 가능한 정보', content: <PagesTable pages={scan.pages ?? []} /> },
          ...((scan.files ?? []).length > 0
            ? [{ key: 'files', label: '내려받은 파일', content: <FilesTable files={scan.files ?? []} /> }]
            : []),
          ...((scan.pages ?? []).some((p) => p.shot !== undefined)
            ? [{ key: 'shots', label: '캡처', content: <Shots id={row.id} pages={scan.pages ?? []} /> }]
            : []),
          { key: 'notes', label: '기록', content: notes },
        ]
      : row.kind === 'api'
        ? [
            { key: 'endpoints', label: '엔드포인트', content: <Endpoints row={row} /> },
            { key: 'notes', label: '기록', content: notes },
          ]
        : [
            { key: 'tools', label: '도구', content: <Tools row={row} /> },
            { key: 'notes', label: '기록', content: notes },
          ];
  tabs.push({ key: 'history', label: '스캔 이력', content: <ScanHistory row={row} /> });
  return (
    <>
      {(scan.summary || scan.keywords.length > 0) && (
        <div className="panel">
          {scan.summary && <p style={{ marginTop: 0 }}>{scan.summary}</p>}
          {scan.keywords.length > 0 && (
            <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>키워드: {scan.keywords.join(', ')}</p>
          )}
        </div>
      )}
      <div className="panel"><Tabs key={row.scanned_at ?? ''} tabs={tabs} /></div>
    </>
  );
}

function MenuTree({ items }: { items: SourceMenuItem[] }) {
  if (!items.length) return <p className="mutedtext" style={{ fontSize: 12 }}>메뉴로 보이는 링크를 찾지 못했습니다.</p>;
  return (
    <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
      {items.map((m, i) => (
        <li key={`${m.label}-${i}`} style={{ margin: '3px 0' }}>
          {m.url ? <a href={m.url} target="_blank" rel="noreferrer">{m.label}</a> : m.label}
          {m.children.length > 0 && <MenuTree items={m.children} />}
        </li>
      ))}
    </ul>
  );
}

function PagesTable({ pages }: { pages: SourcePage[] }) {
  return (
    <table style={{ fontSize: 12 }}>
      <thead>
        <tr><th>화면</th><th>유형</th><th>조회할 수 있는 정보</th><th>조회 조건</th><th>표 항목</th></tr>
      </thead>
      <tbody>
        {pages.map((p) => (
          <tr key={p.url}>
            <td>
              <a href={p.url} target="_blank" rel="noreferrer">{p.title || p.url}</a>
              <ChangeBadge value={p.change} />
              <div className="mono mutedtext" style={{ fontSize: 10, wordBreak: 'break-all' }}>{p.url}</div>
            </td>
            <td>{PAGE_KIND[p.kind] ?? p.kind}{p.requires_login && p.kind !== 'login' && ' · 로그인 필요'}</td>
            <td>{p.info || '—'}</td>
            <td>{p.filters.join(', ') || '—'}</td>
            <td>{p.tables.map((t) => t.join(' · ')).join(' / ') || '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FilesTable({ files }: { files: SourceFile[] }) {
  return (
    <table style={{ fontSize: 12 }}>
      <thead><tr><th>파일</th><th>유형</th><th>링크 글자</th><th>크기</th><th>걸린 화면</th></tr></thead>
      <tbody>
        {files.map((f) => (
          <tr key={f.url}>
            <td>
              <span className="mono">{f.name}</span>
              <ChangeBadge value={f.change} />
              <div className="mono mutedtext" style={{ fontSize: 10, wordBreak: 'break-all' }}>{f.url}</div>
            </td>
            <td>{CATEGORY[f.category]}</td>
            <td>{f.text || '—'}</td>
            <td className="mono">
              {f.size >= 1_000_000 ? `${(f.size / 1_000_000).toFixed(1)}MB` : `${Math.ceil(f.size / 1000)}KB`}
            </td>
            <td className="mono" style={{ fontSize: 10, wordBreak: 'break-all' }}>{f.page || '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** 스캔 이력 — 같은 방법(서버·브라우저)의 지난 스캔이 다음 스캔의 범위와 바뀜 표시의 기준이다. */
function ScanHistory({ row }: { row: SourceOut }) {
  const scans = useApi(() => api.sourceScans(row.id), [row.id, row.scanned_at]);
  const gone = row.scan?.gone ?? [];
  return (
    <div style={{ fontSize: 12 }}>
      <p className="mutedtext" style={{ marginTop: 0, fontSize: 11 }}>
        지난번에 받은 화면은 다음 스캔에서 다시 보고, 지난번에 없던 주소(404)는 한 번 쉬어 쪽수 상한을
        살아 있는 화면에 씁니다. 바뀜 표시는 같은 방법으로 한 지난 스캔과 견준 것입니다.
      </p>
      {gone.length > 0 && (
        <>
          <h4 style={{ margin: '6px 0' }}>지난 스캔에 있었는데 이번에 없는 주소</h4>
          <ul className="mono" style={{ fontSize: 11, margin: 0, paddingLeft: 18 }}>
            {gone.map((u) => <li key={u} style={{ wordBreak: 'break-all' }}>{u}</li>)}
          </ul>
        </>
      )}
      <Async state={scans} empty="아직 스캔 이력이 없습니다.">
        {(rows) => (
          <table style={{ marginTop: 8 }}>
            <thead>
              <tr><th>끝난 시각</th><th>방법</th><th>결과</th><th>화면</th><th>파일</th><th>지난번과 견줌</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="mono" style={{ fontSize: 11 }}>{new Date(r.finished_at).toLocaleString('ko-KR')}</td>
                  <td>{r.via === 'browser' ? '브라우저' : '서버'}</td>
                  <td>{r.status === 'done' ? '완료' : <span className="error">실패</span>}</td>
                  <td className="mono">{r.pages}</td>
                  <td className="mono">{r.files}</td>
                  <td style={{ fontSize: 11 }}>
                    {r.status !== 'done'
                      ? (r.error ?? '').slice(0, 120)
                      : r.changes && `새로 ${r.changes.new} · 바뀜 ${r.changes.changed} · ` +
                        `그대로 ${r.changes.same} · 없어짐 ${r.changes.gone}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
    </div>
  );
}

function Shots({ id, pages }: { id: number; pages: SourcePage[] }) {
  const shot = pages.filter((p) => p.shot !== undefined);
  const [urls, setUrls] = useState<Record<number, string>>({});

  useEffect(() => {
    let alive = true;
    const made: string[] = [];
    shot.forEach((p) => {
      api.sourceShotUrl(id, p.shot!).then((u) => {
        made.push(u);
        if (alive) setUrls((cur) => ({ ...cur, [p.shot!]: u }));
        else URL.revokeObjectURL(u);
      }).catch(() => { /* 캡처가 지워졌으면 빈 칸으로 둔다 */ });
    });
    return () => {
      alive = false;
      made.forEach((u) => URL.revokeObjectURL(u));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, shot.map((p) => p.shot).join(',')]);

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 12 }}>
      {shot.map((p) => (
        <figure key={p.shot} style={{ margin: 0 }}>
          {urls[p.shot!]
            ? <img src={urls[p.shot!]} alt={p.title || p.url}
                   style={{ width: '100%', border: '1px solid var(--border)', borderRadius: 4 }} />
            : <div className="mutedtext" style={{ fontSize: 12 }}>불러오는 중…</div>}
          <figcaption style={{ fontSize: 11 }}>{p.title || p.url}</figcaption>
        </figure>
      ))}
    </div>
  );
}

function Endpoints({ row }: { row: SourceOut }) {
  const scan = row.scan!;
  const eps = scan.endpoints ?? [];
  if (!eps.length) {
    return (
      <>
        <p className="mutedtext" style={{ fontSize: 12 }}>OpenAPI 문서를 찾지 못해 응답의 모양만 읽었습니다.</p>
        <pre className="mono" style={{ fontSize: 11 }}>{JSON.stringify(scan.sample_shape, null, 2)}</pre>
      </>
    );
  }
  return (
    <>
      {scan.openapi && <p className="mono mutedtext" style={{ fontSize: 11 }}>OpenAPI: {scan.openapi}</p>}
      <table style={{ fontSize: 12 }}>
        <thead><tr><th>메서드</th><th>경로</th><th>설명</th><th>조건</th><th>응답 항목</th></tr></thead>
        <tbody>
          {eps.map((e) => (
            <tr key={`${e.method} ${e.path}`} style={e.read_only ? undefined : { opacity: 0.6 }}>
              <td className="mono">{e.method}</td>
              <td className="mono">{e.path}</td>
              <td>{e.summary || '—'}{!e.read_only && ' (변경)'}</td>
              <td>{e.params.join(', ') || '—'}</td>
              <td>{e.fields.join(', ') || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function Tools({ row }: { row: SourceOut }) {
  const tools = row.scan!.tools ?? [];
  return (
    <table style={{ fontSize: 12 }}>
      <thead><tr><th>도구</th><th>설명</th><th>인자</th><th>성격</th></tr></thead>
      <tbody>
        {tools.map((t) => (
          <tr key={t.name}>
            <td className="mono">{t.name}</td>
            <td>{t.description || '—'}</td>
            <td>{t.params.join(', ') || '—'}</td>
            <td>{t.read_only ? '조회' : '변경'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Notes({ row }: { row: SourceOut }) {
  const scan = row.scan!;
  return (
    <div style={{ fontSize: 12 }}>
      {scan.notes.length > 0 ? (
        <ul style={{ marginTop: 0, paddingLeft: 18 }}>{scan.notes.map((n) => <li key={n}>{n}</li>)}</ul>
      ) : (
        <p className="mutedtext" style={{ marginTop: 0 }}>특이 사항 없음</p>
      )}
      {row.kind === 'web' && (
        <p className="mutedtext">
          {scan.via === 'browser'
            ? '사용자 브라우저에서 읽음(북마크릿)'
            : `사이트맵 ${scan.sitemap ?? 0}건 · 브라우저 ${scan.browser ? '사용' : '미사용'}`}
        </p>
      )}
      {(scan.skipped ?? []).length > 0 && (
        <>
          <h4>받지 않은 주소</h4>
          <table>
            <thead><tr><th>주소</th><th>이유</th></tr></thead>
            <tbody>
              {scan.skipped!.map((s) => (
                <tr key={s.url}>
                  <td className="mono" style={{ fontSize: 11, wordBreak: 'break-all' }}>{s.url}</td>
                  <td>{s.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
