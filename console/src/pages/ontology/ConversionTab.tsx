import { useState } from 'react';
import { Link } from 'react-router-dom';
import Async from '../../components/Async';
import Tabs from '../../components/Tabs';
import { api } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import { isAdmin } from '../../lib/auth';
import type { OntologyOverview, OntologyProgress, OntologyProgressStore, OntologyStore } from '../../lib/types';
import { BarList, Heatmap, StatTile, VIZ, fmt } from '../../lib/viz';
import { ANALYZED_PARTS, Breakdown, ORIGIN_PARTS } from './semanticViews';

/**
 * 전환 현황 — 저장소마다 **정보 조회 → 내용 분석 → 워크플로 연결**이 어디까지 왔고 다음에 무엇을 할지
 * (services/ontology_progress). 예전에는 형식 그래프(색인 → 절·용어·표)만 셌다 — 그 뒤의 일(스키마·양식·템플릿·
 * 경로 틀·분석·워크플로 연결)이 어디서 멈췄는지는 각 탭을 돌아다녀야 알 수 있었다.
 *
 *  - 위: 전체 수치와 단계별 문서 수 — 어느 단계가 비었나
 *  - 가운데: 저장소 × 단계 — 한 줄에 그 저장소의 모든 단계와 **다음 할 일 하나**(누르면 그 일을 하는 탭으로)
 *  - 아래: 단계별 상세 — 정보 조회(형식 그래프의 분포·행렬·실패 이유) · 내용 분석 · 워크플로 연결
 */
export default function ConversionTab() {
  const progress = useApi(() => api.ontologyProgress(), []);
  return (
    <>
      <Async state={progress}>
        {(p: OntologyProgress) => <ProgressView p={p} />}
      </Async>
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>단계별 상세</h3>
        <Tabs tabs={[
          { key: 'formal', label: '정보 조회 — 형식 그래프', content: <FormalDetail /> },
          { key: 'semantic', label: '내용 분석', content: (
            <Async state={progress}>{(p: OntologyProgress) => <SemanticDetail p={p} />}</Async>) },
          { key: 'workflow', label: '워크플로 연결', content: (
            <Async state={progress}>{(p: OntologyProgress) => <WorkflowDetail p={p} />}</Async>) },
        ]} />
      </div>
    </>
  );
}

const pct = (n: number, d: number) => (d ? `${Math.round((n / d) * 100)}%` : '—');
const done = (s: NonNullable<OntologyProgressStore['semantic']>) => Object.values(s.analyzed).reduce((a, b) => a + b, 0);

function Mark({ kind, children }: { kind: 'ok' | 'warn' | 'dim' | 'info'; children: React.ReactNode }) {
  return <span className={`status ${kind}`} style={{ whiteSpace: 'nowrap' }}>{children}</span>;
}

