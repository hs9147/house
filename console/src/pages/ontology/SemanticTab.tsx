import { useEffect, useMemo, useState } from 'react';
import { api } from '../../lib/api';
import { useApi, usePolling } from '../../lib/hooks';
import { runTask } from '../../lib/tasks';
import type {
  SemanticForm, SemanticHit, SemanticMapping, SemanticNeighbors, SemanticReview, SemanticSchema, SemanticStatus,
} from '../../lib/types';

/**
 * 내용 분석 — 문서의 **형식**(제목·표·조문)이 아니라 **내용**에서 LLM이 개념과 관계를 뽑는다(services/semantic).
 *
 * 네 단계를 위에서 아래로 한다. 한 번 정해 많은 문서에 퍼지는 것(스키마·양식 대응표·묶기)은 LLM이 제안하면
 * **LLM이 검토하고** 그 의견으로 한 번 고친 안을 보여 준다 — 확정은 관리자가 한다. 문서마다의 추출은 기계
 * 검사(근거 문장이 원문에 있는가)를 전부에 걸고 LLM 검토는 표본만 한다. 표본 오류율이 기준을 넘으면 멈춘다.
 *
 * 형식 그래프(정보 조회 탭)는 그대로 있다 — 이 탭이 만드는 것은 따로 저장되는 내용 그래프다.
 */
