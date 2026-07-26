// unified diff 파서 — DiffView 렌더링용. 의존성 없이 라인 단위 분류만 한다.

export type DiffKind = 'meta' | 'hunk' | 'add' | 'del' | 'ctx';

export interface DiffLine {
  kind: DiffKind;
  text: string;
}

export function parseUnifiedDiff(diff: string): DiffLine[] {
  const lines = diff.replace(/\n$/, '').split('\n');
  const out: DiffLine[] = [];
  for (const text of lines) {
    let kind: DiffKind;
    if (
      text.startsWith('diff --git') ||
      text.startsWith('index ') ||
      text.startsWith('--- ') ||
      text.startsWith('+++ ') ||
      text.startsWith('new file') ||
      text.startsWith('deleted file') ||
      text.startsWith('\\ No newline')
    ) {
      kind = 'meta';
    } else if (text.startsWith('@@')) {
      kind = 'hunk';
    } else if (text.startsWith('+')) {
      kind = 'add';
    } else if (text.startsWith('-')) {
      kind = 'del';
    } else {
      kind = 'ctx';
    }
    out.push({ kind, text });
  }
  return out;
}

/** 채팅 응답에서 ```diff 펜스 블록을 추출 (백엔드 extract_diff와 동일 규칙의 클라이언트판). */
export function extractDiffFromReply(reply: string): string | null {
  const fence = /```(?:diff|patch)\n([\s\S]*?)```/.exec(reply);
  if (fence) {
    return fence[1].trim() ? fence[1] : null;
  }
  const lines = reply.split('\n');
  const start = lines.findIndex(
    (l) => l.startsWith('diff --git ') || l.startsWith('--- a/') || l.startsWith('--- /dev/null'),
  );
  return start >= 0 ? lines.slice(start).join('\n') : null;
}
