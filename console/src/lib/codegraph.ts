import type { CodeMapFile, CodeMapNode } from './types';

// 리포의 코드 구조(정적 파싱)를 **지식 그래프**로 — 온톨로지 조회와 같은 둘러보기(components/ForceGraph).
//
// 처음에는 **다 펼쳐 보인다**(expandAll) — 리포 → 폴더 → 파일 → 클래스·함수, 클래스는 상위·하위 클래스로.
// 다만 힘기반 배치는 노드 수의 제곱으로 무거워지고 이 리포만 해도 선언이 2천 개를 넘는다(파일 225 · 클래스 181 ·
// 함수 1739) — 그래서 MAX_SHOWN까지만 폴더·파일부터 너비 우선으로 펼치고, 남은 것은 온톨로지처럼 노드를 눌러
// 한 걸음씩 펼친다. 이름으로 찾으면 찾은 것들이 검색어 노드를 둘러싼다.
//
// 선은 정적 파싱이 확실히 아는 것만 — 포함(폴더·파일), 정의(파일의 선언), 상속. 호출 관계는 없다
// (없는 관계를 그리면 그 그림으로 설계를 잘못 판단하게 된다).

export type CodeKind = 'root' | 'dir' | 'file' | 'class' | 'function' | 'external' | 'query';
export type CodeRel = 'contains' | 'defines' | 'inherits' | 'match';

export interface CodeInfo {
  id: string;
  label: string;
  kind: CodeKind;
  /** 크기 — 폴더는 아래 파일 수, 파일은 선언 수, 클래스는 메서드 수, 외부 클래스는 하위 클래스 수. */
  weight: number;
  path?: string;
  file?: CodeMapFile;
  node?: CodeMapNode;
}

export interface CodeEdge {
  source: string;
  target: string;
  rel: CodeRel;
}

export interface CodeIndex {
  info: Map<string, CodeInfo>;
  edges: Map<string, CodeEdge[]>;   // 노드마다 닿는 선(들어오는 것·나가는 것)
}

/** 한 번 펼칠 때 더하는 이웃 수 — 온톨로지 조회와 같은 상한. */
export const EXPAND_LIMIT = 30;
/** 처음 그림에 다 펼치는 노드 수의 상한 — 넘으면 남은 것은 눌러서 펼친다(components/ForceGraph는 n²). */
export const MAX_SHOWN = 500;
export const ROOT = 'root';

const dirId = (path: string) => `dir:${path}`;
const fileId = (path: string) => `file:${path}`;
const declId = (path: string, n: CodeMapNode) => `decl:${path}#${n.name}:${n.lineno}`;
const extId = (name: string) => `ext:${name}`;

export function codeIndex(files: CodeMapFile[]): CodeIndex {
  const info = new Map<string, CodeInfo>();
  const edges = new Map<string, CodeEdge[]>();
  const link = (source: string, target: string, rel: CodeRel) => {
    const e = { source, target, rel };
    [source, target].forEach((id) => edges.set(id, [...(edges.get(id) ?? []), e]));
  };
  info.set(ROOT, { id: ROOT, label: '리포', kind: 'root', weight: files.length });

  // 폴더 — 경로의 앞마디마다. 파일 수는 아래에 있는 모든 파일이다.
  const dirOf = (path: string): string => {
    const cut = path.lastIndexOf('/');
    if (cut < 0) return ROOT;
    const dir = path.slice(0, cut), id = dirId(dir);
    if (!info.has(id)) {
      info.set(id, { id, label: dir.slice(dir.lastIndexOf('/') + 1), kind: 'dir', weight: 0, path: dir });
      link(dirOf(dir), id, 'contains');
    }
    return id;
  };
  const classByName = new Map<string, string>();
  files.forEach((file) => {
    const id = fileId(file.path), parent = dirOf(file.path);
    info.set(id, { id, label: file.path.slice(file.path.lastIndexOf('/') + 1), kind: 'file',
                   weight: file.children.length, path: file.path, file });
    link(parent, id, 'contains');
    for (let p = parent; p !== ROOT; p = dirOf(info.get(p)!.path!)) info.get(p)!.weight += 1;
    file.children.forEach((n) => {
      const d = declId(file.path, n);
      info.set(d, { id: d, label: n.name, kind: n.kind === 'class' ? 'class' : 'function',
                    weight: n.children.length, path: file.path, file, node: n });
      link(id, d, 'defines');
      // 같은 이름의 클래스가 여럿이면 먼저 나온 것 — 정적 파싱은 어느 쪽을 상속했는지 모른다.
      if (n.kind === 'class' && !classByName.has(n.name)) classByName.set(n.name, d);
    });
  });
  // 상속 — 같은 리포에 없는 상위 클래스는 외부 노드로 한 번만(선을 지우면 상속이 없어 보인다).
  files.forEach((file) => file.children.forEach((n) => {
    if (n.kind !== 'class') return;
    (n.bases ?? []).forEach((base) => {
      let target = classByName.get(base);
      if (!target) {
        target = extId(base);
        if (!info.has(target)) info.set(target, { id: target, label: base, kind: 'external', weight: 0 });
        info.get(target)!.weight += 1;
      }
      link(declId(file.path, n), target, 'inherits');
    });
  }));
  return { info, edges };
}