function ProgressView({ p }: { p: OntologyProgress }) {
  const t = p.totals;
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>전환 현황 — 정보 조회 · 내용 분석 · 워크플로 연결</h2>
        <p className="mutedtext" style={{ fontSize: 12 }}>
          문서는 세 단계를 거쳐 쓰입니다. <b>정보 조회</b>는 색인한 본문의 구조(절·용어·표)이고, <b>내용 분석</b>은
          스키마로 정한 개념(협력사·계약…)과 관계를 규칙(②-1 양식 · ②-2 템플릿 · ②-3 경로 틀)과 LLM(③)으로 뽑은 것이며,
          <b>워크플로 연결</b>은 부서 워크플로의 단계·낱말이 그 문서·용어에 닿는 선입니다. 아래 표에서 저장소마다 멈춘 단계와
          다음 할 일을 봅니다.
        </p>
        <div className="row" style={{ gap: 10, flexWrap: 'wrap', marginTop: 10 }}>
          <StatTile label="문서" value={fmt(t.indexed)} sub={`저장소 ${fmt(t.stores)} · 본문 추출 ${fmt(t.extracted)}`} />
          <StatTile label="정보 조회 — 구조" value={pct(t.with_nodes, t.extracted)}
                    sub={`절·용어·표가 나온 문서 ${fmt(t.with_nodes)}`} />
          <StatTile label="내용 분석 — 스키마" value={`${t.schemas}/${t.stores}`} sub="스키마를 확정한 저장소" />
          <StatTile label="내용 분석 — 분석" value={pct(t.analyzed, t.extracted)}
                    sub={`분석한 문서 ${fmt(t.analyzed)} · 개체가 나온 문서 ${fmt(t.content_docs)}`} />
          <StatTile label="내용 분석 — ②-3 경로 틀" value={fmt(t.path_docs)}
                    sub={`개체가 달린 문서 · 확정 ${t.paths_confirmed}/${t.paths_possible} 저장소`} />
          <StatTile label="워크플로 연결" value={fmt(t.workflow_docs)}
                    sub={`닿는 문서 · 선 ${fmt(t.workflow_links)} · 워크플로 ${fmt(t.linked_workflows)}`} />
        </div>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>단계별 문서 수</h3>
        <p className="mutedtext" style={{ fontSize: 12 }}>
          앞 단계의 부분집합이 아닌 것도 있습니다 — ②-3 경로 틀은 분석과 상관없이 폴더 이름으로 모든 문서에 개체를 답니다.
          비어 있는 막대가 그 단계를 아직 하지 않았다는 뜻입니다.
        </p>
        <BarList unit="개 문서" rows={[
          { label: '색인된 문서', value: t.indexed },
          { label: '본문 추출', value: t.extracted },
          { label: '정보 조회 — 구조(절·용어·표)', value: t.with_nodes },
          { label: '내용 분석 — 분석한 문서(③)', value: t.analyzed },
          { label: '내용 분석 — 개체·관계가 나온 문서', value: t.content_docs },
          { label: '내용 분석 — 경로 틀로 개체가 달린 문서(②-3)', value: t.path_docs },
          { label: '워크플로 연결 — 닿는 문서', value: t.workflow_docs },
        ].map((r) => ({ ...r, color: r.value ? VIZ.series : VIZ.status.warning }))} />
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>저장소 × 단계</h3>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ fontSize: 12 }}>
            <thead>
              <tr>
                <th>저장소</th><th>문서</th><th>정보 조회</th><th>① 스키마</th><th>②-1 양식</th><th>②-2 템플릿</th>
                <th>②-3 경로 틀</th><th>③ 분석</th><th>워크플로 연결</th><th>다음 할 일</th>
              </tr>
            </thead>
            <tbody>
              {p.stores.map((r) => <StoreRow key={r.store} r={r} />)}
            </tbody>
          </table>
        </div>
        {p.errors.length > 0 && (
          <p className="error" style={{ fontSize: 12 }}>
            읽지 못한 것: {p.errors.map((e) => `${e.store} · ${e.stage}(${e.error})`).join(' / ')}
          </p>
        )}
      </div>
    </>
  );
}

