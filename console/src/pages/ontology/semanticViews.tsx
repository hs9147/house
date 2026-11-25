import type { ReactNode } from 'react';
import type { SemanticMapping, SemanticSchema, SemanticTemplateRules } from '../../lib/types';
import { VIZ, fmt } from '../../lib/viz';

/**
 * 내용 분석의 결과를 **읽는 모양**으로 — 스키마·대응표·규칙은 표로, 처리 내역은 막대로, 근거는 인용으로, 내부 코드
 * (llm·form·id·reference…)는 우리말 이름표로. JSON은 「직접 고치기」 안에만 둔다(고칠 때만 필요하다).
 */

export const LAYER_LABEL: Record<string, string> = { reference: '기준 지식', entity: '대상', state: '상태', event: '사건' };
const LAYER_CLASS: Record<string, string> = { reference: 'info', entity: 'ok', state: 'warn', event: 'dim' };
const LAYER_ORDER = ['reference', 'entity', 'state', 'event'];

export const ORIGIN_LABEL: Record<string, string> = {
  llm: 'LLM 추출', form: '양식 규칙', template: '템플릿 규칙', list: '수집 목록', path: '경로 틀',
};
export const PAIR_SOURCE_LABEL: Record<string, string> = {
  id: '같은 식별자', rule: '이름 기준', llm: 'LLM 판정', human: '사람이 확인',
};

export function LayerBadge({ layer }: { layer?: string }) {
  const l = layer ?? 'entity';
  return <span className={`status ${LAYER_CLASS[l] ?? 'dim'}`}>{LAYER_LABEL[l] ?? l}</span>;
}

/** 문서는 파일 이름만 — 전체 경로는 마우스를 올리면. */
export const docName = (path: string) => path.split('/').pop() || path;
export function DocRef({ path }: { path: string }) {
  return <span className="mono" title={path} style={{ fontSize: 11 }}>{docName(path)}</span>;
}

/** 근거 한 줄 — 「인용」 — 문서 · 어디서 나왔나 · 관측한 날 · 효력 기간. */
export function Quote({ text, path, origin, observed, from, to }: {
  text: string; path: string; origin?: string; observed?: string; from?: string; to?: string;
}) {
  const meta = [origin && origin !== 'llm' ? ORIGIN_LABEL[origin] ?? origin : '', observed ? `관측 ${observed}` : '',
                from || to ? `효력 ${from || '…'} ~ ${to || '…'}` : ''].filter(Boolean);
  return (
    <div style={{ fontSize: 12, margin: '2px 0 2px 2px', paddingLeft: 8, borderLeft: `2px solid ${VIZ.axis}` }}>
      <span>“{text}”</span>
      <span className="mutedtext"> — <DocRef path={path} />{meta.length > 0 && ` · ${meta.join(' · ')}`}</span>
    </div>
  );
}

/** 처리 내역 — 한 줄 막대(부분마다 색)와 범례. total을 주면 나머지를 「남음」(옅게)으로. */
export function Breakdown({ parts, total, unit = '건' }: {
  parts: { label: string; value: number; color: string }[]; total?: number; unit?: string;
}) {
  const shown = parts.filter((p) => p.value > 0);
  const sum = shown.reduce((n, p) => n + p.value, 0);
  const rest = total !== undefined ? Math.max(total - sum, 0) : 0;
  const all = Math.max(sum + rest, 1);
  return (
    <div style={{ margin: '4px 0' }}>
      {/* 부분 사이 2px 표면 간격, 끝은 둥글게(dataviz 마크 규격) — 남은 몫은 바탕 트랙이 보인다 */}
      <div style={{ display: 'flex', gap: 2, height: 10, borderRadius: 4, overflow: 'hidden', background: VIZ.grid,
                    maxWidth: 560 }}>
        {shown.map((p) => (
          <div key={p.label} title={`${p.label} ${fmt(p.value)}${unit} (${Math.round((p.value / all) * 100)}%)`}
               style={{ width: `${(p.value / all) * 100}%`, background: p.color, minWidth: 2 }} />
        ))}
      </div>
      <div className="row" style={{ gap: 12, flexWrap: 'wrap', fontSize: 12, marginTop: 4 }}>
        {shown.map((p) => (
          <span key={p.label}>
            <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 2, background: p.color, marginRight: 4 }} />
            {p.label} <b>{fmt(p.value)}</b>
          </span>
        ))}
        {rest > 0 && <span className="mutedtext">남음 {fmt(rest)}</span>}
        {shown.length === 0 && rest === 0 && <span className="mutedtext">아직 없음</span>}
      </div>
    </div>
  );
}

/** 「직접 고치기」 — JSON 편집은 접어 둔다. */
export function Advanced({ children, label = '직접 고치기(JSON)' }: { children: ReactNode; label?: string }) {
  return (
    <details style={{ fontSize: 12, marginTop: 8 }}>
      <summary style={{ cursor: 'pointer' }} className="mutedtext">{label}</summary>
      <div style={{ marginTop: 6 }}>{children}</div>
    </details>
  );
}

function Arrow({ from, rel, to }: { from: ReactNode; rel: string; to: ReactNode }) {
  return (
    <span style={{ whiteSpace: 'nowrap' }}>
      <b>{from}</b> <span className="mutedtext">—</span> {rel} <span className="mutedtext">→</span> <b>{to}</b>
    </span>
  );
}

