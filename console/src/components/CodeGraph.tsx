import { useMemo, useState } from 'react';
import {
  codeIndex, type CodeEdge, type CodeInfo, type CodeKind, hasMore, neighbors, ROOT, searchCode,
} from '../lib/codegraph';
import type { CodeMapFile } from '../lib/types';
import { fmt, VIZ } from '../lib/viz';
import CodeStructure from './CodeStructure';
import ForceGraph, { type GraphLink, type GraphNode } from './ForceGraph';

// 종류마다 색·표식 — 온톨로지 조회(pages/ontology/ExploreTab)와 같은 팔레트·같은 읽는 법이다.
const KINDS: { key: CodeKind; label: string; color: string; mark: string }[] = [
  { key: 'dir', label: '폴더', color: VIZ.ordinal[4], mark: '▣' },
  { key: 'file', label: '파일', color: VIZ.ordinal[3], mark: '▦' },
  { key: 'class', label: '클래스', color: VIZ.ordinal[2], mark: '◆' },
  { key: 'function', label: '함수', color: VIZ.ordinal[1], mark: '●' },
  { key: 'external', label: '외부 클래스', color: VIZ.muted, mark: '◇' },
];
const ROOT_KIND = { label: '리포', color: VIZ.ink, mark: '◎' };
const QUERY_KIND = { label: '검색어', color: VIZ.ink, mark: '◎' };
const REL_LABEL: Record<string, string> = { contains: '포함', defines: '정의', inherits: '상속', match: '찾음' };

const kindOf = (kind: string) => KINDS.find((k) => k.key === kind)
  ?? (kind === 'root' ? ROOT_KIND : kind === 'query' ? QUERY_KIND : undefined);

/**
 * code 레벨의 지식 그래프 — 리포에서 한 걸음씩 펼친다(lib/codegraph). 노드를 누르면 고르고, 테두리가 빈
 * 노드(더 있다)는 함께 펼친다. 이름으로 찾으면 찾은 것들이 검색어 노드를 둘러싼다.
 */
