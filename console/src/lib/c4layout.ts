import { graphlib, layout as dagreLayout } from '@dagrejs/dagre';
import type { C4Element, C4Relation } from './types';

// C4 다이어그램 배치 — 경계(boundary) 안쪽을 먼저 배치해 크기를 구하고, 그 크기로 바깥
// 층을 다시 배치한다(dagre는 중첩 그래프를 한 번에 풀지 않는다). 렌더는 React Flow가
// 하고 여기서는 좌표만 만든다 — 그래야 배치 규칙을 테스트로 잠글 수 있다.

export const NODE_W = 190;
const NODE_BASE_H = 46;
const TECH_H = 16;
const DESC_H = 26;
// 경계 안쪽 여백. 위쪽은 경계 이름 줄이 들어가 더 넓다.
export const GROUP_PAD = 16;
export const GROUP_HEAD = 30;

export function nodeSize(element: C4Element): { w: number; h: number } {
  return {
    w: NODE_W,
    h: NODE_BASE_H + (element.technology ? TECH_H : 0) + (element.description ? DESC_H : 0),
  };
}

export interface C4Box {
  alias: string;
  parent: string | null; // 감싼 경계 — x·y는 이 경계 노드 기준의 상대 좌표다
  x: number;
  y: number;
  w: number;
  h: number;
}

export function layoutElements(elements: C4Element[], relations: C4Relation[]): C4Box[] {
  const byAlias = new Map(elements.map((e) => [e.alias, e]));
  // 선언되지 않은 경계를 가리키는 parent는 최상위로 떨어뜨린다(문서 오타 방어).
  const parentOf = (alias: string): string | null => {
    const parent = byAlias.get(alias)?.parent;
    return parent && byAlias.has(parent) ? parent : null;
  };
  const children = new Map<string | null, C4Element[]>();
  elements.forEach((e) => {
    const key = parentOf(e.alias);
    children.set(key, [...(children.get(key) ?? []), e]);
  });

  const boxes = new Map<string, C4Box>();

  // 경계를 넘는 관계를 배치에 반영하려면 양 끝을 같은 층의 노드로 접어야 한다 —
  // alias의 조상 중 이 그룹의 직속 자식인 것을 찾는다.
  const foldInto = (alias: string, group: string | null): string | null => {
    let current: string | null = byAlias.has(alias) ? alias : null;
    while (current) {
      const parent = parentOf(current);
      if (parent === group) return current;
      current = parent;
    }
    return null;
  };

  const place = (group: string | null): { w: number; h: number } => {
    const kids = children.get(group) ?? [];
    if (kids.length === 0) return { w: NODE_W, h: NODE_BASE_H };
    const g = new graphlib.Graph();
    g.setGraph({ rankdir: 'TB', nodesep: 36, ranksep: 56, marginx: 0, marginy: 0 });
    g.setDefaultEdgeLabel(() => ({}));

    const sizes = new Map<string, { w: number; h: number }>();
    kids.forEach((kid) => {
      const inner = kid.base === 'boundary' ? place(kid.alias) : nodeSize(kid);
      const size = kid.base === 'boundary'
        ? { w: inner.w + GROUP_PAD * 2, h: inner.h + GROUP_HEAD + GROUP_PAD }
        : inner;
      sizes.set(kid.alias, size);
      g.setNode(kid.alias, { width: size.w, height: size.h });
    });
    relations.forEach((r) => {
      const from = foldInto(r.source, group);
      const to = foldInto(r.target, group);
      if (from && to && from !== to) g.setEdge(from, to);
    });
    dagreLayout(g);

    let width = 0;
    let height = 0;
    kids.forEach((kid) => {
      const node = g.node(kid.alias);
      const size = sizes.get(kid.alias) ?? nodeSize(kid);
      boxes.set(kid.alias, {
        alias: kid.alias,
        parent: group,
        x: node.x - size.w / 2,
        y: node.y - size.h / 2,
        w: size.w,
        h: size.h,
      });
      width = Math.max(width, node.x + size.w / 2);
      height = Math.max(height, node.y + size.h / 2);
    });
    return { w: width, h: height };
  };

  place(null);
  // 경계 안의 좌표는 경계 '내용' 기준으로 나왔다 — 이름 줄과 여백만큼 밀어 경계 노드
  // 기준으로 바꾼다(React Flow의 자식 노드 좌표 규약).
  return elements
    .map((e) => boxes.get(e.alias))
    .filter((box): box is C4Box => box !== undefined)
    .map((box) => (box.parent === null
      ? box
      : { ...box, x: box.x + GROUP_PAD, y: box.y + GROUP_HEAD }));
}
