import { describe, expect, it } from 'vitest';
import { GROUP_HEAD, GROUP_PAD, NODE_W, layoutFlow } from '../lib/c4layout';
import type { C4Element } from '../lib/types';

// code 레벨의 배치. dagre는 **선이 없는 노드를 모두 같은 랭크**에 둬서 박스가 좌우로
// 끝없이 늘어섰다(negowith 보고). 이 레벨은 서로 무관한 박스가 대부분이므로 접어서 쌓는다.

function element(alias: string, parent: string | null = null,
                 base: C4Element['base'] = 'component'): C4Element {
  return {
    kind: base, base, alias, label: alias, technology: '', description: '',
    external: false, shape: 'box', boundary_type: '', parent, link: '', tags: '', paths: [],
  };
}

describe('layoutFlow', () => {
  it('목표 폭을 넘으면 다음 줄로 접는다 — 한 줄로 늘어서지 않는다', () => {
    const boxes = layoutFlow(Array.from({ length: 10 }, (_, i) => element(`n${i}`)));
    const rows = new Set(boxes.map((b) => b.y));
    expect(rows.size).toBeGreaterThan(1);
    // 한 줄에 네 개까지(NODE_W * 4 + 여백)
    const firstRow = boxes.filter((b) => b.y === 0);
    expect(firstRow).toHaveLength(4);
  });

  it('줄 안에서는 왼쪽부터 차례로, 겹치지 않는다', () => {
    const boxes = layoutFlow(Array.from({ length: 4 }, (_, i) => element(`n${i}`)));
    const xs = boxes.map((b) => b.x);
    expect(xs).toEqual([...xs].sort((a, b) => a - b));
    for (let i = 1; i < boxes.length; i += 1) {
      expect(boxes[i].x).toBeGreaterThanOrEqual(boxes[i - 1].x + boxes[i - 1].w);
    }
  });

  it('경계는 자식 격자를 감싸고, 자식 좌표는 경계 기준이다', () => {
    const boxes = new Map(layoutFlow([
      element('file', null, 'boundary'),
      element('a', 'file'),
      element('b', 'file'),
    ]).map((b) => [b.alias, b]));

    const group = boxes.get('file')!;
    const a = boxes.get('a')!;
    // 자식은 경계 안쪽 여백만큼 밀려 있다(React Flow의 자식 좌표 규약)
    expect(a.x).toBe(GROUP_PAD);
    expect(a.y).toBe(GROUP_HEAD);
    // 경계는 자식 둘을 담을 만큼 넓다
    expect(group.w).toBeGreaterThanOrEqual(NODE_W * 2);
    expect(group.h).toBeGreaterThan(a.h);
  });

  it('가장 넓은 박스가 줄을 삐져나가지 않는다', () => {
    // 경계는 자식 수에 따라 목표 폭보다 넓어질 수 있다 — 그때는 줄을 넓힌다.
    const wide = [element('big', null, 'boundary'),
      ...Array.from({ length: 12 }, (_, i) => element(`c${i}`, 'big'))];
    const boxes = layoutFlow([...wide, element('small')]);
    const big = boxes.find((b) => b.alias === 'big')!;
    const small = boxes.find((b) => b.alias === 'small')!;
    // 넓은 경계가 첫 줄을 다 쓰므로 small은 다음 줄로 간다
    expect(small.y).toBeGreaterThan(big.y);
    expect(small.x).toBe(0);
  });

  it('선언되지 않은 경계를 가리키는 parent는 최상위로 떨어진다', () => {
    // 문서가 아니라 파서가 만든 값이지만, 규약은 layoutElements와 같아야 한다.
    const boxes = layoutFlow([element('orphan', 'nope')]);
    expect(boxes).toHaveLength(1);
    expect(boxes[0].parent).toBeNull();
    expect(boxes[0].x).toBe(0);
  });
});

describe('layoutFlow — 큰 그림', () => {
  it('박스가 많아도 폭은 목표 폭에 묶이고 아래로 쌓인다', () => {
    // 이 리포의 code 레벨은 접은 뒤에도 406개다(파일 225 + 클래스 181). 예전 배치는
    // 선이 없는 박스를 한 랭크에 몰아 폭이 박스 수만큼 늘어났다 — 그것이 "좌우로 길게
    // 나열"의 원인이었다.
    const boxes = layoutFlow(Array.from({ length: 400 }, (_, i) => element(`n${i}`)));
    const width = Math.max(...boxes.map((b) => b.x + b.w));
    const height = Math.max(...boxes.map((b) => b.y + b.h));

    expect(width).toBeLessThanOrEqual(NODE_W * 4 + 24 * 3);  // 한 줄에 네 개
    expect(height).toBeGreaterThan(width);                    // 세로로 길다 = 접혔다
    expect(new Set(boxes.map((b) => b.y)).size).toBe(100);    // 400 / 4
  });
});
