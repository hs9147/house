import { graphlib, layout as dagreLayout } from '@dagrejs/dagre';

/**
 * 탐색 그래프 배치 — 좌표만 만든다(렌더는 React Flow가 한다). 규칙을 테스트로 잠그려고
 * 분리했다(c4layout과 같은 이유).
 *
 * **왜 힘기반(force) 시뮬레이션이 아닌가.** 우리 그래프는 문서→절→절→표·용어로 내려가는
 * 거의 트리다(관계 26,450건 중 contains가 대부분). 트리에 물리 시뮬레이션을 돌리면 같은
 * 질문을 다시 물을 때마다 모양이 달라져 비교할 수 없고, 계층이 보여야 하는 자리에서
 * 계층이 사라진다. 계층 배치(dagre)는 같은 입력에 같은 그림을 주고, "무엇이 무엇을
 * 포함하나"를 위치로 말한다. 노드를 끌어 옮기는 건 React Flow가 그대로 해 준다 —
 * 움직임이 필요한 건 배치 알고리즘이 아니라 사람의 손이다.
 *
 * 방향은 쓰는 쪽이 고른다. 온톨로지 탐색은 좌→우(LR)다 — 이름이 한국어 문장(절 제목·표
 * 머리글)이라 가로로 길고, 한 걸음 펼치면 이웃이 옆으로 퍼지는 편이 읽힌다. 워크플로는
 * 위→아래(TB)다: 흐름은 "다음 단계"가 아래에 있는 것이 자연스럽고, 29단계가 가로로 늘어나면
 * 화면에 맞추려고 축소되어 글자를 읽을 수 없다. 세로로 길어지는 것은 스크롤이 해결한다.
 */
export const NODE_W = 210;
// 박스 안은 세 줄이다(종류 11px · 이름 12px · 설명 11px). 54px으로 두면 한국어 줄높이에서
// 마지막 줄 아래가 잘렸다 — 글자가 잘리는 그림은 틀린 그림이다.
export const NODE_H = 72;

export interface LaidOutNode {
  id: string;
  x: number;
  y: number;
}

export function layoutGraph(
  nodes: { id: string }[],
  edges: { source: string; target: string }[],
  direction: 'LR' | 'TB' = 'LR',
): Map<string, { x: number; y: number }> {
  const g = new graphlib.Graph();
  g.setGraph(direction === 'TB'
    // 세로 흐름: 같은 랭크(분기의 양쪽)는 옆으로 벌리고, 단계 사이는 선 라벨이 들어갈 만큼.
    ? { rankdir: 'TB', nodesep: 40, ranksep: 56, marginx: 12, marginy: 12 }
    : { rankdir: 'LR', nodesep: 18, ranksep: 90, marginx: 12, marginy: 12 });
  g.setDefaultEdgeLabel(() => ({}));
  const ids = new Set(nodes.map((n) => n.id));
  nodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }));
  edges.forEach((e) => {
    // 양 끝이 다 캔버스에 있는 선만 배치에 반영한다 — 펼치지 않은 쪽을 가리키는 선은
    // 그리지도 않으므로 배치에 넣으면 보이지 않는 노드가 간격을 벌린다.
    if (ids.has(e.source) && ids.has(e.target) && e.source !== e.target) {
      g.setEdge(e.source, e.target);
    }
  });
  dagreLayout(g);
  const out = new Map<string, { x: number; y: number }>();
  nodes.forEach((n) => {
    const node = g.node(n.id);
    // dagre는 중심 좌표를, React Flow는 왼쪽 위 좌표를 쓴다.
    out.set(n.id, { x: node.x - NODE_W / 2, y: node.y - NODE_H / 2 });
  });
  return out;
}

/** 줄바꿈 배치의 간격. 칸 사이는 선 라벨(참/거짓)이 들어갈 만큼 둔다. */
const GAP_X = 56;
const GAP_Y = 24;

/**
 * 줄바꿈(뱀) 배치 — 흐름을 **글처럼** 왼쪽에서 오른쪽으로 놓고, 폭을 넘으면 다음 줄로 접는다.
 *
 * 세로 한 줄(TB)로 두면 29단계가 210px 폭의 리본이 되어 패널의 1,200px 중 6분의 1만 쓴다.
 * 좌→우 한 줄(LR)로 두면 폭이 6,000px이 되어 가로 스크롤만 남는다. 접으면 둘 다 아니다 —
 * 가로를 다 쓰고, 길어지는 쪽은 아래이고, 아래는 스크롤이 해결한다. 같은 처방을 code 레벨
 * 다이어그램에서 썼다(c4layout.layoutFlow).
 *
 * 단계(rank)는 **가장 긴 경로 깊이**로 매긴다 — 앞 단계가 모두 끝난 자리에 놓이게 된다.
 * 같은 단계의 노드(분기의 양쪽)는 한 칸 안에 위아래로 쌓는다.
 */
export function layoutWrapped(
  nodes: { id: string }[],
  edges: { source: string; target: string }[],
  availableWidth: number,
): { positions: Map<string, { x: number; y: number }>; width: number; height: number } {
  const ids = new Set(nodes.map((n) => n.id));
  const live = edges.filter((e) => ids.has(e.source) && ids.has(e.target)
    && e.source !== e.target);
  const incoming = new Map<string, string[]>();
  nodes.forEach((n) => incoming.set(n.id, []));
  live.forEach((e) => incoming.get(e.target)!.push(e.source));

  // 가장 긴 경로 깊이. 고리가 있으면(검증이 막지만 화면이 터지지 않게) 방문 표시로 끊는다.
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const rankOf = (id: string): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const parents = incoming.get(id) ?? [];
    const value = parents.length === 0
      ? 0
      : Math.max(...parents.map((p) => rankOf(p) + 1));
    visiting.delete(id);
    depth.set(id, value);
    return value;
  };
  nodes.forEach((n) => rankOf(n.id));

  const ranks = new Map<number, string[]>();
  nodes.forEach((n) => {
    const r = depth.get(n.id) ?? 0;
    ranks.set(r, [...(ranks.get(r) ?? []), n.id]);
  });

  const positions = new Map<string, { x: number; y: number }>();
  const columnW = NODE_W + GAP_X;
  // 한 줄에 적어도 한 칸은 들어가야 한다(패널이 아무리 좁아도 배치는 나와야 한다).
  const perRow = Math.max(1, Math.floor((availableWidth + GAP_X) / columnW));
  let width = 0;
  let y = 0;
  let rowHeight = 0;
  [...ranks.keys()].sort((a, b) => a - b).forEach((rank, index) => {
    const column = index % perRow;
    if (column === 0 && index > 0) {
      y += rowHeight + GAP_Y * 2;
      rowHeight = 0;
    }
    const members = ranks.get(rank)!;
    members.forEach((id, slot) => {
      positions.set(id, { x: column * columnW, y: y + slot * (NODE_H + GAP_Y) });
    });
    rowHeight = Math.max(rowHeight, members.length * (NODE_H + GAP_Y) - GAP_Y);
    width = Math.max(width, column * columnW + NODE_W);
  });
  return { positions, width, height: y + rowHeight };
}
