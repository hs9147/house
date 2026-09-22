import type { C4Element, C4Level, CodeMapFile, CodeMapNode } from './types';

// 리포의 코드 구조(정적 파싱)를 C4 code 레벨 그림으로 바꾼다.
//
// **원천이 다르다.** context·container·component는 산출물 문서(mermaid C4 블록)에서 오지만
// code 레벨은 리포에서 온다 — 사람이 문서에 클래스를 손으로 적어 두면 코드와 어긋나고,
// 그 어긋남은 아무도 고치지 않는다. 그래서 여기서는 문서를 읽지 않는다.
//
// 그림이지만 **같은 캔버스**를 쓴다: 파일을 경계(boundary)로, 클래스·함수를 노드로 접어
// C4Element 목록을 만들면 배치·렌더·미니맵이 그대로 재사용된다. 레벨마다 다른 그림 엔진을
// 두면 확대·축소 동작이 레벨별로 미묘하게 달라진다.
//
// **선은 상속만 그린다.** 정적 파싱이 확실히 아는 관계가 그것뿐이다(호출 그래프는 없다).
// 없는 관계를 그려 보여 주면 그림이 사실과 달라지고, 그 그림으로 설계를 판단하게 된다.

const MAX_METHODS = 6;

function fileAlias(path: string): string {
  return `f:${path}`;
}

function nodeAlias(path: string, node: CodeMapNode): string {
  return `n:${path}#${node.name}:${node.lineno}`;
}

/** 클래스 노드의 본문 — 메서드 이름을 몇 개만 싣는다(UML 클래스 박스처럼). */
function members(node: CodeMapNode): string {
  const names = node.children.map((c) => c.name);
  if (names.length === 0) return node.doc;
  const shown = names.slice(0, MAX_METHODS).join(', ');
  return names.length > MAX_METHODS ? `${shown} +${names.length - MAX_METHODS}` : shown;
}

function element(partial: Partial<C4Element> & { alias: string; label: string }): C4Element {
  return {
    kind: '', base: 'component', technology: '', description: '', external: false,
    shape: '', boundary_type: '', parent: null, link: '', tags: '', paths: [],
    ...partial,
  };
}

/**
 * 파일 목록 → code 레벨 그림. 파일이 경계, 최상위 클래스·함수가 노드, 상속이 선이다.
 *
 * 같은 그림에 없는 상위 클래스(예: 라이브러리의 BaseSettings)는 **외부 노드**로 한 번만
 * 만든다 — 선을 지워 버리면 "상속이 없는 클래스"로 보이고, 그건 사실이 아니다.
 */
export function codeLevel(files: CodeMapFile[], title: string): C4Level {
  const elements: C4Element[] = [];
  const relations: C4Level['relations'] = [];
  // 이름 → alias. 같은 이름의 클래스가 여러 파일에 있으면 먼저 나온 것을 쓴다 — 정적
  // 파싱은 어느 쪽을 상속했는지 모르고, 추측해 선을 옮기면 조용히 틀린 그림이 된다.
  const classAlias = new Map<string, string>();
  const externalAlias = new Map<string, string>();

  files.forEach((file) => {
    const group = fileAlias(file.path);
    elements.push(element({
      alias: group,
      label: file.path,
      base: 'boundary',
      // 뷰가 label 뒤에 kind를 붙인다 — 비워 두면 " · "만 덩그러니 남는다.
      kind: 'file',
      boundary_type: 'File',
      description: file.summary,
    }));
    file.children.forEach((node) => {
      const alias = nodeAlias(file.path, node);
      elements.push(element({
        alias,
        label: node.name,
        kind: node.kind,
        technology: node.kind === 'class' ? 'class' : 'function',
        description: members(node),
        parent: group,
      }));
      if (node.kind === 'class' && !classAlias.has(node.name)) classAlias.set(node.name, alias);
    });
  });

  files.forEach((file) => {
    file.children.forEach((node) => {
      if (node.kind !== 'class') return;
      const source = nodeAlias(file.path, node);
      (node.bases ?? []).forEach((base) => {
        let target = classAlias.get(base);
        if (!target) {
          target = externalAlias.get(base);
          if (!target) {
            target = `x:${base}`;
            externalAlias.set(base, target);
            elements.push(element({
              alias: target, label: base, kind: 'class', technology: 'class',
              external: true, description: '이 그림 밖에서 온 상위 클래스',
            }));
          }
        }
        relations.push({
          source, target, label: 'extends', technology: '', bidirectional: false,
        });
      });
    });
  });

  // 문서가 아니라 리포가 원천이므로 '확정' 개념이 없다 — 항상 지금의 코드다.
  return { stage: 'repo', confirmed: true, title, elements, relations };
}
