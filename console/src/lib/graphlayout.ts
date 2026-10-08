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
 * 방향은 좌→우(LR)다. 노드 이름이 한국어 문장(절 제목·표 머리글)이라 가로로 길고,
 * 위→아래로 쌓으면 한 랭크가 화면 밖으로 나간다.
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
): Map<string, { x: number; y: number }> {
  const g = new graphlib.Graph();
  g.setGraph({ rankdir: 'LR', nodesep: 18, ranksep: 90, marginx: 12, marginy: 12 });
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
