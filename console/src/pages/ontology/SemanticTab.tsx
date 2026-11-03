import { useEffect, useMemo, useState } from 'react';
import { api } from '../../lib/api';
import { useApi, usePolling } from '../../lib/hooks';
import { runTask } from '../../lib/tasks';
import type {
  SemanticForm, SemanticHit, SemanticMapping, SemanticMergeRules, SemanticNeighbors, SemanticReview, SemanticSchema,
  SemanticStatus, SemanticTemplate, SemanticTemplateEval, SemanticTemplateRules, SemanticGoldItem, SemanticGoldKind,
} from '../../lib/types';

/**
 * 내용 분석 — 문서의 **형식**(제목·표·조문)이 아니라 **내용**에서 LLM이 개념과 관계를 뽑는다(services/semantic).
 *
 * 네 단계를 위에서 아래로 한다. 한 번 정해 많은 문서에 퍼지는 것(스키마·양식 대응표)은 LLM이 제안하면
 * **LLM이 검토하고** 그 의견으로 한 번 고친 안을 보여 준다 — 확정은 관리자가 한다. 문서마다의 추출은 기계
 * 검사(근거 문장이 원문에 있는가)를 전부에 걸고 LLM 검토는 표본만 한다. 표본 오류율이 기준을 넘으면 멈춘다.
 * 같은 틀로 되풀이되는 문서(템플릿)는 LLM이 짠 정규식 규칙을 **다른 문서의 LLM 추출과 견줘** 검증한 뒤 확정한다.
 * 묶기는 기준(JSON)으로 정하고 애매한 짝만 LLM이 판정·검토한다 — LLM이 제안한 새 기준은 관리자가 확정한다.
 * 사람이 확인한 정답으로 묶기 기준을 다시 배우고(나을 때만 바꾼다) 템플릿 규칙을 다시 잰다(⑤, 하루 한 번도 돈다).
 *
 * 형식 그래프(정보 조회 탭)는 그대로 있다 — 이 탭이 만드는 것은 따로 저장되는 내용 그래프다.
 */
export default function SemanticTab() {
  const overview = useApi(() => api.ontologyOverview(), []);
  const stores = useMemo(() => (overview.data?.stores ?? []).filter((s) => s.extracted > 0), [overview.data]);
  const [store, setStore] = useState('');
  const [status, setStatus] = useState<SemanticStatus | null>(null);
  const [forms, setForms] = useState<SemanticForm[]>([]);
  const [templates, setTemplates] = useState<SemanticTemplate[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    if (!store && stores.length > 0) setStore(stores[0].store);
  }, [stores, store]);

  const reload = async () => {
    if (!store) return;
    try {
      const [s, f, t] = await Promise.all([api.semanticStatus(store), api.semanticForms(store), api.semanticTemplates(store)]);
      setStatus(s);
      setForms(f.forms);
      setTemplates(t.templates);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  useEffect(() => { setStatus(null); setForms([]); setTemplates([]); void reload(); }, [store]); // eslint-disable-line react-hooks/exhaustive-deps
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
                  + `양식 ${s.analyzed.form ?? 0} · 템플릿 ${s.analyzed.template ?? 0} · 중복 ${s.analyzed.copy ?? 0} · 건너뜀 ${s.analyzed.skipped ?? 0}`
                  + ` · 실패 ${s.analyzed.failed ?? 0}` });
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
              <TemplatesStep templates={templates} schemaVersion={status.schema.version ?? ''} busy={busy}
                onPropose={(t) => track('템플릿 규칙 제안·검증', () => api.semanticProposeTemplate(store, t))}
                onConfirm={(t, r) => act('규칙을 확정했습니다 — 분석하면 이 템플릿의 문서는 규칙으로 뽑습니다.',
                  () => api.semanticConfirmTemplate(store, t, r))} />
              <AnalyzeStep status={status}
                onAction={(a) => (a === 'stop'
                  ? act('멈추라고 했습니다(지금 문서까지는 끝냅니다).', () => api.semanticAnalyze(store, a))
                  : trackAnalysis(a))} />
              <MergeStep status={status} busy={busy}
                onMerge={() => track('같은 개체 묶기·검토', () => api.semanticMerge(store))}
                onUnmerge={(cls, alias) => act(`'${alias}' 묶음을 풀었습니다 — 다음 묶기도 다시 묶지 않습니다.`,
                  () => api.semanticUnmerge(store, cls, alias))}
                onRules={(r) => act('묶기 기준을 확정했습니다 — 다음 묶기부터 씁니다.', () => api.semanticMergeRules(store, r))} />
              <GoldStep store={store} status={status} busy={busy} onLabeled={reload}
                onTune={() => act('기준을 다시 배웠습니다 — 아래 기록을 보세요.', () => api.semanticTune(store))}
                onRollback={(r) => act('이전 묶기 기준으로 되돌렸습니다.', () => api.semanticMergeRules(store, r))} />
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