function StoreRow({ r }: { r: OntologyProgressStore }) {
  const f = r.formal;
  const s = r.semantic;
  const w = r.workflow;
  const go = r.next.tab ? `/ontology/${r.next.tab}${r.next.tab === 'semantic' ? `?store=${encodeURIComponent(r.store)}` : ''}` : '';
  return (
    <tr>
      <td className="mono">{r.store}</td>
      <td className="mono">{fmt(f.indexed)}</td>
      <td>
        <Mark kind={f.extracted && f.with_nodes / f.extracted >= 0.5 ? 'ok' : f.indexed ? 'warn' : 'dim'}>
          구조 {pct(f.with_nodes, f.extracted)}
        </Mark>
        {f.extract_failed > 0 && <div className="mutedtext">추출 실패 {fmt(f.extract_failed)}</div>}
      </td>
      {!s ? <td colSpan={5}><span className="mutedtext">내용 분석 상태를 읽지 못했습니다</span></td> : (
        <>
          <td>{s.schema ? <Mark kind={s.schema.layered ? 'ok' : 'warn'}>개념 {s.schema.classes}</Mark>
            : <Mark kind="dim">없음</Mark>}</td>
          <td>{s.forms.total ? <Mark kind={s.forms.confirmed ? 'ok' : 'dim'}>{s.forms.confirmed}/{s.forms.total}</Mark>
            : <span className="mutedtext">—</span>}</td>
          <td>{s.templates.total === null ? <span className="mutedtext" title="내용 분석 화면을 열면 계산합니다">?</span>
            : s.templates.total ? <Mark kind={s.templates.confirmed ? 'ok' : 'dim'}>{s.templates.confirmed}/{s.templates.total}</Mark>
              : <span className="mutedtext">—</span>}</td>
          <td>
            {s.paths.confirmed ? (
              <>
                <Mark kind={s.paths.stale ? 'warn' : 'ok'}>{s.paths.stale ? '옛 스키마' : `${s.paths.confirmed}칸`}</Mark>
                <div className="mutedtext">문서 {fmt(s.path_docs)}{s.paths.mismatches ? ` · 불일치 ${fmt(s.paths.mismatches)}` : ''}</div>
              </>
            ) : s.paths.candidates ? <Mark kind="info">후보 {s.paths.candidates}</Mark> : <span className="mutedtext">—</span>}
          </td>
          <td>
            {!s.schema ? <span className="mutedtext">—</span> : (
              <>
                <Mark kind={s.paused ? 'warn' : done(s) >= s.documents ? 'ok' : 'info'}>
                  {s.paused ? '멈춤' : s.running ? '진행 중' : pct(done(s), s.documents)}
                </Mark>
                <div className="mutedtext">{fmt(done(s))}/{fmt(s.documents)}</div>
              </>
            )}
          </td>
        </>
      )}
      <td>
        {w.links ? (
          <>
            <Mark kind={w.pending ? 'info' : 'ok'}>워크플로 {w.workflows}</Mark>
            <div className="mutedtext">문서 {fmt(w.documents)}{w.pending ? ` · 확인 대기 ${w.pending}` : ''}</div>
          </>
        ) : <Mark kind="dim">없음</Mark>}
      </td>
      <td style={{ minWidth: 220 }}>
        {r.next.stage && <span className="mutedtext">{r.next.stage} · </span>}
        {go ? <Link to={go}>{r.next.text}</Link> : <span>{r.next.text}</span>}
      </td>
    </tr>
  );
}

