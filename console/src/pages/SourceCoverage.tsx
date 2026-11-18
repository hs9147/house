import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Async from '../components/Async';
import { api } from '../lib/api';
import { useApi, usePolling } from '../lib/hooks';
import type { CoverageHarness, CoverageItem, CoverageProposal, SourceInventory, SourceOut } from '../lib/types';

const STATUS: Record<CoverageItem['status'], { label: string; cls: string }> = {
  new: { label: '신규', cls: 'info' },
  similar: { label: '비슷함', cls: 'warn' },
  duplicate: { label: '중복', cls: 'dim' },
  managed: { label: '관리 중', cls: 'ok' },
};

/**
 * 정보 현황(데이터 수집의 탭) — 출처마다 지금 관리하는 데이터의 종류·유형과 합계(services/coverage.inventory).
 * 출처 이름을 누르면 그 출처의 「정보 현황」에서 사이트에서 더 받을 수 있는 것을 분석한다.
 */
export function InventoryTab() {
  const navigate = useNavigate();
  const inv = useApi(() => api.sourceInventory(), []);
  return (
    <Async state={inv}>
      {(data: SourceInventory) => (
        <div className="panel">
          <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
            {Object.entries(data.totals).map(([k, v]) => (
              <span key={k} className="status dim">{k} {v.toLocaleString()}</span>
            ))}
            {Object.entries(data.files).map(([k, v]) => (
              <span key={k} className="status dim">파일 · {k} {v.toLocaleString()}</span>
            ))}
          </div>
          <table style={{ fontSize: 12 }}>
            <thead>
              <tr><th>출처</th><th>종류</th><th>수집 화면(유형 · 스킬 · 누적)</th><th>저장한 파일</th><th>API·MCP</th><th>스캔</th></tr>
            </thead>
            <tbody>
              {data.sources.map((s) => (
                <tr key={s.id}>
                  <td>
                    <a href={`#/sources/${s.id}`} onClick={(e) => { e.preventDefault(); navigate(`/sources/${s.id}`); }}>{s.name}</a>
                  </td>
                  <td>{s.kind}</td>
                  <td>
                    {s.screens.length === 0 ? <span className="mutedtext">없음</span> : s.screens.map((sc) => (
                      <div key={sc.url}>
                        {sc.screen} <span className="mutedtext">· {sc.type}{sc.skill && ` · 🧩 ${sc.skill}`}
                          {sc.dataset && ` · ${sc.dataset.rows.toLocaleString()}건(${sc.dataset.columns.length}열, ${sc.dataset.last || '덜 받음'})`}</span>
                      </div>
                    ))}
                  </td>
                  <td>{s.files.length === 0 ? '—' : s.files.map((f) => `${f.label || f.category} ${f.count}`).join(' · ')}</td>
                  <td>{s.kind === 'api' ? `엔드포인트 ${s.endpoints}` : s.kind === 'mcp' ? `도구 ${s.tools}` : '—'}</td>
                  <td className="mutedtext">{s.scanned_at ? new Date(s.scanned_at).toLocaleDateString('ko-KR') : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mutedtext" style={{ fontSize: 11, margin: '8px 0 0' }}>
            출처를 열어 오른쪽 「🔎 정보 현황」에서 「분석」을 누르면, LLM이 사이트에서 받을 수 있는 정보마다 신규인지 판정하고
            신규 화면의 받을 것·조건·스킬 초안까지 제안합니다 — 확정만 하면 됩니다.
          </p>
        </div>
      )}
    </Async>
  );
}

/**
 * 출처 하나의 정보 현황 — 사람 입력 없이 LLM이 만든 제안(신규 판정 → 받을 것·조건·스킬)을 보고 **확정만** 한다.
 * 기본은 제안 전부가 체크돼 있고, 빼고 싶은 것만 끈다.
 */
export function CoveragePanel({ row, onChange }: { row: SourceOut; onChange: (r: SourceOut) => void }) {
  const [state, setState] = useState<{ proposal: CoverageProposal | null; progress: { running: boolean; steps: { text: string }[] };
                                        harness: CoverageHarness } | null>(null);
  const [off, setOff] = useState<Set<string>>(new Set());
  const [error, setError] = useState('');
  const [done, setDone] = useState('');
  const load = async () => setState(await api.sourceCoverage(row.id));
  useEffect(() => { void load(); }, [row.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const running = !!state?.proposal?.running;
  usePolling(load, 2000, running);
  const p = state?.proposal;

  const start = async () => {
    setError('');
    setDone('');
    setOff(new Set());
    try {
      await api.startSourceCoverage(row.id);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const confirm = async () => {
    if (!p?.proposed) return;
    setError('');
    try {
      const out = await api.confirmSourceCoverage(row.id, p.proposed.filter((x) => !off.has(x.url)).map((x) => x.url));
      setDone(`수집 조건에 ${out.added.length}개를 더했습니다${out.skills.length ? ` — 스킬 ${out.skills.join(', ')}` : ''}.`
        + ' 다음 스캔·봇 실행부터 받습니다.');
      onChange(out.source);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="panel">
      <div className="row" style={{ gap: 8, alignItems: 'center' }}>
        <h3 style={{ margin: 0, flex: 1 }}>🔎 정보 현황 — 신규 정보 찾기</h3>
        <button className="small" disabled={running || !row.scan} onClick={() => void start()}>
          {running ? '분석 중…' : p?.proposed ? '다시 분석' : '분석(LLM)'}
        </button>
      </div>
      <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0' }}>
        지난 스캔이 읽은 화면·파일·API마다 이미 관리하는 것과 견줘 신규인지 판정합니다 — 같은 주소, 결과 열의 겹침(Table Union
        Search), 이름 유사도를 규칙으로 먼저 보고, 애매한 것만 LLM이 뜻으로 다시 봅니다. 신규 화면은 받을 것·조회 조건·스킬 초안까지
        LLM이 정해 제안합니다. 입력할 것은 없습니다 — 아래 제안을 보고 확정만 하세요.
      </p>
      {!row.scan && <p className="mutedtext" style={{ fontSize: 12 }}>먼저 스캔하세요.</p>}
      {state?.harness && row.scan && (
        <Harness id={row.id} h={state.harness} onChange={(h) => setState({ ...state, harness: h })} onError={setError} />
      )}
      {running && (
        <pre className="mono" style={{ fontSize: 11, maxHeight: 160, overflow: 'auto', whiteSpace: 'pre-wrap' }}>
          {(state?.progress.steps ?? []).map((s) => s.text).join('\n')}
        </pre>
      )}
      {p?.error && <p className="error">분석 실패 — {p.error}</p>}
      {error && <p className="error">{error}</p>}
      {done && <p style={{ fontSize: 12, color: 'var(--green)' }}>{done}</p>}
      {p?.proposed && (
        <>
          <div className="row" style={{ gap: 6, flexWrap: 'wrap', margin: '6px 0' }}>
            {(Object.keys(STATUS) as CoverageItem['status'][]).map((k) => (
              <span key={k} className={`status ${STATUS[k].cls}`}>{STATUS[k].label} {p.counts?.[k] ?? 0}</span>
            ))}
            <span className="mutedtext" style={{ fontSize: 11 }}>
              판정 {p.judged_by === 'llm' ? '규칙 + LLM' : '규칙'} · {p.created_at && new Date(p.created_at).toLocaleString('ko-KR')}
            </span>
          </div>
          {p.notes?.map((n) => <p key={n} className="mutedtext" style={{ fontSize: 11, margin: '2px 0' }}>{n}</p>)}
          <h4 style={{ margin: '8px 0 4px' }}>제안 — 수집 조건에 더할 신규 화면 {p.proposed.length}</h4>
          {p.proposed.length === 0 ? <p className="mutedtext" style={{ fontSize: 12 }}>더할 신규 화면이 없습니다.</p> : (
            <table style={{ fontSize: 12 }}>
              <thead><tr><th /><th>화면</th><th>받을 것</th><th>조건</th><th>스킬</th></tr></thead>
              <tbody>
                {p.proposed.map((x) => (
                  <tr key={x.url}>
                    <td><input type="checkbox" checked={!off.has(x.url)} aria-label="확정"
                      onChange={(e) => {
                        const next = new Set(off);
                        if (e.target.checked) next.delete(x.url); else next.add(x.url);
                        setOff(next);
                      }} /></td>
                    <td>{x.title}<div className="mutedtext mono" style={{ fontSize: 10, wordBreak: 'break-all' }}>{x.url}</div></td>
                    <td>{x.screen.collect === 'page' ? '페이지(본문)' : '파일·목록'}</td>
                    <td className="mono" style={{ fontSize: 11 }}>
                      {x.screen.conditions.map((c) => `${c.field}=${c.value}`).join(', ') || '—'}
                    </td>
                    <td>{x.screen.skill ? <>🧩 <b className="mono">{x.screen.skill.name}</b>
                      <div className="mutedtext">{x.screen.skill.description}</div></> : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {p.proposed.length > 0 && (
            <button className="small" style={{ marginTop: 6 }} onClick={() => void confirm()}>
              확정 — {p.proposed.length - off.size}개 더하기
            </button>
          )}
          <details style={{ marginTop: 8 }}>
            <summary style={{ cursor: 'pointer', fontSize: 12 }}>판정 전체 {p.items?.length ?? 0}</summary>
            <table style={{ fontSize: 11 }}>
              <tbody>
                {(p.items ?? []).map((i) => (
                  <tr key={`${i.type}:${i.url}`}>
                    <td><span className={`status ${STATUS[i.status].cls}`}>{STATUS[i.status].label}</span></td>
                    <td>{i.type}</td>
                    <td>{i.title}</td>
                    <td className="mutedtext">{i.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </>
      )}
    </div>
  );
}


/**
 * 하네스 — 출처에서 받을 수 있는 것(발견)과 저장소가 가진 것(유형)을 서로 교정하며 되풀이한 결과. 라운드는 스캔·수집이
 * 끝날 때마다 저절로 돈다. 사람은 판정이 틀린 것만 고친다(다음 라운드부터 먼저 쓴다).
 */
function Harness({ id, h, onChange, onError }: {
  id: number; h: CoverageHarness; onChange: (h: CoverageHarness) => void; onError: (e: string) => void;
}) {
  const act = async (fn: () => Promise<CoverageHarness>) => {
    onError('');
    try { onChange(await fn()); } catch (e) { onError((e as Error).message); }
  };
  const r = h.round;
  return (
    <div style={{ margin: '6px 0', padding: 6, border: '1px dashed var(--border)', borderRadius: 4, fontSize: 12 }}>
      <div className="row" style={{ gap: 8, alignItems: 'center' }}>
        <b style={{ flex: 1 }}>하네스 — 출처 ↔ 저장소 교정 라운드 {h.rounds.length}</b>
        <button className="small secondary" onClick={() => void act(() => api.coverageRound(id))}>지금 라운드</button>
      </div>
      {!r ? <p className="mutedtext" style={{ margin: '4px 0' }}>아직 라운드가 없습니다 — 스캔이 끝나면 돕니다.</p> : (
        <>
          <p className="mutedtext" style={{ margin: '4px 0' }}>
            {new Date(r.at).toLocaleString('ko-KR')} · {r.trigger} — 판정 {r.items}개(신규 {r.counts.new} · 비슷함 {r.counts.similar} ·
            중복 {r.counts.duplicate} · 관리 중 {r.counts.managed}) · 지난 라운드와 바뀐 판정 {r.changed}
            {r.changed === 0 ? ' (수렴)' : ''} · 사람이 고친 것 {r.decided}
          </p>
          {h.rounds.length > 1 && (
            <div className="mono mutedtext" style={{ fontSize: 10 }}>
              이력: {h.rounds.map((x) => `신규${x.counts.new}/비슷${x.counts.similar}/공백${x.gaps}/교정${x.issues}`).join(' → ')}
            </div>
          )}
          {!!h.issues?.length && (
            <div style={{ marginTop: 4 }}>
              <b>교정할 것 {h.issues.length}</b> <span className="mutedtext">— 수집이 기대와 다릅니다(판정이 아니라 수집 방법을 고칠 일)</span>
              <ul style={{ margin: '2px 0', paddingLeft: 18 }}>
                {h.issues.map((i, k) => <li key={k}><span className="status warn">{i.kind}</span> {i.screen} — {i.detail}</li>)}
              </ul>
            </div>
          )}
          {!!h.gaps?.length && (
            <div style={{ marginTop: 4 }}>
              <b>출처 미확인 {h.gaps.length}</b> <span className="mutedtext">— 저장소에 되풀이되는데 어느 출처의 화면과도 겹치지 않는 유형</span>
              <ul style={{ margin: '2px 0', paddingLeft: 18 }}>
                {h.gaps.map((g) => <li key={`${g.store}:${g.name}`}><span className="mono">{g.store}</span> · {g.kind} 「{g.name}」 (문서 {g.documents}건)</li>)}
              </ul>
            </div>
          )}
          {!!h.items?.length && (
            <details style={{ marginTop: 4 }}>
              <summary style={{ cursor: 'pointer' }}>판정 고치기 {h.items.filter((i) => i.addable).length}</summary>
              <table style={{ fontSize: 11 }}>
                <tbody>
                  {h.items.filter((i) => i.addable).map((i) => (
                    <tr key={i.url}>
                      <td>
                        <select value={h.decisions[i.url]?.status ?? ''} aria-label="판정"
                          onChange={(e) => void act(() => api.decideCoverage(id, i.url, e.target.value))}>
                          <option value="">규칙대로({STATUS[i.status].label})</option>
                          {(Object.keys(STATUS) as CoverageItem['status'][]).map((k) => (
                            <option key={k} value={k}>{STATUS[k].label}</option>
                          ))}
                        </select>
                      </td>
                      <td>{i.title}</td>
                      <td className="mutedtext">{i.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}
        </>
      )}
    </div>
  );
}
