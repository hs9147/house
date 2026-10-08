import { useCallback, useEffect, useMemo, useState } from 'react';
import ForceGraph, { type GraphLink, type GraphNode } from '../../components/ForceGraph';
import Split from '../../components/Split';
import { api } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import type { OntologyNeighbor, OntologyNode, OntologyOverview } from '../../lib/types';
import { VIZ, fmt } from '../../lib/viz';

/**
 * 정보 조회 — 추출된 온톨로지를 **움직이는 그래프로** 본다.
 *
 * 표시 방법은 기존 솔루션을 먼저 봤다.
 *
 *  - **전체를 그리지 않는다.** 노드 28,301개를 한 장에 뿌리면 아무 질문에도 답하지 못하는
 *    털뭉치가 된다. 쓰이는 규약은 20년 전부터 같다: **찾고 · 문맥을 보고 · 필요한 데서만
 *    펼친다**(van Ham & Perer 2009, "Search, Show Context, Expand on Demand"). Neo4j
 *    Browser의 더블클릭 확장, AWS Graph Explorer의 "캔버스에 추가", Linkurious의
 *    expand/collapse가 모두 이 규약의 구현이다. 이 화면도 그대로 따른다.
 *  - **같은 이름은 문서를 넘어 하나로 묶는다.** 사내 문서는 같은 양식이 수백 번 되풀이된다
 *    (견적서 표 스키마 320건) — 묶지 않으면 한 걸음에 수백 노드가 쏟아진다. RDF 요약
 *    연구(ABSTAT·LODeX)와 Neo4j의 스키마 뷰가 쓰는 집계 아이디어를 노드 수준에 쓴 것이다.
 *  - **배치는 계층(dagre), 물리 시뮬레이션이 아니다.** 우리 그래프는 문서→절→절→표로
 *    내려가는 거의 트리다(관계의 대부분이 contains). force 레이아웃(vis-network·d3-force·
 *    Obsidian 그래프 뷰의 그 느낌)은 볼 때마다 모양이 달라져 같은 질문을 두 번 물을 수 없고,
 *    계층을 흐린다. "유동적"은 배치가 떨리는 것이 아니라 **사람이 끌어 옮기고 펼치는 것**으로
 *    낸다 — 끌기·확대·미니맵은 React Flow가 한다(C4·토폴로지 화면과 같은 부품).
 *  - 노드-링크는 이 크기(수십 개)에서만 읽힌다. 전체 분포는 옆 탭(전환 현황)의 집계가
 *    맡는다 — 밀도가 높아지면 행렬이 노드-링크를 이긴다는 것이 알려진 결과다(Ghoniem 2004).
 *
 * 색은 종류를 말하고(검증된 ordinal 램프), 종류 이름을 **글자로도** 적는다 — 색만으로
 * 뜻을 나르지 않는다(dataviz 규칙).
 */

const KINDS: { key: string; label: string; color: string; mark: string }[] = [
  { key: 'document', label: '문서', color: VIZ.ordinal[4], mark: '▣' },
  { key: 'section', label: '절', color: VIZ.ordinal[3], mark: '▦' },
  { key: 'term', label: '용어', color: VIZ.ordinal[2], mark: '◆' },
  { key: 'table', label: '표', color: VIZ.ordinal[1], mark: '▤' },
];

const REL_LABEL: Record<string, string> = {
  contains: '포함', defines: '정의', references: '인용',
};

const kindOf = (kind: string) => KINDS.find((k) => k.key === kind);
const idOf = (kind: string, name: string) => `${kind}:${name}`;

interface Placed {
  kind: string;
  name: string;
  documents: number;
  detail: string | null;
  // 이 노드에서 한 걸음 펼쳤는가 — 펼치지 않은 노드는 "+"로 표시해 할 일이 남았음을 보인다.
  expanded: boolean;
}

interface Link {
  source: string;
  target: string;
  rel: string;
  documents: number;
}

