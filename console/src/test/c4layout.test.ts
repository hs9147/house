import { describe, expect, it } from 'vitest';
import { GROUP_HEAD, GROUP_PAD, layoutElements } from '../lib/c4layout';
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
