import { describe, expect, it } from 'vitest';
import { NODE_H, NODE_W, layoutWrapped } from '../lib/graphlayout';

/**
 * 줄바꿈 배치 — 가로를 다 쓰고 아래로 접힌다. 좌표 규칙을 테스트로 잠근다(그림은 눈으로
 * 봐야 하지만, "폭을 넘으면 접힌다"와 "같은 단계는 한 칸에 쌓인다"는 계산이다).
 */
const chain = (n: number) => ({
  nodes: Array.from({ length: n }, (_, i) => ({ id: `n${i}` })),
  edges: Array.from({ length: n - 1 }, (_, i) => ({ source: `n${i}`, target: `n${i + 1}` })),
});

describe('layoutWrapped', () => {
  it('좁은 폭에서는 한 칸씩 — 배치가 나오지 않는 일은 없다', () => {
    const { positions, width } = layoutWrapped(chain(3).nodes, chain(3).edges, 10);
    expect(positions.get('n0')!.x).toBe(0);
    expect(positions.get('n1')!.x).toBe(0);      // 다음 줄로 접힌다
    expect(positions.get('n1')!.y).toBeGreaterThan(positions.get('n0')!.y);
    expect(width).toBe(NODE_W);
  });

  it('폭이 넉넉하면 한 줄에 여러 칸을 놓고, 넘으면 접는다', () => {
    const { nodes, edges } = chain(5);
    // 세 칸이 들어가는 폭
    const { positions, height } = layoutWrapped(nodes, edges, (NODE_W + 56) * 3 - 56);
    expect(positions.get('n0')!.y).toBe(positions.get('n2')!.y);   // 같은 줄
    expect(positions.get('n3')!.y).toBeGreaterThan(positions.get('n0')!.y);  // 접혔다
    expect(positions.get('n3')!.x).toBe(0);                        // 줄의 처음으로
    expect(height).toBeGreaterThan(NODE_H);
  });

  it('같은 단계(분기의 양쪽)는 한 칸 안에 위아래로 쌓인다', () => {
    const { positions } = layoutWrapped(
      [{ id: '판정' }, { id: '승인' }, { id: '자동' }],
      [{ source: '판정', target: '승인' }, { source: '판정', target: '자동' }],
      2000,
    );
    expect(positions.get('승인')!.x).toBe(positions.get('자동')!.x);
    expect(positions.get('승인')!.y).not.toBe(positions.get('자동')!.y);
    expect(positions.get('판정')!.x).toBeLessThan(positions.get('승인')!.x);
  });

  it('고리가 있어도 좌표를 낸다 — 화면이 터지면 고치러 들어갈 수도 없다', () => {
    const { positions } = layoutWrapped(
      [{ id: 'a' }, { id: 'b' }],
      [{ source: 'a', target: 'b' }, { source: 'b', target: 'a' }],
      2000,
    );
    expect(positions.size).toBe(2);
  });
});
