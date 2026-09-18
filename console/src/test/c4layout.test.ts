import { describe, expect, it } from 'vitest';
import { GROUP_HEAD, GROUP_PAD, focusElements, layoutElements } from '../lib/c4layout';
import type { C4Element, C4Relation } from '../lib/types';

function element(alias: string, base: C4Element['base'], parent: string | null = null): C4Element {
  return {
    kind: base, base, alias, label: alias, technology: '', description: '',
    external: false, shape: 'box', boundary_type: '', parent, link: '', tags: '', paths: [],
  };
}

const relation = (source: string, target: string): C4Relation => ({
  source, target, label: '', technology: '', bidirectional: false,
});

describe('layoutElements', () => {
  it('경계 안의 요소는 경계 안쪽 좌표로 배치되고 경계가 그것을 감싼다', () => {
    const elements = [
      element('paas', 'boundary'),
      element('api', 'container', 'paas'),
      element('db', 'container', 'paas'),
    ];
    const boxes = new Map(
      layoutElements(elements, [relation('api', 'db')]).map((b) => [b.alias, b]),
    );

    const group = boxes.get('paas')!;
    const api = boxes.get('api')!;
    const db = boxes.get('db')!;
    // 자식 좌표는 경계 노드 기준의 상대 좌표다(React Flow 규약) — 이름 줄·여백 안쪽에 있다.
    expect(api.parent).toBe('paas');
    expect(api.x).toBeGreaterThanOrEqual(GROUP_PAD);
    expect(api.y).toBeGreaterThanOrEqual(GROUP_HEAD);
    // 경계는 자식 전부를 담을 만큼 크다 — 이 계산이 틀리면 자식이 경계 밖으로 삐져나온다.
    expect(group.w).toBeGreaterThanOrEqual(api.x + api.w + GROUP_PAD);
    expect(group.h).toBeGreaterThanOrEqual(db.y + db.h);
    // 관계가 있는 두 컨테이너는 위아래로 갈라진다(rankdir=TB)
    expect(db.y).toBeGreaterThan(api.y);
  });

  it('선언되지 않은 경계를 가리키는 parent는 최상위로 떨어진다', () => {
    const boxes = layoutElements([element('api', 'container', 'nope')], []);
    expect(boxes[0].parent).toBeNull();
  });

  it('요소가 없으면 배치 결과도 없다', () => {
    expect(layoutElements([], [])).toEqual([]);
  });
});

describe('focusElements', () => {
  const elements = [
    element('paas', 'boundary'),
    element('api', 'boundary', 'paas'),
    element('planner', 'component', 'api'),
    element('gitea', 'system'),
  ];

  it('경계로 좁히면 그 경계는 빠지고 직속 자식이 최상위가 된다', () => {
    const focused = focusElements(elements, 'api');
    expect(focused.map((e) => e.alias)).toEqual(['planner']);
    expect(focused[0].parent).toBeNull();
  });

  it('바깥 경계로 좁히면 그 아래 계층은 그대로 남는다', () => {
    const focused = focusElements(elements, 'paas');
    expect(focused.map((e) => e.alias)).toEqual(['api', 'planner']);
    expect(focused.find((e) => e.alias === 'api')!.parent).toBeNull();
    expect(focused.find((e) => e.alias === 'planner')!.parent).toBe('api'); // 중첩 유지
  });

  it('없는 경계로 좁히면 빈 목록 — 호출부가 전체 그림으로 되돌린다', () => {
    expect(focusElements(elements, 'nope')).toEqual([]);
  });
});