export default function ExploreTab() {
  const overview = useApi(() => api.ontologyOverview(), []);
  const [store, setStore] = useState('');
  const [kind, setKind] = useState('');
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<OntologyNode[] | null>(null);
  const [nodes, setNodes] = useState<Map<string, Placed>>(new Map());
  const [links, setLinks] = useState<Link[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [paths, setPaths] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');

  // 저장소는 그래프가 있는 것만 — 그래프는 저장소마다 따로 든 SQLite에 있다.
  const stores = useMemo(() => {
    const data = overview.data as OntologyOverview | undefined;
    return (data?.stores ?? []).filter((s) => s.nodes > 0);
  }, [overview.data]);

  useEffect(() => {
    if (!store && stores.length > 0) setStore(stores[0].store);
  }, [stores, store]);

  const reset = () => {
    setNodes(new Map());
    setLinks([]);
    setSelected('');
    setPaths([]);
    setNotice('');
  };

  const search = async () => {
    setBusy(true);
    setError('');
    try {
      const res = await api.searchOntologyNodes(store, query.trim(), kind, 25);
      setHits(res.nodes);
      if (res.nodes.length === 0) setNotice('찾은 노드가 없습니다.');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  /** 한 걸음 펼친다 — 이 노드의 이웃을 캔버스에 더한다(van Ham & Perer의 expand on demand). */
  const expand = useCallback(async (kindOfNode: string, name: string, replace = false) => {
    setBusy(true);
    setError('');
    try {
      const res = await api.ontologyNeighbors(store, kindOfNode, name, 30);
      const id = idOf(kindOfNode, name);
      const next = new Map(replace ? [] : nodes);
      next.set(id, {
        kind: kindOfNode, name, documents: res.node.documents,
        detail: null, expanded: true,
      });
      const add = (n: OntologyNeighbor) => {
        const nid = idOf(n.kind, n.name);
        if (!next.has(nid)) {
          next.set(nid, {
            kind: n.kind, name: n.name, documents: n.documents,
            detail: n.detail, expanded: false,
          });
        }
      };
      res.out.forEach(add);
      res.in.forEach(add);
      const fresh: Link[] = [
        ...res.out.map((n) => ({
          source: id, target: idOf(n.kind, n.name), rel: n.rel, documents: n.documents,
        })),
        ...res.in.map((n) => ({
          source: idOf(n.kind, n.name), target: id, rel: n.rel, documents: n.documents,
        })),
      ];
      setNodes(next);
      setLinks((prev) => {
        const seen = new Set<string>();
        return [...(replace ? [] : prev), ...fresh].filter((l) => {
          const key = `${l.source}|${l.rel}|${l.target}`;
          if (seen.has(key)) return false;
          seen.add(key);
          return next.has(l.source) && next.has(l.target);
        });
      });
      setSelected(id);
      setPaths(res.node.paths);
      setNotice(res.truncated
        ? '이웃이 상한(30)을 넘어 일부만 가져왔습니다 — 문서 수가 많은 것부터입니다.'
        : `${name} — 들어오는 ${res.in.length} · 나가는 ${res.out.length}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [store, nodes]);

  // Obsidian·TheBrain 그래프 뷰 스타일 — 힘기반 배치(components/ForceGraph).
  const graphNodes: GraphNode[] = useMemo(
    () => [...nodes.entries()].map(([id, n]) => ({
      id,
      label: n.name,
      kind: n.kind,
      weight: n.documents,
      unexpanded: !n.expanded,
    })),
    [nodes]);

  const graphLinks: GraphLink[] = useMemo(
    () => links.map((l) => ({ source: l.source, target: l.target,
                              label: REL_LABEL[l.rel] ?? l.rel })),
    [links]);

  const current = selected ? nodes.get(selected) : undefined;

  const searchPanel = (
    <>
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>정보 조회</h3>
        <p className="mutedtext" style={{ fontSize: 12 }}>
          이름으로 찾고, 노드를 눌러 한 걸음씩 펼칩니다. 전체 그래프를 한 장에 그리지
          않습니다 — 수만 노드를 뿌리면 아무 질문에도 답하지 못합니다. 같은 이름은 문서를
          넘어 하나로 묶여 있고(문서 N건), 노드를 끌어 옮기면 배치는 그대로 유지됩니다.
        </p>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <select value={store} onChange={(e) => { setStore(e.target.value); reset(); setHits(null); }}>
            {stores.map((s) => (
              <option key={s.store} value={s.store}>
                {s.store} (노드 {fmt(s.nodes)})
              </option>
            ))}
          </select>
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">모든 종류</option>
            {KINDS.map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}
          </select>
          <input
            placeholder="이름 일부 — 예: 연차, 견적서, 제3조"
            value={query}
            style={{ minWidth: 240 }}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && store) search(); }}
          />
          <button className="small" disabled={!store || busy} onClick={search}>
            {busy ? '조회 중…' : '찾기'}
          </button>
          {nodes.size > 0 && (
            <button className="small secondary" onClick={reset}>캔버스 비우기</button>
          )}
        </div>
        {stores.length === 0 && !overview.loading && (
          <p className="mutedtext" style={{ fontSize: 12 }}>
            아직 그래프가 있는 저장소가 없습니다 — 전환 현황 탭에서 재색인하면 생깁니다.
          </p>
        )}
        {error && <p className="error">{error}</p>}
      </div>

      {hits && hits.length > 0 && (
        <div className="panel">
          <h4 style={{ margin: '0 0 6px' }}>찾은 노드 {hits.length}개</h4>
          <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
            문서 수가 많은 것부터입니다 — 되풀이되는 양식·용어가 이 조직의 데이터 모델입니다.
          </p>
          <table>
            <thead><tr><th>종류</th><th>이름</th><th>문서</th><th></th></tr></thead>
            <tbody>
              {hits.map((n) => (
                <tr key={idOf(n.kind, n.name)}>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <span aria-hidden style={{ color: kindOf(n.kind)?.color, marginRight: 4 }}>
                      {kindOf(n.kind)?.mark ?? '●'}
                    </span>
                    {kindOf(n.kind)?.label ?? n.kind}
                  </td>
                  <td style={{ fontSize: 12, maxWidth: 520, overflowWrap: 'anywhere' }}>
                    {n.name}
                  </td>
                  <td className="mono">{fmt(n.documents)}</td>
                  <td>
                    <button className="small secondary" disabled={busy}
                            onClick={() => expand(n.kind, n.name, true)}>
                      그래프로 보기
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

    </>
  );

  const graphPanel = nodes.size > 0 ? (
        <div className="panel">
          <div className="row" style={{ gap: 14, flexWrap: 'wrap', marginBottom: 6 }}>
            {KINDS.map((k) => (
              <span key={k.key} style={{ fontSize: 12, color: VIZ.muted }}>
                <span aria-hidden style={{ color: k.color, marginRight: 4 }}>{k.mark}</span>
                {k.label}
              </span>
            ))}
            <span style={{ fontSize: 12, color: VIZ.muted }}>
              노드 {fmt(nodes.size)} · 선 {fmt(links.length)} — 노드를 누르면 한 걸음 펼칩니다
            </span>
          </div>
          <ForceGraph
            nodes={graphNodes}
            links={graphLinks}
            selected={selected}
            colorOf={(kind) => kindOf(kind)?.color ?? VIZ.series}
            onSelect={(id) => {
              setSelected(id);
              const placed = nodes.get(id);
              if (placed) setPaths([]);   // 상세는 펼칠 때 서버가 준다
            }}
            onExpand={(id) => {
              const placed = nodes.get(id);
              if (placed) expand(placed.kind, placed.name);
            }}
          />
          {notice && <p className="mutedtext" style={{ fontSize: 12 }}>{notice}</p>}
          {current && (
            <div style={{ marginTop: 8 }}>
              <h4 style={{ margin: '0 0 4px' }}>
                <span aria-hidden style={{ color: kindOf(current.kind)?.color, marginRight: 6 }}>
                  {kindOf(current.kind)?.mark ?? '●'}
                </span>
                {current.name}
              </h4>
              <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
                {kindOf(current.kind)?.label ?? current.kind}
                {current.detail ? ` · ${current.detail}` : ''}
                {` · 이 이름이 나오는 문서 ${fmt(current.documents)}건`}
              </p>
              {paths.length > 0 && (
                <ul className="mono" style={{ fontSize: 12, margin: '4px 0 0 18px' }}>
                  {paths.map((p) => <li key={p}>{p}</li>)}
                  {current.documents > paths.length && (
                    <li className="mutedtext">
                      … 그 외 {fmt(current.documents - paths.length)}건
                    </li>
                  )}
                </ul>
              )}
            </div>
          )}
        </div>
  ) : (
    <div className="panel">
      <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
        왼쪽에서 이름을 찾아 「그래프로 보기」를 누르면 여기에 그려집니다.
      </p>
    </div>
  );

  // 좌: 찾고 고르는 자리(사람) · 우: 그려진 그래프(기계가 낸 것). 콘솔 2단 규약과 같다.
  return <Split leftLabel="검색" left={searchPanel} right={graphPanel} />;
}