/** 스키마 — 층위별 개념 표와 관계 표. */
export function SchemaView({ schema }: { schema: SemanticSchema }) {
  const classes = [...schema.classes].sort((a, b) =>
    LAYER_ORDER.indexOf(a.layer ?? 'entity') - LAYER_ORDER.indexOf(b.layer ?? 'entity'));
  return (
    <div className="row" style={{ gap: 16, flexWrap: 'wrap', alignItems: 'flex-start', marginTop: 6 }}>
      <table style={{ fontSize: 12, flex: '1 1 380px' }}>
        <thead><tr><th>층위</th><th>개념</th><th>뜻</th><th>예</th></tr></thead>
        <tbody>
          {classes.map((c) => (
            <tr key={c.name}>
              <td><LayerBadge layer={c.layer} /></td>
              <td><b>{c.name}</b></td>
              <td className="mutedtext">{c.description}</td>
              <td className="mutedtext">{c.examples.slice(0, 3).join(', ')}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <table style={{ fontSize: 12, flex: '1 1 380px' }}>
        <thead><tr><th>관계</th><th>시점</th><th>뜻</th></tr></thead>
        <tbody>
          {schema.relations.map((r) => (
            <tr key={`${r.from}:${r.name}:${r.to}`}>
              <td><Arrow from={r.from} rel={r.name} to={r.to} /></td>
              <td>{r.temporal ? <span className="status warn" title="시점에 매인 관계 — 문서마다 관측으로 쌓여 최신 값과 이력이 남는다">시점</span>
                : <span className="mutedtext">—</span>}</td>
              <td className="mutedtext">{r.description}</td>
            </tr>
          ))}
          {schema.relations.length === 0 && <tr><td colSpan={3} className="mutedtext">관계 없음</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

/** 양식 대응표 — 열마다 개념, 열 사이의 관계. */
export function MappingView({ mapping, columns }: { mapping: SemanticMapping & { date?: string | null }; columns: string[] }) {
  return (
    <div style={{ marginTop: 6 }}>
      <table style={{ fontSize: 12 }}>
        <thead><tr><th>표의 열</th><th>개념</th></tr></thead>
        <tbody>
          {columns.map((c) => (
            <tr key={c}>
              <td>{c}</td>
              <td>{mapping.columns[c] ? <b>{mapping.columns[c]}</b> : <span className="mutedtext">쓰지 않음</span>}
                {mapping.date === c && <span className="status info" style={{ marginLeft: 6 }}>관측한 날</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {mapping.relations.length > 0 && (
        <div style={{ fontSize: 12, marginTop: 6 }}>
          <span className="mutedtext">한 행에서 잇는 관계: </span>
          {mapping.relations.map((r, i) => (
            <span key={i} style={{ marginRight: 12 }}>
              <Arrow from={`${r.from}(${mapping.columns[r.from] ?? '?'})`} rel={r.rel} to={`${r.to}(${mapping.columns[r.to] ?? '?'})`} />
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** 템플릿 규칙 — 찾는 것(개념·모양·필수)과 그 사이의 관계. */
export function RulesView({ rules }: { rules: SemanticTemplateRules }) {
  const name = (i: number) => rules.fields[i]?.class ?? `#${i}`;
  return (
    <div style={{ marginTop: 6 }}>
      <table style={{ fontSize: 12 }}>
        <thead><tr><th>찾는 개념</th><th>찾는 자리(규칙)</th><th>필수</th></tr></thead>
        <tbody>
          {rules.fields.map((f, i) => (
            <tr key={i}>
              <td><b>{f.class}</b>{f.note && <div className="mutedtext">{f.note}</div>}</td>
              <td className="mono" style={{ fontSize: 11 }} title="정규식 — 첫 괄호 묶음이 이름이 된다">{f.pattern}</td>
              <td>{f.required ? <span className="status warn" title="못 찾으면 그 문서는 LLM이 읽는다">필수</span>
                : <span className="mutedtext">—</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rules.relations.length > 0 && (
        <div style={{ fontSize: 12, marginTop: 6 }}>
          <span className="mutedtext">잇는 관계: </span>
          {rules.relations.map((r, i) => (
            <span key={i} style={{ marginRight: 12 }}><Arrow from={name(r.from)} rel={r.rel} to={name(r.to)} /></span>
          ))}
        </div>
      )}
    </div>
  );
}

/** 분석한 문서가 처리된 길 — 색은 상태 팔레트(규칙은 초록, LLM은 기본, 실패는 빨강). */
// 정체는 범주 팔레트를 정한 순서로(VIZ.categorical — 검증 결과는 lib/viz 주석), 나머지는 중립, 실패는 상태 색
export const ANALYZED_PARTS = (a: Record<string, number>) => [
  { label: 'LLM', value: a.ok ?? 0, color: VIZ.categorical[0] },
  { label: '양식 규칙', value: a.form ?? 0, color: VIZ.categorical[1] },
  { label: '템플릿 규칙', value: a.template ?? 0, color: VIZ.categorical[2] },
  { label: '같은 본문', value: a.copy ?? 0, color: VIZ.categorical[3] },
  { label: '건너뜀', value: a.skipped ?? 0, color: VIZ.neutral },
  { label: '실패', value: a.failed ?? 0, color: VIZ.status.critical },
];

export const ORIGIN_PARTS = (n: Record<string, number>) => [
  { label: 'LLM 추출', value: n.llm ?? 0, color: VIZ.categorical[0] },
  { label: '양식 규칙', value: n.form ?? 0, color: VIZ.categorical[1] },
  { label: '템플릿 규칙', value: n.template ?? 0, color: VIZ.categorical[2] },
  { label: '수집 목록', value: n.list ?? 0, color: VIZ.categorical[3] },
  { label: '경로 틀', value: n.path ?? 0, color: VIZ.categorical[4] },
];
