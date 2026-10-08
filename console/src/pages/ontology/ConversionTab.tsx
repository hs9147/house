import { useState } from 'react';
import Async from '../../components/Async';
import Split from '../../components/Split';
import Tabs from '../../components/Tabs';
import { api } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import { isAdmin } from '../../lib/auth';
import type { OntologyOverview, OntologyStore } from '../../lib/types';
import { BarList, FunnelBars, Heatmap, StackedBars, StatTile, VIZ, fmt } from '../../lib/viz';

/**
 * 온톨로지 관리 — 문서가 그래프로 **얼마나 옮겨졌고 어디서 멈췄는지**.
 *
 * 시각화 방법은 기존 솔루션을 먼저 봤고, 그 결론이 이 화면의 구성이다.
 *
 *  - **노드-링크는 전체 그래프에 쓰지 않는다.** 수백 노드만 넘어도 "털뭉치"가 되어 아무
 *    질문에도 답하지 못한다는 것이 알려진 결과다(Ghoniem 등 2004: 밀도가 오르면 행렬이
 *    노드-링크를 이긴다 / Katifori 등 2007, Dudáš 등 2018의 온톨로지 시각화 조사도 같은
 *    결론). 그래서 전체는 **집계**로 보고, 노드-링크는 **종류 수준(4개 노드)** 에만 쓴다 —
 *    VOWL·Neo4j의 스키마 뷰·ABSTAT의 요약 그래프가 그 자리에 쓰는 형태다.
 *  - **격자 밀도는 행렬(히트맵)** 로 본다(저장소 × 노드 종류).
 *  - **전환은 파이프라인**이라 단계별 감소를 퍼널로 본다 — 데이터 품질 대시보드(Luzzu,
 *    SHACL 적합성 리포트, Great Expectations의 docs)가 쓰는 방식이고, 사람이 고칠 자리는
 *    "어디서 떨어졌나"에 있다.
 *  - 실제 노드 탐색(이름으로 찾고 이웃 보기)은 이미 paas-graph MCP가 한다 — 여기서 같은
 *    것을 또 만들지 않고, 예시 노드만 보여 준다.
 *
 * 색은 역할로 고르고 검증기를 돌렸다(lib/viz.tsx의 VIZ 주석에 결과를 적어 두었다).
 */
