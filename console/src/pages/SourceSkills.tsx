import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Async from '../components/Async';
import Modal from '../components/Modal';
import { api } from '../lib/api';
import { fmtBytes } from '../lib/format';
import { useApi } from '../lib/hooks';
import type { OrgOut, SkillOut, SkillRunResult, SourceQueryCondition, SourceSkill } from '../lib/types';

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
                      <td style={{ maxWidth: 220 }}>{s.outputs.map((o) => o.name).join(', ') || '—'}</td>
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
      {out && <SkillResult out={out} />}
    </Modal>
  );
}

function SkillResult({ out }: { out: SkillRunResult }) {
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

const EMPTY: SourceSkill = { name: '', description: '', when: '', params: [], outputs: [], audience: [] };

/**
 * 화면 하나의 스킬 — 수집 조건의 그 화면 카드 안에서 고친다(저장은 수집 조건과 함께). 입력은 조건 값의 {이름}
 * 자리다 — 부를 때 값을 받고, 수집(북마크·봇)은 기본값으로 돈다.
 */
export function SkillEditor({ sourceId, url, skill, conditions, saved, orgs, onChange }: {
  sourceId: number;
  url: string;
  skill?: SourceSkill;
  conditions: SourceQueryCondition[];
  saved: boolean;          // 이 화면이 저장돼 있어야 초안을 만든다(서버가 저장한 화면을 읽는다)
  orgs: OrgOut[];
  onChange: (skill: SourceSkill | undefined, conditions?: SourceQueryCondition[]) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [via, setVia] = useState('');
  const makeDraft = async () => {
    setBusy(true);
    setError('');
    try {
      const out = await api.draftSourceSkill(sourceId, url);
      setVia(out.via === 'llm' ? 'LLM 초안' : '규칙 초안(LLM 없음)');
      onChange({ ...out.skill, audience: skill?.audience ?? [] }, out.conditions);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  if (!skill) {
    return (
      <div className="row" style={{ gap: 6, alignItems: 'center', marginTop: 6, fontSize: 11 }}>
        <button className="small secondary" disabled={busy || !saved} onClick={() => void makeDraft()}>
          {busy ? '…' : '스킬 초안 만들기'}
        </button>
        <button className="small secondary" onClick={() => onChange({ ...EMPTY, name: '' })}>직접 쓰기</button>
        {!saved && <span className="mutedtext">수집 조건을 저장한 뒤에 초안을 만듭니다</span>}
        {error && <span className="error">{error}</span>}
      </div>
    );
  }
  const set = (next: Partial<SourceSkill>) => onChange({ ...skill, ...next });
  const used = new Set(conditions.flatMap((c) => [...(c.value || '').matchAll(/\{([^{}]{1,40})\}/g)].map((m) => m[1])));
  return (
    <div style={{ marginTop: 6, padding: 6, borderRadius: 4, background: 'rgba(56,189,248,0.06)', fontSize: 12 }}>
      <div className="row" style={{ gap: 6, alignItems: 'center' }}>
        <b>🧩 스킬</b>
        <input value={skill.name} placeholder="이름 — 예: 공지-검색" aria-label="스킬 이름" style={{ width: 180 }}
               onChange={(e) => set({ name: e.target.value })} />
        {via && <span className="mutedtext">{via}</span>}
        <div className="spacer" />
        <button className="small secondary" disabled={busy || !saved} onClick={() => void makeDraft()}>{busy ? '…' : '초안 다시'}</button>
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
      {error && <p className="error">{error}</p>}
    </div>
  );
}