export default function CodeGraph({ files }: { files: CodeMapFile[] }) {
  const idx = useMemo(() => codeIndex(files), [files]);
  const start = () => {
    const first = neighbors(idx, ROOT, new Set([ROOT]));
    return { ids: new Set([ROOT, ...first.edges.map((e) => e.target)]), edges: first.edges,
             expanded: new Set([ROOT]), note: first.more ? `리포 아래 ${first.more}개는 더 펼치지 않았습니다` : '' };
  };
  const [state, setState] = useState(start);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string>(ROOT);
  // 찾기 노드 — 그래프에는 있지만 리포의 것이 아니다(색인 밖).
  const [queryNode, setQueryNode] = useState<CodeInfo | null>(null);

  const infoOf = (id: string): CodeInfo | undefined => (queryNode && id === queryNode.id ? queryNode : idx.info.get(id));

  const expand = (id: string) => {
    if (id === queryNode?.id) return;
    setState((s) => {
      const got = neighbors(idx, id, s.ids);
      const ids = new Set(s.ids);
      got.edges.forEach((e) => { ids.add(e.source); ids.add(e.target); });
      const label = idx.info.get(id)?.label ?? id;
      return { ids, edges: [...s.edges, ...got.edges], expanded: new Set([...s.expanded, id]),
               note: got.more ? `${label} — ${got.edges.length}개를 펼쳤고 ${got.more}개가 더 있습니다(다시 누르면 이어서)`
                 : `${label} — ${got.edges.length}개를 펼쳤습니다` };
    });
  };

  const search = () => {
    const hits = searchCode(idx, query);
    const q: CodeInfo = { id: 'query', label: `“${query.trim()}”`, kind: 'query', weight: 30 };
    setQueryNode(q);
    setSelected(q.id);
    setState({ ids: new Set([q.id, ...hits.map((h) => h.id)]),
               edges: hits.map((h) => ({ source: q.id, target: h.id, rel: 'match' as const })),
               expanded: new Set([q.id]),
               note: hits.length ? `찾은 ${hits.length}개 — 노드를 누르면 그 자리(폴더·파일·상속)로 펼칩니다`
                 : '찾은 것이 없습니다' });
  };
  const reset = () => { setQueryNode(null); setSelected(ROOT); setState(start()); };

  const nodes: GraphNode[] = useMemo(() => [...state.ids].map((id) => {
    const n = infoOf(id)!;
    // 펼친 노드라도 상한에 걸려 남은 이웃이 있으면 다시 비운다 — 누르면 이어서 펼친다.
    const more = id !== queryNode?.id && hasMore(idx, id, state.ids);
    return { id, label: n.label, kind: n.kind, weight: n.kind === 'query' || n.kind === 'root' ? 30 : n.weight,
             unexpanded: more, fixed: n.kind === 'query' || (n.kind === 'root' && !queryNode) };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [state, idx, queryNode]);
  const links: GraphLink[] = useMemo(
    () => state.edges.map((e: CodeEdge) => ({ source: e.source, target: e.target, label: REL_LABEL[e.rel] })),
    [state.edges]);

  const current = infoOf(selected);

  return (
    <div>
      <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 6 }}>
        <input placeholder="이름 일부 — 파일·클래스·함수" value={query} style={{ minWidth: 220 }}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && query.trim()) search(); }} />
        <button className="small" disabled={!query.trim()} onClick={search}>찾기</button>
        <button className="small secondary" onClick={reset}>처음부터(리포)</button>
      </div>
      <div className="row" style={{ gap: 14, flexWrap: 'wrap', marginBottom: 6 }}>
        {KINDS.map((k) => (
          <span key={k.key} style={{ fontSize: 12, color: VIZ.muted }}>
            <span aria-hidden style={{ color: k.color, marginRight: 4 }}>{k.mark}</span>{k.label}
          </span>
        ))}
        <span style={{ fontSize: 12, color: VIZ.muted }}>
          노드 {fmt(state.ids.size)} · 선 {fmt(state.edges.length)} — 테두리가 빈 노드를 누르면 한 걸음 펼칩니다
        </span>
      </div>
      <ForceGraph nodes={nodes} links={links} selected={selected} height={480}
        colorOf={(kind) => kindOf(kind)?.color ?? VIZ.series}
        onSelect={setSelected} onExpand={expand} />
      {state.note && <p className="mutedtext" style={{ fontSize: 12, margin: '4px 0' }}>{state.note}</p>}
      {current && <Detail info={current} />}
    </div>
  );
}

/** 고른 노드의 상세 — 그래프에 담을 수 없는 시그니처·설명·메서드를 여기에(파일이면 그 파일 전체, 선언이면 그것 하나). */
function Detail({ info }: { info: CodeInfo }) {
  const k = kindOf(info.kind);
  const node = info.node;
  return (
    <div style={{ marginTop: 6 }}>
      <h4 style={{ margin: '0 0 4px' }}>
        <span aria-hidden style={{ color: k?.color, marginRight: 6 }}>{k?.mark ?? '●'}</span>
        {info.label}
        <span className="mutedtext" style={{ fontSize: 12, fontWeight: 400, marginLeft: 6 }}>{k?.label ?? info.kind}</span>
      </h4>
      {info.path && <div className="mono mutedtext" style={{ fontSize: 11 }}>{info.path}{node ? `:${node.lineno}` : ''}</div>}
      {info.kind === 'dir' && <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>아래 파일 {fmt(info.weight)}개</p>}
      {info.kind === 'file' && info.file && (
        <>
          <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 4px' }}>
            클래스 {info.file.children.filter((c) => c.kind === 'class').length} ·
            함수 {info.file.children.filter((c) => c.kind !== 'class').length}
          </p>
          <CodeStructure files={[info.file]} open />
        </>
      )}
      {info.kind === 'external' && (
        <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
          이 리포 밖의 상위 클래스 — 이 리포에서 상속하는 클래스 {fmt(info.weight)}개
        </p>
      )}
      {node && info.file && <CodeStructure files={[{ ...info.file, children: [node] }]} open />}
    </div>
  );
}
