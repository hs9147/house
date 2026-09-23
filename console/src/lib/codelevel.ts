import type { C4Element, C4Level, CodeMapFile, CodeMapNode } from './types';

// 리포의 코드 구조(정적 파싱)를 C4 code 레벨 그림으로 바꾼다.
//
// **원천이 다르다.** context·container·component는 산출물 문서(mermaid C4 블록)에서 오지만
// code 레벨은 리포에서 온다 — 사람이 문서에 클래스를 손으로 적어 두면 코드와 어긋나고,
// 그 어긋남은 아무도 고치지 않는다. 그래서 여기서는 문서를 읽지 않는다.
//
// **추상화 레벨: 클래스가 박스, 함수는 파일의 목록이다.** 처음에는 최상위 함수도 박스로
// 그렸는데 이 리포에서 박스가 2145개가 됐다(파일 225 · 클래스 181 · 함수 1739). 한 파일에
// 65개가 한 줄로 늘어서 읽을 수 없었다. C4의 code 레벨은 본래 클래스 다이어그램이고, 자유
// 함수는 클래스가 아니라 **파일의 멤버**다 — 메서드를 클래스 박스 안에 적는 것과 같은
// 규칙으로 파일 박스 안에 적는다. 박스가 406개로 줄고 정보는 남는다.
//
// 그래서 파일은 두 모양으로 나온다: 클래스가 있으면 경계(그 안에 클래스 박스), 없으면
// 그냥 박스 하나. 빈 경계를 그리면 이름만 있는 큰 사각형이 176개 생긴다.
//
// **선은 상속만 그린다.** 정적 파싱이 확실히 아는 관계가 그것뿐이다(호출 그래프는 없다).
// 없는 관계를 그려 보여 주면 그림이 사실과 달라지고, 그 그림으로 설계를 판단하게 된다.

const MAX_MEMBERS = 6;

function fileAlias(path: string): string {
  return `f:${path}`;
}

function nodeAlias(path: string, node: CodeMapNode): string {
  return `n:${path}#${node.name}:${node.lineno}`;
}

/** 이름 몇 개만 싣고 나머지는 개수로 — 박스에 들어갈 만큼만 보여 준다. */
function nameList(names: string[]): string {
  if (names.length === 0) return '';
  const shown = names.slice(0, MAX_MEMBERS).join(', ');
  return names.length > MAX_MEMBERS ? `${shown} +${names.length - MAX_MEMBERS}` : shown;
}

function element(partial: Partial<C4Element> & { alias: string; label: string }): C4Element {
  return {
    kind: '', base: 'component', technology: '', description: '', external: false,
    shape: '', boundary_type: '', parent: null, link: '', tags: '', paths: [],
    ...partial,
  };
}

/**
 * 파일 목록 → code 레벨 그림.
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
    const classes = file.children.filter((n) => n.kind === 'class');
    const functions = file.children.filter((n) => n.kind !== 'class');
    const members = nameList(functions.map((n) => n.name));
    const group = fileAlias(file.path);

    elements.push(element({
      alias: group,
      label: file.path,
      // 클래스가 없으면 경계로 만들지 않는다 — 이름만 있는 빈 사각형이 된다.
      base: classes.length > 0 ? 'boundary' : 'component',
      // 뷰가 label 뒤에 kind를 붙인다 — 비워 두면 " · "만 덩그러니 남는다.
      kind: 'file',
      boundary_type: 'File',
      technology: classes.length > 0 ? '' : `함수 ${functions.length}`,
      // 파일 요약보다 멤버 목록이 그림에서 쓸모 있다. 둘 다 없으면 요약으로 떨어진다.
      description: members || file.summary,
    }));

    classes.forEach((node) => {
      const alias = nodeAlias(file.path, node);
      elements.push(element({
        alias,
        label: node.name,
        kind: node.kind,
        technology: 'class',
        // 메서드 이름 — UML 클래스 박스와 같은 자리다. 없으면 독스트링으로 떨어진다.
        description: nameList(node.children.map((c) => c.name)) || node.doc,
        parent: group,
      }));
      if (!classAlias.has(node.name)) classAlias.set(node.name, alias);
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
