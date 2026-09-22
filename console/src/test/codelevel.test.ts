import { describe, expect, it } from 'vitest';
import { codeLevel } from '../lib/codelevel';
import type { CodeMapFile, CodeMapNode } from '../lib/types';

// code 레벨은 문서가 아니라 **리포**가 원천이다(services/codemap의 정적 파싱). 그림이
// 말하는 것과 코드가 어긋나면 안 되므로, 파싱 결과를 그대로 접는 것 외의 판단을 두지 않는다.

function node(
  kind: CodeMapNode['kind'], name: string,
  extra: Partial<CodeMapNode> = {},
): CodeMapNode {
  return {
    kind, name, signature: name, doc: '', lineno: 1, children: [], ...extra,
  };
}

const file = (path: string, children: CodeMapNode[], summary = ''): CodeMapFile => ({
  path, lang: 'python', summary, children,
});

describe('codeLevel', () => {
  it('파일이 경계가 되고 최상위 클래스·함수가 그 안의 노드가 된다', () => {
    const level = codeLevel([
      file('app/pay.py', [node('class', 'Pay'), node('function', 'run')], '결제'),
    ], 'backend');

    const boundary = level.elements.find((e) => e.base === 'boundary');
    expect(boundary?.label).toBe('app/pay.py');
    expect(boundary?.description).toBe('결제');
    const inside = level.elements.filter((e) => e.parent === boundary?.alias);
    expect(inside.map((e) => e.label)).toEqual(['Pay', 'run']);
    expect(inside.map((e) => e.technology)).toEqual(['class', 'function']);
  });

  it('상속을 선으로 그린다 — 같은 그림 안의 클래스끼리', () => {
    const level = codeLevel([
      file('a.py', [node('class', 'Base')]),
      file('b.py', [node('class', 'Widget', { bases: ['Base'] })]),
    ], 'ui');

    expect(level.relations).toHaveLength(1);
    const [rel] = level.relations;
    const byAlias = new Map(level.elements.map((e) => [e.alias, e]));
    expect(byAlias.get(rel.source)?.label).toBe('Widget');
    expect(byAlias.get(rel.target)?.label).toBe('Base');
    expect(rel.label).toBe('extends');
  });

  it('그림 밖의 상위 클래스는 외부 노드로 한 번만 만든다', () => {
    // 선을 지우면 "상속이 없는 클래스"로 보인다 — 그건 사실이 아니다. 그리고 같은 상위
    // 클래스를 여러 클래스가 상속하면 노드는 하나여야 한다(그게 공통 조상이라는 사실이다).
    const level = codeLevel([
      file('m.py', [
        node('class', 'A', { bases: ['BaseSettings'] }),
        node('class', 'B', { bases: ['BaseSettings'] }),
      ]),
    ], 'cfg');

    const external = level.elements.filter((e) => e.external);
    expect(external).toHaveLength(1);
    expect(external[0].label).toBe('BaseSettings');
    expect(level.relations).toHaveLength(2);
    expect(level.relations.every((r) => r.target === external[0].alias)).toBe(true);
  });

  it('클래스 본문에 메서드 이름을 싣고, 많으면 개수로 줄인다', () => {
    const methods = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'].map((n) => node('method', n));
    const level = codeLevel([file('x.py', [node('class', 'Big', { children: methods })])], 'x');
    const box = level.elements.find((e) => e.label === 'Big');
    expect(box?.description).toBe('a, b, c, d, e, f +2');
  });

  it('메서드가 없으면 독스트링을 본문으로 쓴다', () => {
    const level = codeLevel([
      file('x.py', [node('class', 'Empty', { doc: '빈 클래스' })]),
    ], 'x');
    expect(level.elements.find((e) => e.label === 'Empty')?.description).toBe('빈 클래스');
  });

  it('같은 이름의 노드가 여러 파일에 있어도 alias가 겹치지 않는다', () => {
    // 겹치면 React Flow가 노드를 하나로 합쳐 버려 한쪽이 화면에서 사라진다.
    const level = codeLevel([
      file('a.py', [node('function', 'main')]),
      file('b.py', [node('function', 'main')]),
    ], 'dup');
    const aliases = level.elements.map((e) => e.alias);
    expect(new Set(aliases).size).toBe(aliases.length);
  });

  it('bases가 없는 파서 결과(구 버전)에서도 깨지지 않는다', () => {
    const level = codeLevel([file('a.py', [node('class', 'Old')])], 'old');
    expect(level.relations).toEqual([]);
  });

  it('리포가 원천이라 확정 개념이 없다 — 항상 지금의 코드다', () => {
    const level = codeLevel([file('a.py', [])], 'c');
    expect(level.stage).toBe('repo');
    expect(level.confirmed).toBe(true);
    expect(level.title).toBe('c');
  });
});
