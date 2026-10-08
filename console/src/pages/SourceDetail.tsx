import { useEffect, useId, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import Async from '../components/Async';
import Split from '../components/Split';
import Tabs from '../components/Tabs';
import { api } from '../lib/api';
import { useApi, usePolling } from '../lib/hooks';
import scanAgent from '../lib/scanAgent.js?raw';
import type {
  SourceCapabilities, SourceCategory, SourceChange, SourceFile, SourceMenuItem, SourceOut, SourcePage,
  SourceSaveResult, SourceSaveTarget, SourceTarget, StorageStore,
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
 * 정보 출처 상세 — 왼쪽은 사람이 할 일(스캔·헤더·저장 결정), 오른쪽은 스캔이 읽은 것.
 *
 * 저장 위치는 유형(조회 정보 정리·문서·표·발표 파일)마다 제안을 **고칠 수 있는 입력값**으로 채워
 * 둔다. 그대로 누르면 제안대로, 고치면 고친 대로 저장된다 — 새 저장소면 서버가 폴더를 만들고
 * .env에 넣어 재시작 없이 보이게 한다. 같은 자리에 다시 저장하면 바뀐 것만 덮어쓴다.
 */
export default function SourceDetail() {
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const [row, setRow] = useState<SourceOut | null>(null);
  const [error, setError] = useState('');
  const caps = useApi(() => api.sourceCapabilities(), []);
  const first = useApi(() => api.getSource(id), [id]);

  useEffect(() => { if (first.data) setRow(first.data); }, [first.data]);
  usePolling(async () => setRow(await api.getSource(id)), 2000, row?.status === 'scanning');

  const rescan = async () => {
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
        <div className="row" style={{ gap: 8, marginTop: 8 }}>
          <button className="small" disabled={row.status === 'scanning'} onClick={rescan}>
            {row.status === 'scanning' ? '스캔 중…' : row.scan ? '다시 스캔' : '스캔'}
          </button>
        </div>
        {row.status === 'failed' && row.error && <p className="error">{row.error}</p>}
        {error && <p className="error">{error}</p>}
      </div>
      {row.kind === 'web' && <BrowserScanPanel row={row} onChange={setRow} />}
      <HeadersPanel row={row} onChange={setRow} />
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

// 북마크에 통째로 들어간다 — 주석과 들여쓰기만 덜어 낸다(코드에 줄 주석 //는 쓰지 않았다).
const SCAN_CODE = scanAgent.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\n\s+/g, '\n').trim();

// 북마크는 출처마다 하나 — 이름으로 구분하고, 다른 사이트에서 누르면 읽지 않는다.
function scanCode(row: SourceOut): string {
  const source = { id: row.id, name: row.name, origin: new URL(row.url).origin };
  return SCAN_CODE.replace('__GPAX_SOURCE__', JSON.stringify(source));
}

/**
 * SSO 뒤의 사이트 — 서버는 로그인할 수 없으니 사용자가 로그인한 탭에서 북마크릿이 읽는다.
 * 결과는 파일로 오간다: 운영 콘솔은 http라 https 사이트의 페이지가 이쪽을 부를 수 없다.
 */
function BrowserScanPanel({ row, onChange }: { row: SourceOut; onChange: (r: SourceOut) => void }) {
  const link = useRef<HTMLAnchorElement>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const code = scanCode(row);

  // React는 javascript: href를 경고한다 — DOM에 직접 넣는다.
  useEffect(() => { link.current?.setAttribute('href', `javascript:${encodeURIComponent(code)}`); }, [code]);

  const copy = () => {
    // http에서는 navigator.clipboard가 없다 — 고른 글을 복사하는 옛 방식으로.
    const area = document.createElement('textarea');
    area.value = code;
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    setCopied(ok);
    if (!ok) setError('복사하지 못했습니다 — 북마크로 끌어다 놓아 쓰세요.');
  };

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    setError('');
    try {
      onChange(await api.uploadBrowserScan(row.id, file));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>브라우저에서 스캔</h3>
      <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
        사내 SSO로 로그인하는 사이트는 서버가 들어갈 수 없습니다. 로그인한 내 브라우저에서 읽어 옵니다 —
        세션은 브라우저 밖으로 나가지 않고, 입력값·쿠키는 읽지 않습니다.
      </p>
      <ol style={{ fontSize: 12, paddingLeft: 18, margin: '6px 0' }}>
        <li>
          <a ref={link} className="mono" onClick={(e) => e.preventDefault()}
             style={{ padding: '2px 6px', border: '1px solid var(--border)', borderRadius: 4, cursor: 'grab' }}>
            GPAX 스캔 · {row.name}
          </a>
          {' '}을 북마크바로 끌어다 놓습니다. 이 출처 전용입니다 — 다른 사이트에서 누르면 읽지 않습니다(또는{' '}
          <button className="small secondary" onClick={copy}>{copied ? '복사됨' : '코드 복사'}</button>
          {' '}후 개발자 도구 Console에 붙여 넣기).
        </li>
        <li>
          <span className="mono">{row.url}</span>에 로그인하고, 읽힐 화면(메뉴를 펼친 상태, 보고 싶은 탭)을 엽니다.
        </li>
        <li>
          북마크를 누르면 메뉴와 열린 화면을 읽어{' '}
          <span className="mono">gpax-scan-{row.id}-*.json</span>을 내려받습니다.
        </li>
        <li>
          그 파일을 올립니다:{' '}
          <input type="file" accept=".json,application/json" disabled={busy || row.status === 'scanning'}
                 onChange={(e) => { upload(e.target.files?.[0]); e.target.value = ''; }} />
        </li>
      </ol>
      <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
        메뉴의 GET 링크는 최대 25곳까지 더 열어 보고(로그아웃·삭제는 건너뜀), 스크립트로만 여는 메뉴는
        지금 열려 있는 화면만 읽습니다. 화면에 걸린 문서·표·발표 파일은 20개(하나 10MB, 모두 50MB)까지
        함께 받습니다. 화면 캡처는 없습니다.
      </p>
      {busy && <p className="mutedtext" style={{ fontSize: 12 }}>올리는 중…</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function HeadersPanel({ row, onChange }: { row: SourceOut; onChange: (r: SourceOut) => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState('');
  const [error, setError] = useState('');

  const apply = async (headers: string) => {
    setError('');
    try {
      onChange(await api.updateSource(row.id, { headers }));
      setEditing(false);
      setText('');
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>요청 헤더</h3>
      <p style={{ fontSize: 12, margin: '4px 0' }}>
        {row.headers.length ? row.headers.map((h) => <code key={h} style={{ marginRight: 6 }}>{h}</code>) : '없음'}
      </p>
      <p className="mutedtext" style={{ fontSize: 11 }}>
        값은 다시 보여 주지 않습니다. 세션이 만료됐으면 새 값으로 바꾸고 다시 스캔하세요.
      </p>
      {editing ? (
        <>
          <textarea className="mono" rows={3} style={{ width: '100%' }} value={text}
                    placeholder={'Cookie: SESSION=...\nAuthorization: Bearer ...'}
                    onChange={(e) => setText(e.target.value)} />
          <div className="row" style={{ gap: 8, marginTop: 6 }}>
            <button className="small" disabled={!text.trim()} onClick={() => apply(text)}>바꾸기</button>
            <button className="small secondary" onClick={() => setEditing(false)}>취소</button>
          </div>
        </>
      ) : (
        <div className="row" style={{ gap: 8 }}>
          <button className="small secondary" onClick={() => setEditing(true)}>
            {row.headers.length ? '헤더 바꾸기' : '헤더 넣기'}
          </button>
          {row.headers.length > 0 && (
            <button className="small secondary"
                    onClick={() => window.confirm('헤더를 모두 지웁니까?') && apply('')}>지우기</button>
          )}
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

interface Pick {
  mode: 'existing' | 'new' | 'skip';
  existing: string;
  name: string;
  path: string;
}

function pickOf(t: SourceTarget): Pick {
  return t.mode === 'existing'
    ? { mode: 'existing', existing: t.store, name: '', path: '' }
    : { mode: 'new', existing: '', name: t.store, path: t.path ?? '' };
}

function SavePanel({ row, onSaved }: { row: SourceOut; onSaved: (r: SourceSaveResult) => void }) {
  const proposal = row.proposal!.targets;
  const categories = CATEGORY_ORDER.filter((c) => proposal[c]);
  const stores = useApi(() => api.listStorageStores(), []);
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
        : { mode: 'new', store: p.name.trim(), path: p.path.trim() };
  });
  const chosen = categories.filter((c) => targets[c]!.mode !== 'skip');
  const ready = chosen.length > 0 && chosen.every((c) => {
    const t = targets[c]!;
    return t.store && (t.mode !== 'new' || t.path);
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
      '폴더를 만들고 .env의 PAAS_DOC_ROOTS에 추가합니다(이전 파일은 .env.bak).')) return;
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
                      pick={picks[c]!} writable={writable} onChange={(patch) => set(c, patch)} />
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

function TargetPicker({ label, proposal, pick, writable, onChange }: {
  label: string; proposal: SourceTarget; pick: Pick; writable: StorageStore[];
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
