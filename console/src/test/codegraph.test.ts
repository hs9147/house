import { describe, expect, it } from 'vitest';
import { codeIndex, EXPAND_LIMIT, expandAll, neighbors, ROOT, searchCode } from '../lib/codegraph';
import type { CodeMapFile, CodeMapNode } from '../lib/types';

function node(kind: CodeMapNode['kind'], name: string, extra: Partial<CodeMapNode> = {}): CodeMapNode {
  return { kind, name, signature: name, doc: '', lineno: 1, children: [], ...extra };
}
const file = (path: string, children: CodeMapNode[]): CodeMapFile => ({ path, lang: 'python', summary: '', children });

const FILES = [
  file('app/api/pay.py', [node('class', 'PayApi', { bases: ['BaseApi'], children: [node('method', 'get')] }),
                          node('function', 'route')]),
  file('app/api/base.py', [node('class', 'BaseApi', { bases: ['BaseModel'] })]),
  file('main.py', [node('function', 'main')]),
];

describe('codeIndex', () => {
  it('리포 → 폴더 → 파일 → 선언으로 잇고, 폴더 크기는 아래 파일 수다', () => {
    const idx = codeIndex(FILES);
    expect(idx.info.get('dir:app')?.weight).toBe(2);
    expect(idx.info.get('dir:app/api')?.label).toBe('api');
    expect(idx.info.get('file:app/api/pay.py')?.weight).toBe(2);
    const root = neighbors(idx, ROOT, new Set([ROOT])).edges.map((e) => e.target);
    expect(root).toEqual(['dir:app', 'file:main.py']);        // 폴더가 먼저
  });

  it('상속은 같은 리포의 클래스로, 없으면 외부 노드로 한 번만', () => {
    const idx = codeIndex(FILES);
    const pay = 'decl:app/api/pay.py#PayApi:1';
    const inherits = (idx.edges.get(pay) ?? []).filter((e) => e.rel === 'inherits');
    expect(inherits.map((e) => e.target)).toEqual(['decl:app/api/base.py#BaseApi:1']);
    expect(idx.info.get('ext:BaseModel')?.kind).toBe('external');
    // 상위 클래스를 펼치면 하위 클래스와 정의한 파일이 이웃이다
    const base = neighbors(idx, 'decl:app/api/base.py#BaseApi:1', new Set()).edges;
    expect(base.map((e) => e.rel).sort()).toEqual(['defines', 'inherits', 'inherits']);
  });

  it('이미 캔버스에 있는 것은 빼고, 많으면 상한까지만 펼친다', () => {
    const many = [file('big.py', Array.from({ length: 45 }, (_, i) => node('function', `f${i}`, { lineno: i })))];
    const idx = codeIndex(many);
    const first = neighbors(idx, 'file:big.py', new Set([ROOT, 'file:big.py']));
    expect(first.edges).toHaveLength(EXPAND_LIMIT);
    expect(first.more).toBe(45 - EXPAND_LIMIT);
    const shown = new Set([ROOT, 'file:big.py', ...first.edges.map((e) => e.target)]);
    expect(neighbors(idx, 'file:big.py', shown).edges).toHaveLength(15);
  });

  it('이름으로 찾으면 클래스·함수가 먼저다', () => {
    const hits = searchCode(codeIndex(FILES), 'pay');
    expect(hits.map((h) => h.kind)).toEqual(['class', 'file']);
    expect(searchCode(codeIndex(FILES), '  ')).toEqual([]);
  });

  it('처음 그림은 다 펼친다 — 모든 노드와 그 사이의 선(펼친 두 클래스 사이의 상속까지)', () => {
    const idx = codeIndex(FILES);
    const all = expandAll(idx);
    expect([...all.ids].sort()).toEqual([...idx.info.keys()].sort());
    expect(all.more).toBe(0);
    const edges = new Set([...idx.edges.values()].flat());
    expect(all.edges).toHaveLength(edges.size);
    expect(all.edges.filter((e) => e.rel === 'inherits')).toHaveLength(2);
    expect([...all.expanded]).toContain(ROOT);
  });

  it('많으면 리포에서 가까운 것부터 상한까지만 — 남은 수를 알린다', () => {
    const many = [file('big.py', Array.from({ length: 45 }, (_, i) => node('function', `f${i}`, { lineno: i })))];
    const idx = codeIndex(many);
    const some = expandAll(idx, 10);
    expect(some.ids.size).toBe(10);
    expect(some.ids.has('file:big.py')).toBe(true);           // 파일이 선언보다 먼저
    expect(some.more).toBe(idx.info.size - 10);
    expect(some.expanded.has('file:big.py')).toBe(false);      // 다 펼치지 못한 노드는 눌러서 이어 펼친다
  });
});