export default function ConversionTab() {
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
        const summary = (
          <>
            <div className="panel">
              <h2>온톨로지 전환 현황</h2>
              <p className="mutedtext" style={{ fontSize: 12 }}>
                온톨로지는 문서 색인의 파생물입니다 — 색인이 돌 때 같은 추출에서 노드와 관계가
                만들어집니다. 그래서 아래 수치는 추정이 아니라 색인 DB를 센 값이고, 전환을
                진행시키는 수단은 재색인 하나입니다.
              </p>
              <div className="row" style={{ gap: 10, flexWrap: 'wrap', marginTop: 10 }}>
                <StatTile label="저장소" value={fmt(t.stores)} />
                <StatTile label="색인된 문서" value={fmt(t.indexed)}
                          sub={`본문 추출 ${fmt(t.extracted)}`} />
                <StatTile label="구조 추출된 문서" value={fmt(t.with_nodes)}
                          sub={`추출 대비 ${t.conversion_rate}%`} />
                <StatTile label="노드" value={fmt(t.nodes)} />
                <StatTile label="관계" value={fmt(t.edges)} />
                {t.stale_nodes > 0 && (
                  <StatTile label="옛 노드" value={fmt(t.stale_nodes)}
                            sub="본문을 못 읽는 문서에 남음 — 재색인하면 정리됩니다" />
                )}
              </div>
              {data.errors.length > 0 && (
                <p className="error" style={{ fontSize: 12 }}>
                  읽지 못한 저장소: {data.errors.map((e) => `${e.store}(${e.error})`).join(' · ')}
                </p>
              )}
            </div>

            <div className="panel">
              <h3 style={{ marginTop: 0 }}>전환 파이프라인</h3>
              <p className="mutedtext" style={{ fontSize: 12 }}>
                단계마다 직전 단계의 부분집합입니다. 떨어지는 폭이 곧 고칠 자리입니다 —
                추출에서 떨어지면 원본 형식(스캔 PDF·97-2003), 구조에서 떨어지면 문서 자체에
                제목·표·정의문이 없는 것이 원인입니다. 문서 노드는 모든 문서에 하나씩 생기므로
                전환의 근거로 세지 않습니다 — 그렇게 세면 평문 코퍼스도 100%로 보입니다.
              </p>
              <FunnelBars stages={data.funnel} />
            </div>

          </>
        );
        const detail = (
          <>
            <div className="panel">
              <h3 style={{ marginTop: 0 }}>저장소별 전환 상태</h3>
              {/* 같은 값을 그림과 표로 — 같은 자리에서 바꿔 본다(components/Tabs). */}
              <Tabs tabs={[
                { key: 'chart', label: '그림', content: (
                  <>
              <StackedBars
                legend={[
                  { key: 'converted', label: '구조 추출됨', color: VIZ.status.good, mark: '●' },
                  { key: 'no_nodes', label: '구조 없음(본문만 읽힘)', color: VIZ.status.warning, mark: '▲' },
                  { key: 'failed', label: '추출 실패', color: VIZ.status.critical, mark: '■' },
                ]}
                rows={data.stores.map((s) => ({
                  label: s.store,
                  parts: [
                    { key: '구조 추출됨', value: s.with_nodes, color: VIZ.status.good },
                    { key: '구조 없음', value: s.no_nodes, color: VIZ.status.warning },
                    { key: '추출 실패', value: s.extract_failed, color: VIZ.status.critical },
                  ],
                }))}
              />
                  </>
                ) },
                { key: 'table', label: '표 · 저장소별 조치', content: (
                  <>
                <table style={{ marginTop: 8 }}>
                  <thead>
                    <tr>
                      <th>저장소</th><th>색인</th><th>추출</th><th>구조</th><th>구조 없음</th>
                      <th>실패</th><th>노드</th><th>관계</th>
                      {admin && <th>동작</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {data.stores.map((s: OntologyStore) => (
                      <tr key={s.store}>
                        <td>{s.store}</td>
                        <td className="mono">{fmt(s.indexed)}</td>
                        <td className="mono">{fmt(s.extracted)}</td>
                        <td className="mono">{fmt(s.with_nodes)}</td>
                        <td className="mono">{fmt(s.no_nodes)}</td>
                        <td className="mono">{fmt(s.extract_failed)}</td>
                        <td className="mono">{fmt(s.nodes)}</td>
                        <td className="mono">{fmt(s.edges)}</td>
                        {admin && (
                          <td>
                            <button className="small secondary" disabled={!!busy}
                                    onClick={() => reindex(s.store, false)}>
                              {busy === s.store ? '색인 중…' : '재색인'}
                            </button>
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
                  </>
                ) },
              ]} />
              {note && <p style={{ fontSize: 12, color: 'var(--green)' }}>{note}</p>}
              {error && <p className="error">{error}</p>}
            </div>

            <div className="panel">
              <h3 style={{ marginTop: 0 }}>그래프 구성</h3>
              <div className="row" style={{ gap: 24, flexWrap: 'wrap', alignItems: 'flex-start' }}>
                <div style={{ flex: '1 1 300px' }}>
                  <div className="mutedtext" style={{ fontSize: 12, marginBottom: 4 }}>
                    노드 종류
                  </div>
                  <BarList rows={Object.entries(t.node_kinds)
                    .map(([label, value]) => ({ label, value }))} />
                </div>
                <div style={{ flex: '1 1 300px' }}>
                  <div className="mutedtext" style={{ fontSize: 12, marginBottom: 4 }}>
                    관계 종류
                  </div>
                  <BarList rows={Object.entries(t.edge_kinds)
                    .map(([label, value]) => ({ label, value }))} />
                </div>
              </div>
              <div className="mutedtext" style={{ fontSize: 12, margin: '12px 0 4px' }}>
                스키마 — 이 그래프가 **무엇을 어떻게** 잇는지(개수는 전체 합계)
              </div>
              <SchemaDiagram nodeKinds={t.node_kinds} edgeKinds={t.edge_kinds} />
            </div>

            <div className="panel">
              <h3 style={{ marginTop: 0 }}>저장소 × 노드 종류</h3>
              <p className="mutedtext" style={{ fontSize: 12 }}>
                어느 저장소가 어떤 구조를 많이 담고 있는지 — 표가 많은 저장소(대장·점검표)와
                용어가 많은 저장소(규정)는 쓰는 방법이 다릅니다.
              </p>
              <Heatmap
                rows={data.stores.map((s) => s.store)}
                columns={kinds}
                value={(row, col) =>
                  data.stores.find((s) => s.store === row)?.node_kinds[col] ?? 0}
              />
            </div>

            <div className="panel">
              <h3 style={{ marginTop: 0 }}>문서당 구조 노드 수</h3>
              <p className="mutedtext" style={{ fontSize: 12 }}>
                절·용어·표만 셉니다(문서 노드는 모든 문서에 하나씩 있어 전환의 근거가 아닙니다).
                0건은 "적다"가 아니라 "전환되지 않았다"입니다 — 검색에는 걸리지만 그래프에는
                문서 노드 하나만 남은 문서입니다.
              </p>
              <BarList unit="개 문서"
                       rows={Object.entries(t.node_buckets).map(([label, value]) => ({
                         label, value,
                         color: label.startsWith('0') ? VIZ.status.warning : VIZ.series,
                       }))} />
            </div>

            {data.table_schemas.length > 0 && (
              <div className="panel">
                <h3 style={{ marginTop: 0 }}>되풀이되는 표 스키마</h3>
                <p className="mutedtext" style={{ fontSize: 12 }}>
                  사내 문서의 점검표·대장·양식은 사실상 레코드 타입이고, 머리글이 곧 스키마입니다 —
                  같은 머리글이 여러 문서에 나오면 그것이 이 조직의 데이터 모델입니다.
                </p>
                <table>
                  <thead>
                    <tr><th>저장소</th><th>컬럼</th><th>문서 수</th></tr>
                  </thead>
                  <tbody>
                    {data.table_schemas.map((s, i) => (
                      <tr key={`${s.store}-${i}`}>
                        <td>{s.store}</td>
                        <td className="mono" style={{ fontSize: 12 }}>
                          {s.columns.join(' | ')}
                        </td>
                        <td className="mono">{fmt(s.documents)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {Object.keys(data.failure_reasons).length > 0 && (
              <div className="panel">
                <h3 style={{ marginTop: 0 }}>추출 실패 이유</h3>
                <p className="mutedtext" style={{ fontSize: 12 }}>
                  **그때 색인한 시점의 이유**입니다 — 추출기를 그 뒤에 설치했다면 이 문서들은
                  재색인하면 전환됩니다(이유 문구는 재색인할 때 갱신됩니다).
                </p>
                <table>
                  <thead><tr><th>이유</th><th>문서 수</th></tr></thead>
                  <tbody>
                    {Object.entries(data.failure_reasons).map(([reason, n]) => (
                      <tr key={reason}>
                        <td style={{ fontSize: 12 }}>{reason}</td>
                        <td className="mono">{fmt(n)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        );
        // 좌: 전체 요약과 파이프라인(읽고 판단하는 것) · 우: 저장소별 분포·행렬·표
        // (근거 자료). 그림이 아홉 개라 한 화면을 넘으므로 2단 규약을 쓴다.
        return <Split leftLabel="요약" left={summary} right={detail} />;
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
  return (
    {/* 폭을 100%로 늘리면 글자까지 커진다(viz.useWidth 주석 참고) — 픽셀 크기로 그린다. */}
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