const pct = (v: number) => `${Math.round(v * 100)}%`;

function EvalView({ title, ev, need }: { title: string; ev: SemanticTemplateEval; need: number }) {
  return (
    <div style={{ fontSize: 12, marginTop: 6 }}>
      <b>{title}</b> — 정밀도 {pct(ev.precision)}{ev.precision < need ? ` (기준 ${pct(need)} 미달)` : ''} · 재현율{' '}
      {pct(ev.recall)}
      <ul style={{ paddingLeft: 18, margin: '2px 0' }}>
        {ev.docs.map((d) => (
          <li key={d.path}>
            <span className="mono">{d.path}</span>
            {d.missing_required.length > 0 && <span className="error"> 필수 필드 못 찾음(LLM으로 넘김)</span>}
            {d.extra.length > 0 && <span> · 더 뽑음: {d.extra.join(', ')}</span>}
            {d.missed.length > 0 && <span className="mutedtext"> · 놓침: {d.missed.join(', ')}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function TemplatesStep({ templates, schemaVersion, busy, onPropose, onConfirm }: {
  templates: SemanticTemplate[]; schemaVersion: string; busy: boolean;
  onPropose: (id: string) => void; onConfirm: (id: string, rules: SemanticTemplateRules) => void;
}) {
  const [open, setOpen] = useState('');
  return (
    <section style={{ marginTop: 16 }}>
      <h3 style={{ fontSize: 14 }}>②′ 템플릿 — 같은 틀로 되풀이되는 문서</h3>
      <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
        표가 아니어도 같은 틀(라벨 줄은 같고 값만 다른)로 되풀이되는 문서는 LLM이 정규식 규칙을 짭니다. 규칙은 몇 건으로
        짜고 <b>다른</b> 몇 건에서 LLM 추출과 견줘 검증합니다 — 정밀도가 기준을 넘어야 확정할 수 있습니다. 확정하면 그
        문서들은 LLM 없이 규칙으로 뽑고, 필수 필드를 못 찾는 문서만 LLM이 읽습니다.
      </p>
      {templates.length === 0 && <p className="mutedtext" style={{ fontSize: 12 }}>되풀이되는 틀이 없습니다.</p>}
      <table style={{ fontSize: 12 }}>
        <tbody>
          {templates.map((t) => {
            const stale = t.confirmed && t.confirmed.schema_version !== schemaVersion;
            return (
              <tr key={t.id}>
                <td style={{ maxWidth: 420 }} title={t.skeleton.join('\n')}>
                  {t.label}<div className="mutedtext mono">예: {t.example}</div>
                </td>
                <td>{t.documents.toLocaleString()}건</td>
                <td>{t.confirmed
                  ? (stale ? '⚠️ 옛 스키마의 규칙' : `✅ 확정(정밀도 ${pct(t.confirmed.precision)})`)
                  : t.suspended ? `⏸ 멈춤 — ${t.suspended.reason}` : t.draft ? '검증된 안 있음' : '—'}</td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  <button className="small secondary" disabled={busy} onClick={() => onPropose(t.id)}>규칙 제안·검증(LLM)</button>{' '}
                  {t.draft && <button className="small secondary" onClick={() => setOpen(open === t.id ? '' : t.id)}>보기</button>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {templates.filter((t) => t.id === open && t.draft).map((t) => {
        const d = t.draft!;
        const fixed = JSON.stringify(d.proposal) !== JSON.stringify(d.revised);
        return (
          <div key={t.id} style={{ marginTop: 8 }}>
            <div className="mutedtext" style={{ fontSize: 12 }}>
              규칙을 짠 문서: {d.train.join(', ')} · 검증 문서: {d.check.map((c) => c.path).join(', ')}
            </div>
            {fixed && d.first && <EvalView title="처음 규칙" ev={d.first} need={d.precision_needed} />}
            {d.evaluation && <EvalView title={fixed ? '검증 결과로 고친 규칙' : '검증'} ev={d.evaluation} need={d.precision_needed} />}
            {d.problems.length > 0 && <p className="error" style={{ fontSize: 12 }}>기계 검사: {d.problems.join(' / ')}</p>}
            <JsonEditor value={d.revised} onConfirm={(r) => onConfirm(t.id, r)} label="규칙 확정(검증 문서로 다시 잽니다)" />
          </div>
        );
      })}
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
        {status.analyzed.form ?? 0} · 템플릿 규칙 {status.analyzed.template ?? 0} · 같은 본문 옮김 {status.analyzed.copy ?? 0} · 건너뜀(작업 파일·빈 문서){' '}
        {status.analyzed.skipped ?? 0} · 실패 {status.analyzed.failed ?? 0}
        {' · '}표본 검토 {status.review.items}개 중 틀림 {status.review.wrong}
        {rate !== null && ` (${Math.round(rate * 100)}%, 멈춤 기준 ${Math.round(status.review.stop_rate * 100)}%)`}
        {status.review.check_items > 0
          && ` · 이름이 근거에 없는 항목 검토 ${status.review.check_items}개 중 틀림 ${status.review.check_wrong}`}
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

function MergeStep({ status, busy, onMerge, onUnmerge, onRules }: {
  status: SemanticStatus; busy: boolean; onMerge: () => void; onUnmerge: (cls: string, alias: string) => void;
  onRules: (rules: Partial<SemanticMergeRules>) => void;
}) {
  const last = status.last_merge;
  const st = last?.stats;
  const rules = status.merge_rules;
  const draft = status.merge_rules_draft;
  const [text, setText] = useState('');
  const [bad, setBad] = useState('');
  const { version, confirmed_by, confirmed_at, ...editable } = rules;   // 판·확정자는 고치는 것이 아니다
  const saveText = () => {
    try {
      onRules(JSON.parse(text));
      setBad('');
    } catch (e) {
      setBad(`JSON이 아닙니다: ${(e as Error).message}`);
    }
  };
  return (
    <section style={{ marginTop: 16 }}>
      <h3 style={{ fontSize: 14 }}>④ 같은 개체 묶기</h3>
      <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
        같은 식별자(협력사코드·사업자번호)면 묶고, 이름 점수가 묶음 문턱({rules.thresholds.merge}) 이상이면 묶고 거절
        문턱({rules.thresholds.reject}) 이하면 거절합니다. 그 사이의 애매한 짝만 LLM이 판정하고 다시 검토합니다. 한 번
        정한 짝은 다시 묻지 않고, 「풀기」한 짝은 다시 묶지 않습니다. 기준 판 {rules.version}
        {confirmed_at && ` · ${confirmed_by} 확정 ${confirmed_at}`}
      </p>
      <button className="secondary" disabled={busy} onClick={onMerge}>묶기(기준 → 애매한 짝만 LLM)</button>
      {last && (
        <p className="mutedtext" style={{ fontSize: 12 }}>
          {last.at}: 묶음 {last.applied.length}건 새로 적용
          {st && ` · 짝 ${st.pairs.toLocaleString()}개 — 식별자 ${st.id} · 기준 ${st.rule} · LLM ${st.llm}(호출 ${st.llm_calls})`
            + ` · 지난 판정 재사용 ${st.cached} · 사람이 정한 것 ${st.human}`}
          {last.rejected.length > 0 && ` · LLM이 다르다고 한 짝 ${last.rejected.length}개 (`
            + `${last.rejected.slice(0, 8).map((r) => `${r.a}≠${r.b}`).join(', ')}${last.rejected.length > 8 ? ' …' : ''})`}
        </p>
      )}
      {draft && (
        <div style={{ fontSize: 12, padding: 8, marginTop: 8, border: '1px solid var(--border)', borderRadius: 6 }}>
          LLM이 판정한 짝 {draft.from_pairs}개에서 제안한 새 기준 — 확정하면 다음부터 LLM 없이 처리합니다.
          {Object.keys(draft.translit).length > 0 && (
            <div>음역: {Object.entries(draft.translit).map(([k, v]) => `${k} → ${v}`).join(', ')}</div>)}
          {draft.strip.length > 0 && <div>떼어 낼 말: {draft.strip.join(', ')}</div>}
          <button className="small" style={{ marginTop: 6 }} disabled={busy}
            onClick={() => onRules({ translit: { ...rules.translit, ...draft.translit },
                                     strip: [...rules.strip, ...draft.strip] })}>제안 기준 확정</button>
        </div>
      )}
      <details style={{ marginTop: 8, fontSize: 12 }}
        onToggle={(e) => { if ((e.target as HTMLDetailsElement).open) setText(JSON.stringify(editable, null, 2)); }}>
        <summary>기준(JSON) 보기·고치기</summary>
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={14}
          style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }} />
        {bad && <p className="error">{bad}</p>}
        <button className="small" disabled={busy} onClick={saveText}>기준 확정</button>
      </details>
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

const GOLD_KINDS: { kind: SemanticGoldKind; label: string; yes: string; no: string }[] = [
  { kind: 'pair', label: '묶기 짝', yes: '같다', no: '다르다' },
  { kind: 'node', label: '개체', yes: '맞다', no: '틀리다' },
  { kind: 'edge', label: '관계', yes: '맞다', no: '틀리다' },
];

function GoldItem({ it }: { it: SemanticGoldItem }) {
  if (it.kind === 'pair') {
    return (
      <>
        <b>{it.cls}: {it.a} ↔ {it.b}</b>
        <span className="mutedtext"> — 지금 판정 {it.decision === 'same' ? '같다' : '다르다'}({it.source}, 점수 {it.score?.toFixed(2)})</span>
        {[it.a_evidence, it.b_evidence].map((e, i) => e && (
          <div key={i} className="mutedtext">“{e.evidence.slice(0, 160)}” — <span className="mono">{e.path}</span></div>))}
      </>
    );
  }
  return (
    <>
      <b>{it.kind === 'node' ? `${it.cls}: ${it.name}` : `${it.src} —${it.rel}→ ${it.dst}`}</b>
      <span className="mutedtext"> ({it.origin})</span>
      <div className="mutedtext">“{it.evidence?.slice(0, 200)}” — <span className="mono">{it.path}</span></div>
    </>
  );
}

function GoldStep({ store, status, busy, onLabeled, onTune, onRollback }: {
  store: string; status: SemanticStatus; busy: boolean; onLabeled: () => void; onTune: () => void;
  onRollback: (rules: Partial<SemanticMergeRules>) => void;
}) {
  const [kind, setKind] = useState<SemanticGoldKind>('pair');
  const [items, setItems] = useState<SemanticGoldItem[] | null>(null);
  const [error, setError] = useState('');
  const spec = GOLD_KINDS.find((k) => k.kind === kind)!;
  const load = async (k: SemanticGoldKind) => {
    setKind(k);
    setError('');
    try { setItems((await api.semanticGold(store, k)).items); } catch (e) { setError((e as Error).message); }
  };
  const label = async (it: SemanticGoldItem, value: boolean) => {
    try {
      await api.semanticLabel(store, it.kind, it.item, value);
      setItems((xs) => (xs ?? []).filter((x) => x.item !== it.item));
      onLabeled();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <section style={{ marginTop: 16 }}>
      <h3 style={{ fontSize: 14 }}>⑤ 사람 확인 · 기준 다시 배우기</h3>
      <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
        사람이 확인한 것이 정답입니다. 짝 판정은 바로 다음 묶기에 쓰이고, 틀린 개체·관계는 그래프에서 뺍니다. 정답이
        쌓이면 묶기 기준(가중치·문턱)을 다시 배워 <b>배우는 데 쓰지 않은 정답</b>에서 지금 기준보다 나을 때만 바꾸고,
        템플릿 규칙은 정답으로 다시 재어 기준 밑이면 멈춥니다. 하루에 한 번 스케줄러도 돕니다(LLM을 부르지 않습니다).
      </p>
      <div className="row" style={{ gap: 6 }}>
        {GOLD_KINDS.map((k) => (
          <button key={k.kind} className={`small ${items && kind === k.kind ? '' : 'secondary'}`} onClick={() => load(k.kind)}>
            {k.label} 확인하기
          </button>))}
        <button className="small secondary" disabled={busy} onClick={onTune}>지금 다시 배우기</button>
      </div>
      {error && <p className="error" style={{ fontSize: 12 }}>{error}</p>}
      {items && (items.length === 0
        ? <p className="mutedtext" style={{ fontSize: 12 }}>확인할 것이 없습니다.</p>
        : (
          <ul style={{ fontSize: 12, paddingLeft: 18 }}>
            {items.map((it) => (
              <li key={it.item} style={{ marginBottom: 4 }}>
                <GoldItem it={it} />{' '}
                <button className="small" onClick={() => label(it, true)}>{spec.yes}</button>{' '}
                <button className="small secondary" onClick={() => label(it, false)}>{spec.no}</button>
              </li>))}
          </ul>))}
      <p className="mutedtext" style={{ fontSize: 12 }}>
        정답 — {GOLD_KINDS.map((k) => {
          const m = status.gold[k.kind];
          const by = Object.entries(m.by).map(([src, b]) => `${src} ${b.correct}/${b.n}(${pct(b.accuracy)})`).join(', ');
          return `${k.label} ${m.labeled}개${by ? ` [${by}]` : ''}`;
        }).join(' · ')}
      </p>
      {status.tune_history.length > 0 && (
        <details style={{ fontSize: 12 }}>
          <summary>다시 배운 기록 {status.tune_history.length}건</summary>
          <ul style={{ paddingLeft: 18 }}>
            {status.tune_history.slice().reverse().map((h) => (
              <li key={h.at}>
                {h.at} ({h.actor}): 묶기 기준 {({ promoted: '바꿈', kept: '그대로', skipped: '배우지 않음' })[h.merge.status]} — {h.merge.reason}
                {h.merge.champion && h.merge.challenger && ` (틀림·LLM행: 지금 ${h.merge.champion.wrong}·${h.merge.champion.gray}`
                  + ` → 새 기준 ${h.merge.challenger.wrong}·${h.merge.challenger.gray}, 정답 ${h.merge.champion.n}개)`}
                {h.merge.previous && (
                  <> <button className="small secondary" disabled={busy} onClick={() => onRollback(h.merge.previous!)}>
                    이전 기준으로 되돌리기</button></>)}
                {h.templates.filter((t) => t.status === 'suspended').map((t) => (
                  <div key={t.id} className="error">템플릿 {t.id} 멈춤 — 검증 문서 {t.check_precision === null ? '—' : pct(t.check_precision)}
                    · 사람 정답 {t.human_precision === null ? '—' : pct(t.human_precision)}({t.human_labels}개)</div>))}
              </li>))}
          </ul>
        </details>
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