const ORDER: Record<CodeKind, number> = { query: 0, root: 1, dir: 2, class: 3, external: 4, file: 5, function: 6 };

/**
 * 한 걸음 펼치기 — 그 노드에 닿는 선과 그 끝의 노드. 이미 캔버스에 있는 것은 빼고, 많으면 상한까지
 * (폴더·클래스가 먼저, 같은 종류는 큰 것부터). 남은 수는 more로 알린다.
 */
export function neighbors(idx: CodeIndex, id: string, shown: Set<string>): { edges: CodeEdge[]; more: number } {
  const all = unshown(idx, id, shown);
  return { edges: all.slice(0, EXPAND_LIMIT), more: Math.max(0, all.length - EXPAND_LIMIT) };
}

/** 그 노드에서 캔버스에 아직 없는 이웃으로 가는 선 — 폴더·클래스가 먼저, 같은 종류는 큰 것부터. */
function unshown(idx: CodeIndex, id: string, shown: Set<string>): CodeEdge[] {
  // 위로도 간다(담고 있는 폴더·정의한 파일) — 찾기로 바로 연 노드에서 제자리를 찾아 올라갈 수 있게.
  // 위에서 내려온 둘러보기라면 위는 이미 캔버스에 있어 빠진다.
  const all = (idx.edges.get(id) ?? []).filter((e) => !shown.has(e.source === id ? e.target : e.source));
  const other = (e: CodeEdge) => idx.info.get(e.source === id ? e.target : e.source)!;
  return all.sort((a, b) => ORDER[other(a).kind] - ORDER[other(b).kind] || other(b).weight - other(a).weight
    || other(a).label.localeCompare(other(b).label));
}

/** 처음 그림 — 리포에서 닿는 것을 다 펼친다. 많으면 limit까지(리포에서 가까운 것부터 너비 우선), more는 남은 수.
 *  선은 보이는 두 노드를 잇는 것을 다 넣는다(둘 다 이미 펼친 클래스 사이의 상속도). */
export function expandAll(idx: CodeIndex, limit = MAX_SHOWN):
  { ids: Set<string>; edges: CodeEdge[]; expanded: Set<string>; more: number } {
  const ids = new Set([ROOT]), expanded = new Set<string>(), queue = [ROOT];
  while (queue.length && ids.size < limit) {
    const id = queue.shift()!;
    const all = unshown(idx, id, ids);
    for (const e of all) {
      if (ids.size >= limit) break;
      const next = e.source === id ? e.target : e.source;
      ids.add(next);
      queue.push(next);
    }
    if (unshown(idx, id, ids).length === 0) expanded.add(id);
  }
  const edges = new Set<CodeEdge>();
  ids.forEach((id) => (idx.edges.get(id) ?? []).forEach((e) => {
    if (ids.has(e.source) && ids.has(e.target)) edges.add(e);
  }));
  return { ids, edges: [...edges], expanded, more: idx.info.size - ids.size };
}

/** 펼칠 것이 남았는가 — 노드 테두리를 비워 "더 있다"를 보인다. */
export function hasMore(idx: CodeIndex, id: string, shown: Set<string>): boolean {
  return neighbors(idx, id, shown).edges.length > 0;
}

/** 이름으로 찾기 — 대소문자 무시, 클래스·함수·파일·폴더 순, 큰 것부터. */
export function searchCode(idx: CodeIndex, query: string): CodeInfo[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  return [...idx.info.values()]
    .filter((n) => n.kind !== 'root' && n.kind !== 'external' && n.label.toLowerCase().includes(q))
    .sort((a, b) => ORDER[a.kind] - ORDER[b.kind] || b.weight - a.weight || a.label.localeCompare(b.label))
    .slice(0, EXPAND_LIMIT);
}
