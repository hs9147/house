import { describe, expect, it } from 'vitest';
import { codeLevel } from '../lib/codelevel';
import type { CodeMapFile } from '../lib/types';

// 실제 파서가 내는 모양(app/services/codemap.py를 이 리포에 돌린 결과에서 옮겼다)으로
// 한 번 태운다 — 손으로 만든 최소 입력만 보면 "실제 데이터에서 박스가 몇 개가 되는가"를
// 놓친다. 그 수가 폭발하면 그림이 아니라 벽지가 된다.
const REAL: CodeMapFile[] = [
  {
    path: 'app/models.py',
    lang: 'python',
    summary: '',
    children: [
      { kind: 'class', name: 'ProjectType', signature: 'class ProjectType(str, Enum)',
        doc: '', lineno: 14, bases: ['str', 'Enum'], children: [] },
      { kind: 'class', name: 'BuildProfile', signature: 'class BuildProfile(str, Enum)',
        doc: '빌드 옵션.', lineno: 26, bases: ['str', 'Enum'], children: [] },
      { kind: 'class', name: 'Project', signature: 'class Project(Base)',
        doc: '', lineno: 53, bases: ['Base'], children: [] },
      { kind: 'function', name: 'utcnow', signature: 'def utcnow()',
        doc: '', lineno: 10, bases: undefined, children: [] },
    ],
  },
  {
    path: 'app/db.py',
    lang: 'python',
    summary: '',
    children: [
      { kind: 'class', name: 'Base', signature: 'class Base(DeclarativeBase)',
        doc: '', lineno: 7, bases: ['DeclarativeBase'], children: [] },
    ],
  },
];

describe('codeLevel — 실제 파서 출력', () => {
  it('파일 2개가 경계, 최상위 선언이 노드가 된다', () => {
    const level = codeLevel(REAL, 'backend');
    const boundaries = level.elements.filter((e) => e.base === 'boundary');
    expect(boundaries.map((e) => e.label)).toEqual(['app/models.py', 'app/db.py']);
    // 파일 안의 노드 4 + 1, 그림 밖 상위 클래스(str · Enum · DeclarativeBase) 3
    expect(level.elements.filter((e) => e.parent !== null)).toHaveLength(5);
    expect(level.elements.filter((e) => e.external).map((e) => e.label))
      .toEqual(['str', 'Enum', 'DeclarativeBase']);
  });

  it('같은 리포 안의 상속은 내부 노드를 가리킨다', () => {
    const level = codeLevel(REAL, 'backend');
    const byAlias = new Map(level.elements.map((e) => [e.alias, e]));
    const toBase = level.relations.filter((r) => byAlias.get(r.target)?.label === 'Base');
    expect(toBase).toHaveLength(1);
    expect(byAlias.get(toBase[0].source)?.label).toBe('Project');
    // Base는 db.py에 실재하므로 외부 노드로 만들지 않는다
    expect(byAlias.get(toBase[0].target)?.external).toBe(false);
  });

  it('선은 상속 수와 정확히 같다 — 없는 관계를 만들지 않는다', () => {
    const level = codeLevel(REAL, 'backend');
    const declared = REAL.flatMap((f) => f.children).flatMap((n) => n.bases ?? []);
    expect(level.relations).toHaveLength(declared.length);
  });
});