function SemanticDetail({ p }: { p: OntologyProgress }) {
  const rows = p.stores.filter((r) => r.semantic);
  return (
    <>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        위 표의 진행률 뒤에 있는 것 — 분석한 문서가 어떤 길로 처리됐는지(규칙이 많을수록 LLM을 덜 씁니다), 개체가 어디서
        나왔는지, 관계·묶은 별칭 수, 멈춘 까닭입니다. 고치는 일은 <Link to="/ontology/semantic">내용 분석</Link> 탭에서 합니다.
      </p>
      <table style={{ fontSize: 12 }}>
        <thead>
          <tr><th>저장소</th><th>분석한 문서 — 처리한 길</th><th>개체 — 나온 곳</th><th>관계</th><th>별칭</th><th>멈춘 까닭</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const s = r.semantic!;
            return (
              <tr key={r.store}>
                <td className="mono">
                  <Link to={`/ontology/semantic?store=${encodeURIComponent(r.store)}`}>{r.store}</Link>
                </td>
                <td style={{ minWidth: 260 }}><Breakdown parts={ANALYZED_PARTS(s.analyzed)} total={s.documents} /></td>
                <td style={{ minWidth: 260 }}><Breakdown parts={ORIGIN_PARTS(s.nodes)} unit="개" /></td>
                <td className="mono">{fmt(s.edges)}</td>
                <td className="mono">{fmt(s.aliases)}</td>
                <td style={{ maxWidth: 320 }}>
                  {s.paused ? <span className="error" style={{ fontSize: 11 }}>{s.paused}</span>
                    : <span className="mutedtext">—</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

const SOURCE_LABEL: Record<string, string> = {
  spec: '설계', run: '실행 기록', name: '이름 일치', index: '전문 색인', llm: 'LLM 제안',
};

function WorkflowDetail({ p }: { p: OntologyProgress }) {
  return (
    <>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        위 표의 워크플로·닿는 문서 뒤에 있는 선을 근거별로 셉니다 — 설계·실행 기록·이름 일치·전문 색인은 다시 계산할 때
        바뀌고, LLM 제안은 사람이 확인해야 쓰입니다. 연결과 확인은{' '}
        <Link to="/ontology/workflows">워크플로 연결</Link> 탭에서 합니다.
      </p>
      <table style={{ fontSize: 12 }}>
        <thead><tr><th>저장소</th><th>선</th><th>근거별</th></tr></thead>
        <tbody>
          {p.stores.filter((r) => r.workflow.links > 0).map((r) => (
            <tr key={r.store}>
              <td className="mono">{r.store}</td>
              <td className="mono">{fmt(r.workflow.links)}</td>
              <td>{Object.entries(r.workflow.by_source).map(([k, v]) => `${SOURCE_LABEL[k] ?? k} ${fmt(v)}`).join(' · ')}</td>
            </tr>
          ))}
          {!p.stores.some((r) => r.workflow.links > 0) && (
            <tr><td colSpan={3} className="mutedtext">워크플로와 연결된 저장소가 없습니다.</td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}

/**
 * 정보 조회 상세 — 형식 그래프(절·용어·표). 전체 수치·단계별 문서 수·저장소별 구조 비율은 위(전환 현황)에 있으므로 여기는
 * **그것으로 답하지 못하는 것만** 둔다: 저장소별 수치와 재색인, 그래프의 구성(스키마 그림), 노드 종류 × 저장소, 문서당 구조
 * 노드 수, 추출 실패 이유. 되풀이되는 표는 내용 분석 ②-1 양식에서 다룬다(대응표를 정하는 자리).
 *
 * 시각화 방법은 기존 솔루션을 먼저 봤다.
 *  - **노드-링크는 전체 그래프에 쓰지 않는다.** 수백 노드만 넘어도 "털뭉치"가 되어 아무 질문에도 답하지 못한다는 것이
 *    알려진 결과다(Ghoniem 등 2004 / Katifori 등 2007, Dudáš 등 2018의 온톨로지 시각화 조사). 그래서 전체는 **집계**로
 *    보고, 노드-링크는 **종류 수준(4개 노드)** 에만 쓴다 — VOWL·Neo4j의 스키마 뷰·ABSTAT의 요약 그래프가 쓰는 형태다.
 *  - **격자 밀도는 행렬(히트맵)** 로 본다(저장소 × 노드 종류).
 *  - 실제 노드 탐색(이름으로 찾고 이웃 보기)은 정보 조회 탭과 paas-graph MCP가 한다.
 *
 * 색은 역할로 고르고 검증기를 돌렸다(lib/viz.tsx의 VIZ 주석에 결과를 적어 두었다).
 */
function FormalDetail() {
  const admin = isAdmin();
  const state = useApi(() => api.ontologyOverview(), []);
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const [error, setError] = useState('');

  const reindex = async (store: string, force: boolean) => {
    setBusy(store);
    setError('');
    setNote('');
    try {
      const res = await api.reindexOntology(store, force);
      setNote(
        `${store}: ${res.done ? '색인 완료' : '시간 예산까지 진행'} — `
        + `갱신 ${fmt(res.indexed ?? 0)} · 남음 ${fmt(res.remaining ?? 0)}`
        // 추출 능력이 바뀌어 다시 보는 문서 — 파일은 그대로인데 왜 할 일이 있는지의 답이다.
        + (res.retried ? ` · 다시 추출 ${fmt(res.retried)}` : '')
        + (res.done ? '' : ' (다시 누르면 이어서 진행합니다)'),
      );
      state.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  return (
    <Async state={state}>
      {(data: OntologyOverview) => {
        const t = data.totals;
        const kinds = Object.keys(t.node_kinds);
        return (
          <>
            {data.errors.length > 0 && (
              <p className="error" style={{ fontSize: 12 }}>
                읽지 못한 저장소: {data.errors.map((e) => `${e.store}(${e.error})`).join(' · ')}
              </p>
            )}

            <h4 style={{ margin: '4px 0' }}>저장소별 수치</h4>
            <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 4px' }}>
              「구조 없음」은 본문은 읽혔는데 제목·표·정의문이 없어 문서 노드 하나만 남은 문서입니다(검색에는 걸립니다).
              「실패」는 원본 형식(스캔 PDF·97-2003) 때문에 본문을 읽지 못한 문서입니다 — 이유는 맨 아래에 있습니다.
            </p>
            <table style={{ fontSize: 12 }}>
              <thead>
                <tr>
                  <th>저장소</th><th>색인</th><th>추출</th><th>구조</th><th>구조 없음</th>
                  <th>실패</th><th>노드</th><th>관계</th>
                  {admin && <th>동작</th>}
                </tr>
              </thead>
              <tbody>
                {data.stores.map((st: OntologyStore) => (
                  <tr key={st.store}>
                    <td>{st.store}</td>
                    <td className="mono">{fmt(st.indexed)}</td>
                    <td className="mono">{fmt(st.extracted)}</td>
                    <td className="mono">{fmt(st.with_nodes)}</td>
                    <td className="mono">{fmt(st.no_nodes)}</td>
                    <td className="mono">{fmt(st.extract_failed)}</td>
                    <td className="mono">{fmt(st.nodes)}</td>
                    <td className="mono">{fmt(st.edges)}</td>
                    {admin && (
                      <td>
                        <button className="small secondary" disabled={!!busy}
                                onClick={() => reindex(st.store, false)}>
                          {busy === st.store ? '색인 중…' : '재색인'}
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
            {t.stale_nodes > 0 && (
              <p className="mutedtext" style={{ fontSize: 12 }}>
                옛 노드 {fmt(t.stale_nodes)}개 — 본문을 읽지 못하게 된 문서에 남은 것입니다. 위쪽의 「클린징」이 지웁니다.
              </p>
            )}
            {note && <p style={{ fontSize: 12, color: 'var(--green)' }}>{note}</p>}
            {error && <p className="error">{error}</p>}

            <h4 style={{ margin: '16px 0 4px' }}>그래프 구성</h4>
            <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 4px' }}>
              이 그래프가 무엇을 어떻게 잇는지 — 상자 안의 수는 노드, 선 위의 수는 관계(전체 합계)입니다.
            </p>
            {/* 픽셀 고정(620)이라 좁은 칸에서는 넘친다 — 패널 밖으로 새지 않게 가로 스크롤. */}
            <div style={{ overflowX: 'auto' }}>
              <SchemaDiagram nodeKinds={t.node_kinds} edgeKinds={t.edge_kinds} />
            </div>

            <h4 style={{ margin: '16px 0 4px' }}>노드 종류 × 저장소</h4>
            <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 4px' }}>
              표가 많은 저장소(대장·점검표)와 용어가 많은 저장소(규정)는 쓰는 방법이 다릅니다. 저장소는 원으로 찍혀 있고,
              마우스를 올리면 이름이 아래에 뜹니다(넘치면 오른쪽으로 스크롤).
            </p>
            <Heatmap
              rows={kinds}
              columns={data.stores.map((st) => st.store)}
              cellSize={22}
              columnHeader="dot"
              value={(row, col) => data.stores.find((st) => st.store === col)?.node_kinds[row] ?? 0}
            />

            <h4 style={{ margin: '16px 0 4px' }}>문서당 구조 노드 수</h4>
            <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 4px' }}>
              절·용어·표만 셉니다(문서 노드는 모든 문서에 하나씩 있어 세지 않습니다). 0건은 「구조 없음」입니다.
            </p>
            <BarList unit="개 문서"
                     rows={Object.entries(t.node_buckets).map(([label, value]) => ({
                       label, value,
                       color: label.startsWith('0') ? VIZ.status.warning : VIZ.series,
                     }))} />

            {Object.keys(data.failure_reasons).length > 0 && (
              <>
                <h4 style={{ margin: '16px 0 4px' }}>추출 실패 이유</h4>
                <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 4px' }}>
                  그때 색인한 시점의 이유입니다 — 추출기를 그 뒤에 설치했다면 재색인하면 전환됩니다.
                </p>
                <table style={{ fontSize: 12 }}>
                  <thead><tr><th>이유</th><th>문서 수</th></tr></thead>
                  <tbody>
                    {Object.entries(data.failure_reasons).map(([reason, n]) => (
                      <tr key={reason}>
                        <td>{reason}</td>
                        <td className="mono">{fmt(n)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </>
        );
      }}
    </Async>
  );
}

/**
 * 스키마 수준 노드-링크 — 노드 4종, 관계 3종. **이 크기에서만** 노드-링크가 읽힌다.
 *
 * 실제 연결 규칙은 추출기에서 온다(services/ontology.extract):
 * 문서→절(포함), 절→절(중첩 포함), 절→표(포함), 절→용어(정의), 절→문서(인용).
 */
function SchemaDiagram({ nodeKinds, edgeKinds }: {
  nodeKinds: Record<string, number>;
  edgeKinds: Record<string, number>;
}) {
  const W = 620;
  const H = 210;
  const box = { w: 120, h: 44 };
  const at: Record<string, { x: number; y: number; label: string }> = {
    document: { x: 20, y: 80, label: '문서' },
    section: { x: 230, y: 80, label: '절' },
    term: { x: 460, y: 20, label: '용어' },
    table: { x: 460, y: 140, label: '표' },
  };
  const count = (k: string) => nodeKinds[k] ?? 0;
  const rel = (k: string) => edgeKinds[k] ?? 0;
  const edge = (from: string, to: string, label: string, n: number, dy = 0) => {
    const a = at[from];
    const b = at[to];
    const x1 = a.x + box.w;
    const y1 = a.y + box.h / 2 + dy;
    const x2 = b.x;
    const y2 = b.y + box.h / 2;
    const mx = (x1 + x2) / 2;
    return (
      <g key={`${from}-${to}-${label}`}>
        <path d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
              fill="none" stroke={VIZ.axis} strokeWidth={2} />
        <text x={mx} y={(y1 + y2) / 2 - 6} textAnchor="middle" fill={VIZ.muted} fontSize="11">
          {label} {n > 0 ? `(${fmt(n)})` : ''}
        </text>
      </g>
    );
  };
  // 폭을 100%로 늘리면 글자까지 커진다(viz.useWidth 주석 참고) — 픽셀 크기로 그린다.
  return (
    <svg width={W} height={H} role="img"
         aria-label="온톨로지 스키마: 문서·절·용어·표와 포함·정의·인용 관계">
      {edge('document', 'section', '포함', rel('contains'))}
      {edge('section', 'term', '정의', rel('defines'))}
      {edge('section', 'table', '포함', 0)}
      {/* 절→절 중첩(자기 참조)과 절→문서 인용은 선으로 그리면 교차만 늘어난다 — 글로 적는다. */}
      {Object.entries(at).map(([kind, pos]) => (
        <g key={kind}>
          <rect x={pos.x} y={pos.y} width={box.w} height={box.h} rx={6}
                fill="none" stroke={VIZ.series} strokeWidth={2} />
          <text x={pos.x + box.w / 2} y={pos.y + 19} textAnchor="middle"
                fill={VIZ.ink} fontSize="13">{pos.label}</text>
          <text x={pos.x + box.w / 2} y={pos.y + 35} textAnchor="middle"
                fill={VIZ.muted} fontSize="11"
                style={{ fontVariantNumeric: 'tabular-nums' }}>
            {fmt(count(kind))}
          </text>
        </g>
      ))}
      <text x={20} y={H - 10} fill={VIZ.muted} fontSize="11">
        절은 절을 다시 포함하고(중첩), 절이 다른 문서를 인용합니다
        {rel('references') > 0 ? ` — 인용 ${fmt(rel('references'))}건` : ''}
      </text>
    </svg>
  );
}
