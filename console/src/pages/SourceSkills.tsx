import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Async from '../components/Async';
import Modal from '../components/Modal';
import { api } from '../lib/api';
import { fmtBytes } from '../lib/format';
import { useApi } from '../lib/hooks';
import type {
  OrgOut, SkillChange, SkillOut, SkillRunResult, SourceOut, SourceQueryCondition, SourceQueryScreen,
  SourceSkill,
} from '../lib/types';

/**
 * 수집 스킬 — 크롤링(스캔)이 읽은 화면을 수집 조건으로 정하고, 그 화면에 이름·용도·입력·출력·공개 범위를 붙이면
 * 에이전트가 골라 부르는 일이 된다(services/skills). 여기는 모든 출처의 스킬을 모아 보고 입력을 넣어 지금 돌려
 * 본다 — 스마트워크 대화와 MCP(/mcp/skills)가 부르는 것과 같은 실행이다(저장소에 쓰지 않는다).
 */
export function SkillCatalog() {
  const navigate = useNavigate();
  const list = useApi(() => api.listSkills(), []);
  const orgs = useApi(() => api.listOrgs(), []);
  const [running, setRunning] = useState<SkillOut | null>(null);
  const orgName = (id: number) => orgs.data?.find((o) => o.id === id)?.name ?? `#${id}`;
  return (
    <div className="panel">
      <Async state={list}>
        {(data) => (
          <>
            {!data.runnable && (
              <p className="error" style={{ fontSize: 12 }}>지금 서버에서 스킬을 돌릴 수 없습니다 — {data.reason}</p>
            )}
            {data.skills.length === 0 ? (
              <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
                아직 스킬이 없습니다 — 출처 상세의 「수집 조건 · 스킬」에서 화면마다 「스킬 초안」을 만들고 저장하세요.
              </p>
            ) : (
              <table style={{ fontSize: 12 }}>
                <thead>
                  <tr><th>스킬</th><th>용도 · 쓸 때</th><th>입력</th><th>결과 열</th><th>공개 범위</th><th /></tr>
                </thead>
                <tbody>
                  {data.skills.map((s) => (
                    <tr key={s.id}>
                      <td>
                        <b className="mono">{s.name}</b>
                        <div className="mutedtext">
                          <a href={`#/sources/${s.source_id}`}
                             onClick={(e) => { e.preventDefault(); navigate(`/sources/${s.source_id}`); }}>{s.source}</a>
                          {' · '}{s.screen}
                        </div>
                        {!s.session && (
                          <div className="mutedtext" title="서버 봇은 맡긴 세션으로 들어갑니다">맡긴 세션 없음 — 로그인 없이 열리는 화면만</div>
                        )}
                      </td>
                      <td>{s.description}{s.when && <div className="mutedtext">쓸 때: {s.when}</div>}</td>
                      <td className="mono">
                        {s.params.length === 0 ? '—' : s.params.map((p) => (
                          <div key={p.name}>{p.name}{p.required ? '*' : ''}{p.default ? ` = ${p.default}` : ''}</div>
                        ))}
                      </td>
                      <td style={{ maxWidth: 220 }} title="다시 받아 본 건에서 열마다 바뀐 비율 — 고정은 지식(신원·기준), 변경은 정보(상태)">
                        {s.outputs.length === 0 ? '—' : s.outputs.map((o, k) => (
                          <span key={o.name}>{k > 0 && ', '}{o.name}
                            {o.volatility === 'fixed' && <span className="mutedtext">(고정)</span>}
                            {o.volatility === 'changing' && <span className="mutedtext">(변경)</span>}
                          </span>
                        ))}
                      </td>
                      <td>{s.audience.length ? s.audience.map(orgName).join(', ') : <span className="mutedtext">관리자만</span>}</td>
                      <td>
                        <button className="small" disabled={!data.runnable} onClick={() => setRunning(s)}>실행</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="mutedtext" style={{ fontSize: 11, margin: '8px 0 0' }}>
              스킬은 스마트워크 대화(도구 skills__list · skills__run)와 MCP 서버 <span className="mono">/mcp/skills</span>에서
              부릅니다. 공개 범위의 조직 사람만 보고 부를 수 있습니다 — 서버 봇은 출처에 맡긴 세션으로 사이트에 들어가므로,
              조직에 열면 그 조직 사람이 그 세션으로 조회한 결과를 받습니다.
            </p>
          </>
        )}
      </Async>
      {running && <SkillRunModal skill={running} onClose={() => setRunning(null)} />}
    </div>
  );
}

function SkillRunModal({ skill, onClose }: { skill: SkillOut; onClose: () => void }) {
  const [params, setParams] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [out, setOut] = useState<SkillRunResult | null>(null);
  const run = async () => {
    setBusy(true);
    setError('');
    try {
      setOut(await api.runSkill(skill.id, Object.fromEntries(Object.entries(params).filter(([, v]) => v.trim()))));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={`스킬 실행 — ${skill.id}`} onClose={onClose} closable={!busy} wide>
      <p style={{ fontSize: 12, marginTop: 0 }}>{skill.description}</p>
      {skill.params.map((p) => (
        <div key={p.name} className="row" style={{ gap: 6, alignItems: 'center', marginBottom: 4 }}>
          <label className="mono" style={{ width: 120, fontSize: 12 }}>{p.name}{p.required ? ' *' : ''}</label>
          <input style={{ flex: 1 }} value={params[p.name] ?? ''} placeholder={p.default ? `기본값: ${p.default}` : p.description}
                 onChange={(e) => setParams({ ...params, [p.name]: e.target.value })} />
        </div>
      ))}
      <div className="row" style={{ gap: 8, alignItems: 'center', margin: '6px 0' }}>
        <button className="small" disabled={busy} onClick={() => void run()}>{busy ? '서버 브라우저가 조회하는 중…' : '실행'}</button>
        <span className="mutedtext" style={{ fontSize: 11 }}>몇십 초에서 몇 분 걸립니다. 같은 입력은 5분 동안 지난 결과를 줍니다.</span>
      </div>
      {error && <p className="error">{error}</p>}
      {/* 서버가 그 사이트에 로그인돼 있지 않다 — 내 브라우저에서(로그인 → 북마크) 돌리는 화면으로 */}
      {error.includes('#/skill-run?') && (
        <a className="button" href={`#/skill-run?${error.split('#/skill-run?')[1].split(/\s/)[0]}`}>
          🔐 내 브라우저로 로그인해 실행
        </a>
      )}
      {out && <SkillResult out={out} />}
    </Modal>
  );
}

export function SkillResult({ out }: { out: SkillRunResult }) {
  return (
    <div style={{ fontSize: 12 }}>
      <p className="mutedtext" style={{ margin: '4px 0' }}>
        {out.cached ? '지난 결과(5분 안 같은 입력)' : `${out.seconds}초`} · 입력{' '}
        {Object.entries(out.params).map(([k, v]) => `${k}=${v || '(빈 값)'}`).join(', ') || '없음'}
      </p>
      {out.lists.map((l) => (
        <div key={l.screen} style={{ marginBottom: 8 }}>
          <b>{l.screen}</b> — {l.got}건{l.total !== null ? `/전체 ${l.total}건` : ''}{l.complete ? ' 빠짐없음' : ''}
          {l.rows.length > 0 && (
            <div style={{ overflow: 'auto', maxHeight: 300 }}>
              <table>
                <thead><tr>{l.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                <tbody>
                  {l.rows.map((r, i) => (
                    <tr key={i}>{l.columns.map((c) => <td key={c}>{r[c] === null || r[c] === undefined ? '' : String(r[c])}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
      {out.pages.map((p, i) => (
        <details key={i}><summary>페이지 · {p.title || p.url}</summary>
          <pre className="mono" style={{ whiteSpace: 'pre-wrap', maxHeight: 300, overflow: 'auto' }}>{p.markdown}</pre>
        </details>
      ))}
      {out.files.map((f) => (
        <details key={f.name}><summary>파일 · {f.name} ({fmtBytes(f.size)})</summary>
          <pre className="mono" style={{ whiteSpace: 'pre-wrap', maxHeight: 300, overflow: 'auto' }}>{f.text || '(본문을 읽지 못했습니다)'}</pre>
        </details>
      ))}
      {out.notes.map((n) => <p key={n} className="mutedtext" style={{ margin: '2px 0' }}>{n}</p>)}
    </div>
  );
}

/**
 * 화면 하나의 스킬 — 수집 조건의 그 화면 카드 안에서 고친다(저장은 수집 조건과 함께). 스킬은 「🔎 정보 현황」의 제안이나
 * 「🧩 스킬 작업」 대화가 만든다 — 여기서는 확정 전에 손볼 것만 고친다. 입력은 조건 값의 {이름} 자리다.
 */
export function SkillEditor({ skill, conditions, orgs, onChange }: {
  skill?: SourceSkill;
  conditions: SourceQueryCondition[];
  orgs: OrgOut[];
  onChange: (skill: SourceSkill | undefined, conditions?: SourceQueryCondition[]) => void;
}) {
  if (!skill) {
    return (
      <p className="mutedtext" style={{ fontSize: 11, margin: '6px 0 0' }}>
        스킬 없음 — 위 대화에 「이 화면의 스킬을 만들어 줘」라고 하거나, 「🔎 정보 현황」 제안으로 만듭니다.
      </p>
    );
  }
  const set = (next: Partial<SourceSkill>) => onChange({ ...skill, ...next });
  const used = new Set(conditions.flatMap((c) => [...(c.value || '').matchAll(/\{([^{}]{1,40})\}/g)].map((m) => m[1])));
  const missing = [...used].filter((u) => !skill.params.some((p) => p.name === u));
  return (
    <div style={{ marginTop: 6, padding: 6, borderRadius: 4, background: 'rgba(56,189,248,0.06)', fontSize: 12 }}>
      <div className="row" style={{ gap: 6, alignItems: 'center' }}>
        <b>🧩 스킬</b>
        <input value={skill.name} placeholder="이름 — 예: 공지-검색" aria-label="스킬 이름" style={{ width: 180 }}
               onChange={(e) => set({ name: e.target.value })} />
        <div className="spacer" />
        <button className="small secondary" onClick={() => onChange(undefined)}>스킬 빼기</button>
      </div>
      <textarea value={skill.description} rows={2} placeholder="용도 — 돌려주는 것(어떤 목록, 행 하나가 무엇인지)"
                aria-label="스킬 용도" style={{ width: '100%', marginTop: 4, fontFamily: 'inherit' }}
                onChange={(e) => set({ description: e.target.value })} />
      <input value={skill.when} placeholder="쓸 때 — 에이전트가 이 스킬을 고를 질문" aria-label="쓸 때"
             style={{ width: '100%', marginTop: 4 }} onChange={(e) => set({ when: e.target.value })} />
      <div style={{ marginTop: 4 }}>
        입력 <span className="mutedtext">— 조건 값에 <span className="mono">{'{이름}'}</span>으로 쓴 자리. 부를 때 값을 받고, 수집은 기본값으로 돕니다</span>
        {skill.params.map((p, k) => (
          <div key={k} className="row" style={{ gap: 4, marginTop: 2, alignItems: 'center' }}>
            <input className="mono" value={p.name} placeholder="이름" aria-label="입력 이름" style={{ width: 110 }}
                   onChange={(e) => set({ params: skill.params.map((x, m) => (m === k ? { ...x, name: e.target.value } : x)) })} />
            <input value={p.description} placeholder="설명" aria-label="입력 설명" style={{ flex: 1 }}
                   onChange={(e) => set({ params: skill.params.map((x, m) => (m === k ? { ...x, description: e.target.value } : x)) })} />
            <input value={p.default} placeholder="기본값" aria-label="기본값" style={{ width: 110 }}
                   onChange={(e) => set({ params: skill.params.map((x, m) => (m === k ? { ...x, default: e.target.value } : x)) })} />
            <label><input type="checkbox" checked={p.required}
                          onChange={(e) => set({ params: skill.params.map((x, m) => (m === k ? { ...x, required: e.target.checked } : x)) })} /> 필수</label>
            {!used.has(p.name) && <span className="status warn" title="조건 값에 이 자리가 없습니다">안 씀</span>}
            <button className="small secondary" onClick={() => set({ params: skill.params.filter((_, m) => m !== k) })}>×</button>
          </div>
        ))}
        <button className="small secondary" style={{ marginTop: 2 }}
                onClick={() => set({ params: [...skill.params, { name: '', description: '', default: '', required: false }] })}>
          입력 더하기
        </button>
        {missing.length > 0 && (
          <div style={{ marginTop: 2 }}>
            <span className="status warn">입력 없음</span> 조건에 쓴 자리가 입력에 없습니다: <span className="mono">{missing.join(', ')}</span>
            {' '}<button className="small secondary" onClick={() => set({ params: [...skill.params, ...missing.map((name) => ({
              name, description: `「${conditions.find((c) => c.value.includes(`{${name}}`))?.field ?? name}」 칸에 넣을 값`,
              default: '', required: false }))] })}>입력으로 더하기</button>
          </div>
        )}
      </div>
      <div style={{ marginTop: 4 }}>
        결과 열 <span className="mutedtext">(쉼표 — 비우면 지난 수집이 받은 열)</span>
        <input value={skill.outputs.map((o) => o.name).join(', ')} aria-label="결과 열" style={{ width: '100%' }}
               onChange={(e) => set({ outputs: e.target.value.split(',').map((x) => x.trim()).filter(Boolean)
                 .map((name) => ({ name, description: skill.outputs.find((o) => o.name === name)?.description ?? '' })) })} />
      </div>
      <div style={{ marginTop: 4 }}>
        공개 범위 <span className="mutedtext">— 고른 조직 사람(과 그 사람의 에이전트)이 부릅니다. 아무것도 고르지 않으면 관리자만</span>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          {orgs.map((o) => (
            <label key={o.id}>
              <input type="checkbox" checked={skill.audience.includes(o.id)}
                     onChange={(e) => set({ audience: e.target.checked ? [...skill.audience, o.id]
                       : skill.audience.filter((a) => a !== o.id) })} /> {o.name}
            </label>
          ))}
        </div>
      </div>
    </div>
  );
}

/** 수집 조건 편집본 — 출처 상세의 왼쪽(수집 조건)과 오른쪽(스킬 작업)이 함께 쓴다(SourceDetail.useQueryDraft). */
export interface QueryDraft {
  screens: SourceQueryScreen[] | null;
  busy: boolean;
  saved: boolean;
  error: string;
  savedUrls: Set<string>;
  edit: (next: SourceQueryScreen[]) => void;
  setScreen: (i: number, s: SourceQueryScreen) => void;
  save: () => Promise<void>;
}

type ChatTurn = { role: 'user' | 'assistant'; content: string; changes?: SkillChange[]; notes?: string[]; applied?: boolean };

const STARTERS = [
  '화면마다 스킬 초안을 만들어 줘',
  '부를 때 바꿀 칸(입력)을 골라 줘 — 날짜는 수집 조건대로 두고',
  '에이전트가 언제 이 스킬을 써야 하는지(쓸 때)를 업무 말로 다듬어 줘',
];

/** 바뀐 것 한 줄 — 화면 이름 · 스킬 이름 · 입력 · 조건 */
function changeLine(c: SkillChange, screens: SourceQueryScreen[]): string {
  const name = c.screen ?? screens.find((s) => s.url === c.url)?.screen ?? c.url;
  if (c.skill === null) return `${name}: 스킬 빼기`;
  const skill = c.skill
    ? ` → 🧩 ${c.skill.name}${c.skill.params.length ? ` (입력 ${c.skill.params.map((p) => p.name).join(', ')})` : ''}`
    : '';
  return `${c.new ? '+ ' : ''}${name}${skill} · 조건 ${c.conditions.map((x) => `${x.field}=${x.value}`).join(', ') || '없음'}`;
}

/**
 * 스킬 작업 — 출처 상세의 오른쪽(왼쪽은 폭이 좁다). 크롤링이 읽은 화면을 LLM과 **대화로** 스킬로 정리한다: 말하면
 * LLM이 답과 고칠 것을 내고(사실에 있는 칸·열만 — 서버가 다시 본다), 「적용」하면 편집본에 들어가며, 아래 카드에서
 * 손으로도 고친 뒤 저장한다. 대화는 이 화면만 들고 있다(저장하지 않는다 — 남는 것은 저장한 스킬이다).
 */
export function SkillWorkspace({ row, q }: { row: SourceOut; q: QueryDraft }) {
  const orgs = useApi(() => api.listOrgs(), []);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const screens = q.screens ?? [];

  const send = async (said: string) => {
    if (!said.trim() || busy) return;
    const next: ChatTurn[] = [...turns, { role: 'user', content: said.trim() }];
    setTurns(next);
    setText('');
    setBusy(true);
    setError('');
    try {
      const out = await api.chatSourceSkill(row.id, next.map(({ role, content }) => ({ role, content })),
        screens.map(({ why: _why, ...s }) => s));
      setTurns([...next, { role: 'assistant', content: out.reply, changes: out.changes, notes: out.notes }]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const apply = (at: number) => {
    let next = [...screens];
    for (const c of turns[at].changes ?? []) {
      const i = next.findIndex((s) => s.url === c.url);
      const base: SourceQueryScreen = i >= 0 ? next[i] : {
        screen: c.screen ?? c.url, url: c.url, conditions: [],
        ...(c.path ? { path: c.path, start: c.start, frame: c.frame } : {}),
      };
      const { skill: _old, ...rest } = base;
      const kept = c.skill === null ? undefined : c.skill ?? base.skill;
      const s: SourceQueryScreen = { ...rest, conditions: c.conditions, ...(kept ? { skill: kept } : {}) };
      next = i >= 0 ? next.map((x, j) => (j === i ? s : x)) : [...next, s];
    }
    q.edit(next);
    setTurns(turns.map((t, j) => (j === at ? { ...t, applied: true } : t)));
  };

  return (
    <>
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>🧩 스킬 작업 — LLM과 대화로 정리</h3>
        <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
          크롤링이 읽은 화면(칸·결과 열·지난 수집)을 보고 LLM이 스킬(이름·용도·쓸 때·입력·결과 열)을 제안합니다. 고칠 것은
          「적용」해야 편집본에 들어가고 「저장」해야 남습니다 — 왼쪽 수집 조건과 같은 편집본입니다. 공개 범위는 아래 카드에서
          직접 고릅니다.
        </p>
        <div style={{ maxHeight: 420, overflow: 'auto', margin: '6px 0' }}>
          {turns.length === 0 && (
            <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
              {STARTERS.map((t) => (
                <button key={t} className="small secondary" disabled={busy || !screens.length}
                        onClick={() => void send(t)}>{t}</button>
              ))}
              {!screens.length && (
                <span className="mutedtext" style={{ fontSize: 11 }}>
                  수집 조건에 화면이 없습니다 — 「🔎 정보 현황」에서 분석하고 제안을 확정하세요.
                </span>
              )}
            </div>
          )}
          {turns.map((t, i) => (
            <div key={i} style={{ margin: '6px 0', padding: 6, borderRadius: 4, fontSize: 12,
                                  background: t.role === 'user' ? 'rgba(255,255,255,0.04)' : 'rgba(56,189,248,0.06)' }}>
              <b>{t.role === 'user' ? '나' : 'LLM'}</b>
              <div style={{ whiteSpace: 'pre-wrap' }}>{t.content}</div>
              {!!t.changes?.length && (
                <div style={{ marginTop: 4 }}>
                  {t.changes.map((c, k) => (
                    <div key={k} className="mono" style={{ fontSize: 11 }}>{changeLine(c, screens)}</div>
                  ))}
                  <button className="small" style={{ marginTop: 4 }} disabled={t.applied} onClick={() => apply(i)}>
                    {t.applied ? '적용함' : `적용(${t.changes.length})`}
                  </button>
                </div>
              )}
              {t.notes?.map((n) => <div key={n} className="mutedtext" style={{ fontSize: 11 }}>{n}</div>)}
            </div>
          ))}
          {busy && <p className="mutedtext" style={{ fontSize: 12 }}>LLM이 정리하는 중…</p>}
        </div>
        <div className="row" style={{ gap: 6, alignItems: 'flex-end' }}>
          <textarea value={text} rows={2} style={{ flex: 1, fontFamily: 'inherit' }} aria-label="스킬 대화"
                    placeholder="예: 공지 조회는 구분을 입력으로 받고, 쓸 때는 '최근 공지를 물을 때'로 해 줘 (Ctrl+Enter로 보내기)"
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void send(text); }} />
          <button className="small" disabled={busy || !text.trim()} onClick={() => void send(text)}>보내기</button>
          {turns.length > 0 && (
            <button className="small secondary" disabled={busy} onClick={() => setTurns([])}>새 대화</button>
          )}
        </div>
        {error && <p className="error">{error}</p>}
      </div>
      <div className="panel">
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          <h3 style={{ margin: 0, flex: 1 }}>화면별 스킬</h3>
          {!q.saved && q.screens && <span className="status warn">저장 전</span>}
          <button className="small" disabled={q.busy || !q.screens} onClick={() => void q.save()}>저장</button>
        </div>
        {q.saved && <p className="mutedtext" style={{ fontSize: 11 }}>저장했습니다 — 스킬 탭·스마트워크·MCP에 바로 보입니다.</p>}
        {q.error && <p className="error">{q.error}</p>}
        {screens.length === 0 && <p className="mutedtext" style={{ fontSize: 12 }}>수집 조건에 화면이 없습니다.</p>}
        {screens.map((s, i) => (
          <div key={s.url} style={{ border: '1px solid var(--border)', borderRadius: 4, padding: 6, marginTop: 6 }}>
            <b style={{ fontSize: 12 }}>{s.screen}</b>
            <span className="mutedtext mono" style={{ fontSize: 10, marginLeft: 6 }}>
              조건 {s.conditions.map((c) => `${c.field}=${c.value}`).join(', ') || '없음'}
            </span>
            <SkillEditor skill={s.skill} conditions={s.conditions} orgs={orgs.data ?? []}
              onChange={(skill, conditions) => {
                const { skill: _old, ...rest } = s;
                q.setScreen(i, { ...rest, ...(skill ? { skill } : {}), ...(conditions ? { conditions } : {}) });
              }} />
          </div>
        ))}
      </div>
    </>
  );
}