export default function SemanticTab() {
  const overview = useApi(() => api.ontologyOverview(), []);
  const stores = useMemo(() => (overview.data?.stores ?? []).filter((s) => s.extracted > 0), [overview.data]);
  const [store, setStore] = useState('');
  const [status, setStatus] = useState<SemanticStatus | null>(null);
  const [forms, setForms] = useState<SemanticForm[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    if (!store && stores.length > 0) setStore(stores[0].store);
  }, [stores, store]);

  const reload = async () => {
    if (!store) return;
    try {
      const [s, f] = await Promise.all([api.semanticStatus(store), api.semanticForms(store)]);
      setStatus(s);
      setForms(f.forms);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  useEffect(() => { setStatus(null); setForms([]); void reload(); }, [store]); // eslint-disable-line react-hooks/exhaustive-deps
  // 제안(뒤에서 LLM)이나 분석이 도는 동안만 다시 읽는다.
  usePolling(reload, 3000, !!status && (status.task.running || status.running));

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setError('');
    setNotice('');
    try {
      await fn();
      setNotice(label);
      await reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // 오래 걸리는 일(LLM 여러 번)은 **작업 트레이**에서 진행을 보인다 — 폴더 업로드와 같은 자리. 서버가 뒤에서 돌고
  // 단계·문구를 남기므로(semantic.submit) 브라우저는 그것을 읽어 옮긴다. 다른 메뉴로 가도 트레이에서 계속 보인다.
  const track = (label: string, start: () => Promise<unknown>) => {
    setError('');
    setNotice('');
    const where = store;
    runTask(`semantic:${where}`, { label: `${label} · ${where}`, to: '/ontology/semantic' }, async (h) => {
      await start();
      if (where === store) await reload();   // 탭도 「진행 중」으로 — 버튼이 잠기고 화면이 따라 갱신된다
      for (;;) {
        await new Promise((ok) => setTimeout(ok, 2000));
        const s = await api.semanticStatus(where);
        const t = s.task;
        h.set({ progress: t.progress || '진행 중…', sent: t.step ?? undefined, total: t.steps ?? undefined });
        if (t.done && !t.running) {
          if (t.error) throw new Error(t.error);
          h.set({ progress: '끝났습니다 — 결과를 확인하세요' });
          break;
        }
      }
      if (where === store) await reload();
    }).catch((e) => setError((e as Error).message));
  };
  // 문서 분석 — 분석한 문서 수로 진행을 보인다. 멈춤 조건에 걸리면 그 사유로 끝난다.
  const trackAnalysis = (action: 'start' | 'resume') => {
    setError('');
    setNotice('');
    const where = store;
    runTask(`semantic-analyze:${where}`, { label: `내용 분석 · ${where}`, to: '/ontology/semantic' }, async (h) => {
      await api.semanticAnalyze(where, action);
      if (where === store) await reload();
      for (;;) {
        await new Promise((ok) => setTimeout(ok, 3000));
        const s = await api.semanticStatus(where);
        const done = Object.values(s.analyzed).reduce((n, v) => n + v, 0);
        h.set({ sent: done, total: s.documents,
                progress: `${done.toLocaleString()} / ${s.documents.toLocaleString()}건 — LLM ${s.analyzed.ok ?? 0} · `
                  + `양식 ${s.analyzed.form ?? 0} · 실패 ${s.analyzed.failed ?? 0}` });
        if (!s.running) {
          if (s.paused) throw new Error(`멈춤 — ${s.paused}`);
          break;
        }
      }
      if (where === store) await reload();
    }).catch((e) => setError((e as Error).message));
  };

  const busy = !!status?.task.running;
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>내용 분석</h2>
      <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
        문서의 형식이 아니라 내용에서 개념(협력사·품목·계약 등)과 관계를 기본 LLM으로 뽑습니다. 스키마·양식 대응표·묶기는
        LLM이 제안한 것을 LLM이 다시 검토해 고친 안을 보여 드리고, 확정은 관리자가 합니다. 형식 그래프(정보 조회)는
        그대로 있습니다.
      </p>
      <div className="row" style={{ gap: 8, marginBottom: 8 }}>
        <label className="field">
          저장소
          <select value={store} onChange={(e) => setStore(e.target.value)}>
            {stores.map((s) => <option key={s.store} value={s.store}>{s.store} ({s.extracted.toLocaleString()}건)</option>)}
          </select>
        </label>
        {status?.task.label && (
          <span className="mutedtext" style={{ fontSize: 12, alignSelf: 'flex-end' }}>
            {status.task.running ? `⏳ ${status.task.label} 중… (LLM 여러 번 — 몇 분 걸릴 수 있습니다)`
              : status.task.error ? `❌ ${status.task.label}: ${status.task.error}` : `✅ ${status.task.label} 끝`}
          </span>
        )}
      </div>
      {stores.length === 0 && !overview.loading && <p className="mutedtext">본문을 읽어 낸 저장소가 없습니다 — 먼저 색인하세요.</p>}
      {notice && <p className="mutedtext" style={{ fontSize: 12 }}>{notice}</p>}
      {error && <p className="error">{error}</p>}
      {status && (
        <>
          <SchemaStep status={status} busy={busy}
            onPropose={() => track('스키마 제안·검토', () => api.semanticProposeSchema(store))}
            onConfirm={(s) => act('스키마를 확정했습니다 — ② 양식과 ③ 분석으로 넘어가세요.', () => api.semanticConfirmSchema(store, s))} />
          {status.schema && (
            <>
              <FormsStep forms={forms} schemaVersion={status.schema.version ?? ''} busy={busy}
                onPropose={(f) => track('양식 대응 제안·검토', () => api.semanticProposeForm(store, f))}
                onConfirm={(f, m) => act('대응표를 확정했습니다 — 분석하면 이 양식의 모든 문서에 규칙으로 적용됩니다.',
                  () => api.semanticConfirmForm(store, f, m))} />
              <AnalyzeStep status={status}
                onAction={(a) => (a === 'stop'
                  ? act('멈추라고 했습니다(지금 문서까지는 끝냅니다).', () => api.semanticAnalyze(store, a))
                  : trackAnalysis(a))} />
              <MergeStep status={status} busy={busy}
                onMerge={() => track('같은 개체 묶기·검토', () => api.semanticMerge(store))}
                onUnmerge={(cls, alias) => act(`'${alias}' 묶음을 풀었습니다.`, () => api.semanticUnmerge(store, cls, alias))} />
              <Explore store={store} schema={status.schema} />
            </>
          )}
        </>
      )}
    </div>
  );
}

function Issues({ review }: { review: SemanticReview }) {
  const issues = review.issues ?? [];
  return (
    <div style={{ fontSize: 12, margin: '4px 0' }}>
      <b>LLM 검토</b> — {review.verdict === 'ok' ? '고칠 것 없음' : `지적 ${issues.length}건 → 반영해 고친 안`}
      {issues.length > 0 && (
        <ul style={{ margin: '2px 0', paddingLeft: 18 }}>
          {issues.map((i, n) => (
            <li key={n}><span className="mono">{i.target ?? i.column}</span>: {i.problem}{i.suggestion && ` → ${i.suggestion}`}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** 고친 안을 JSON으로 보여 주고 손으로 더 고칠 수 있게 — 확정은 그 글을 그대로 보낸다(서버가 다시 검사한다). */
function JsonEditor<T>({ value, onConfirm, label }: { value: T; onConfirm: (v: T) => void; label: string }) {
  const [text, setText] = useState(JSON.stringify(value, null, 2));
  const [bad, setBad] = useState('');
  useEffect(() => setText(JSON.stringify(value, null, 2)), [value]);
  return (
    <>
      <textarea className="mono" value={text} onChange={(e) => setText(e.target.value)} spellCheck={false}
        style={{ width: '100%', height: 220, fontSize: 12 }} />
      {bad && <p className="error" style={{ fontSize: 12 }}>{bad}</p>}
      <button onClick={() => {
        try { setBad(''); onConfirm(JSON.parse(text) as T); } catch (e) { setBad(`JSON이 아닙니다: ${(e as Error).message}`); }
      }}>{label}</button>
    </>
  );
}

function SchemaStep({ status, busy, onPropose, onConfirm }: {
  status: SemanticStatus; busy: boolean; onPropose: () => void; onConfirm: (s: SemanticSchema) => void;
}) {
  const draft = status.schema_draft;
  const confirmed = status.schema;
  return (
    <section style={{ marginTop: 12 }}>
      <h3 style={{ fontSize: 14 }}>① 스키마 — 개념·관계 종류</h3>
      {confirmed && (
        <p style={{ fontSize: 12 }}>
          ✅ 확정 판 <span className="mono">{confirmed.version}</span> ({confirmed.confirmed_by}, {confirmed.confirmed_at}) — 개념{' '}
          {confirmed.classes.map((c) => c.name).join('·')} / 관계 {confirmed.relations.map((r) => `${r.from}→${r.name}→${r.to}`).join(', ')}
        </p>
      )}
      <button className="secondary" disabled={busy} onClick={onPropose}>
        {draft ? '다시 제안·검토(LLM)' : '스키마 제안·검토(LLM)'}
      </button>
      {draft && (
        <div style={{ marginTop: 8 }}>
          <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
            표본 {draft.samples.length}건으로 제안({draft.provider}, {draft.created_at}) — 원안 개념{' '}
            {draft.proposal.classes.map((c) => c.name).join('·')}
          </p>
          <Issues review={draft.review} />
          {draft.problems.length > 0 && <p className="error" style={{ fontSize: 12 }}>기계 검사: {draft.problems.join(' / ')}</p>}
          <JsonEditor value={draft.revised} onConfirm={onConfirm} label={confirmed ? '이 안으로 판 바꾸기(문서를 다시 분석)' : '확정'} />
        </div>
      )}
    </section>
  );
}

function FormsStep({ forms, schemaVersion, busy, onPropose, onConfirm }: {
  forms: SemanticForm[]; schemaVersion: string; busy: boolean;
  onPropose: (form: string) => void; onConfirm: (form: string, m: SemanticMapping) => void;
}) {
  const [open, setOpen] = useState('');
  return (
    <section style={{ marginTop: 16 }}>
      <h3 style={{ fontSize: 14 }}>② 양식 — 되풀이되는 표의 열 대응</h3>
      <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
        여러 문서에 되풀이되는 표는 LLM이 문서마다 읽지 않습니다. 열이 어떤 개념인지 대응표를 정하면 그 양식의 모든
        문서에서 규칙으로 뽑습니다. 검토는 실제 값 표본과 대조합니다.
      </p>
      {forms.length === 0 && <p className="mutedtext" style={{ fontSize: 12 }}>되풀이되는 양식이 없습니다.</p>}
      <table style={{ fontSize: 12 }}>
        <tbody>
          {forms.map((f) => {
            const stale = f.confirmed && f.confirmed.schema_version !== schemaVersion;
            return (
              <tr key={f.name}>
                <td className="mono" style={{ maxWidth: 420 }}>{f.name}</td>
                <td>{f.documents.toLocaleString()}건</td>
                <td>{f.confirmed ? (stale ? '⚠️ 옛 스키마의 대응표' : '✅ 확정') : f.draft ? '검토된 안 있음' : '—'}</td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  <button className="small secondary" disabled={busy} onClick={() => onPropose(f.name)}>제안·검토(LLM)</button>{' '}
                  {f.draft && <button className="small secondary" onClick={() => setOpen(open === f.name ? '' : f.name)}>보기</button>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {forms.filter((f) => f.name === open && f.draft).map((f) => (
        <div key={f.name} style={{ marginTop: 8 }}>
          <div className="mutedtext" style={{ fontSize: 12 }}>실제 값 표본</div>
          <table style={{ fontSize: 11 }}>
            <thead><tr>{f.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
            <tbody>{f.draft!.sample_rows.slice(0, 4).map((r, i) => (
              <tr key={i}>{f.columns.map((c) => <td key={c}>{r[c]}</td>)}</tr>))}</tbody>
          </table>
          <Issues review={f.draft!.review} />
          {f.draft!.problems.length > 0 && <p className="error" style={{ fontSize: 12 }}>기계 검사: {f.draft!.problems.join(' / ')}</p>}
          <JsonEditor value={f.draft!.revised} onConfirm={(m) => onConfirm(f.name, m)} label="대응표 확정" />
        </div>
      ))}
    </section>
  );
}

function AnalyzeStep({ status, onAction }: { status: SemanticStatus; onAction: (a: 'start' | 'stop' | 'resume') => void }) {
  const done = Object.values(status.analyzed).reduce((n, v) => n + v, 0);
  const rate = status.review.error_rate;
  const outside = Object.entries(status.outside).slice(0, 15);
  return (
    <section style={{ marginTop: 16 }}>
      <h3 style={{ fontSize: 14 }}>③ 문서 분석</h3>
      <p style={{ fontSize: 12, margin: '4px 0' }}>
        {done.toLocaleString()} / {status.documents.toLocaleString()}건 — LLM {status.analyzed.ok ?? 0} · 양식 규칙{' '}
        {status.analyzed.form ?? 0} · 실패 {status.analyzed.failed ?? 0}
        {' · '}표본 검토 {status.review.items}개 중 틀림 {status.review.wrong}
        {rate !== null && ` (${Math.round(rate * 100)}%, 멈춤 기준 ${Math.round(status.review.stop_rate * 100)}%)`}
        {' · '}개념 {Object.values(status.counts.nodes).reduce((n, v) => n + v, 0).toLocaleString()} · 관계{' '}
        {Object.values(status.counts.edges).reduce((n, v) => n + v, 0).toLocaleString()}
      </p>
      {status.paused && <p className="error" style={{ fontSize: 12 }}>⏸ {status.paused}</p>}
      <div className="row" style={{ gap: 6 }}>
        {status.running
          ? <button className="secondary" onClick={() => onAction('stop')}>중지</button>
          : status.paused
            ? <button onClick={() => onAction('resume')}>고쳤습니다 — 다시 시작</button>
            : <button onClick={() => onAction('start')}>{done ? '바뀐 문서 분석' : '분석 시작'}</button>}
        {status.running && <span className="mutedtext" style={{ fontSize: 12 }}>⏳ 분석 중 — 바뀐 문서만, 문서마다 LLM 한 번</span>}
      </div>
      {status.review.notes.length > 0 && (
        <details style={{ fontSize: 12, marginTop: 6 }} open={!!status.paused}>
          <summary>LLM 검토가 틀렸다고 한 것 {status.review.notes.length}건 — 스키마를 고칠지, 그대로 다시 시작할지의 근거</summary>
          <ul style={{ paddingLeft: 18 }}>
            {status.review.notes.slice().reverse().map((n, i) => (
              <li key={i}>
                <b>{n.item}</b> — {n.problem}
                <div className="mutedtext">“{n.evidence}” — <span className="mono">{n.path}</span></div>
              </li>
            ))}
          </ul>
        </details>
      )}
      {status.failed.length > 0 && (
        <details style={{ fontSize: 12, marginTop: 6 }}>
          <summary>실패한 문서 {status.failed.length}건</summary>
          <ul>{status.failed.map((f) => <li key={f.path}><span className="mono">{f.path}</span>: {f.error}</li>)}</ul>
        </details>
      )}
      {outside.length > 0 && (
        <p className="mutedtext" style={{ fontSize: 12 }}>
          스키마 밖 후보(다음 판에 넣을지): {outside.map(([n, c]) => `${n}(${c})`).join(', ')}
        </p>
      )}
    </section>
  );
}

function MergeStep({ status, busy, onMerge, onUnmerge }: {
  status: SemanticStatus; busy: boolean; onMerge: () => void; onUnmerge: (cls: string, alias: string) => void;
}) {
  const last = status.last_merge;
  return (
    <section style={{ marginTop: 16 }}>
      <h3 style={{ fontSize: 14 }}>④ 같은 개체 묶기</h3>
      <button className="secondary" disabled={busy} onClick={onMerge}>묶기 제안·검토(LLM)</button>
      {last && (
        <p className="mutedtext" style={{ fontSize: 12 }}>
          {last.at}: 묶음 {last.applied.length}건 적용 · LLM 검토가 거절한 묶음 {last.rejected.length}건
          {last.rejected.length > 0 && ` (${last.rejected.map((r) => `${r.canonical}≠${r.names.join('/')}`).join(', ')})`}
        </p>
      )}
      {status.aliases.length > 0 && (
        <table style={{ fontSize: 12 }}>
          <thead><tr><th>개념</th><th>이름</th><th>대표 이름</th><th>이유</th><th /></tr></thead>
          <tbody>{status.aliases.map((a) => (
            <tr key={`${a.cls}:${a.alias}`}>
              <td>{a.cls}</td><td>{a.alias}</td><td>{a.canonical}</td><td className="mutedtext">{a.reason}</td>
              <td><button className="small secondary" onClick={() => onUnmerge(a.cls, a.alias)}>풀기</button></td>
            </tr>))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Explore({ store, schema }: { store: string; schema: SemanticSchema }) {
  const [cls, setCls] = useState('');
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<SemanticHit[] | null>(null);
  const [picked, setPicked] = useState<SemanticNeighbors | null>(null);
  const [error, setError] = useState('');
  const find = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    try { setHits((await api.semanticSearch(store, cls, q)).nodes); setPicked(null); } catch (err) { setError((err as Error).message); }
  };
  const open = async (h: SemanticHit) => {
    try { setPicked(await api.semanticNeighbors(store, h.cls, h.name)); } catch (err) { setError((err as Error).message); }
  };
  return (
    <section style={{ marginTop: 16 }}>
      <h3 style={{ fontSize: 14 }}>내용 그래프 조회</h3>
      <form className="row" style={{ gap: 6 }} onSubmit={find}>
        <select value={cls} onChange={(e) => setCls(e.target.value)}>
          <option value="">모든 개념</option>
          {schema.classes.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
        </select>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="이름 일부(별칭 포함)" />
        <button type="submit" className="secondary">찾기</button>
      </form>
      {error && <p className="error">{error}</p>}
      {hits && hits.length === 0 && <p className="mutedtext" style={{ fontSize: 12 }}>찾은 것이 없습니다.</p>}
      {hits && hits.length > 0 && (
        <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginTop: 6 }}>
          {hits.map((h) => (
            <button key={`${h.cls}:${h.name}`} className="small secondary" onClick={() => void open(h)}
              title={h.aliases.length ? `별칭: ${h.aliases.join(', ')}` : undefined}>
              {h.cls} · {h.name} ({h.documents})
            </button>
          ))}
        </div>
      )}
      {picked && (
        <div style={{ marginTop: 8, fontSize: 12 }}>
          <b>{picked.cls} · {picked.name}</b> — 관계 {picked.relations.length}
          <ul style={{ paddingLeft: 18 }}>
            {picked.relations.map((r) => (
              <li key={`${r.rel}:${r.direction}:${r.cls}:${r.name}`} style={{ marginBottom: 4 }}>
                {r.direction === 'out' ? `—${r.rel}→ ${r.cls} · ${r.name}` : `← ${r.cls} · ${r.name} —${r.rel}`}
                {r.evidence.map((e, i) => (
                  <div key={i} className="mutedtext">
                    “{e.text}” — <span className="mono">{e.path}</span>{e.origin === 'form' && ' (양식 규칙)'}
                  </div>
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
