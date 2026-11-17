/**
 * 스마트워크 보고서용 작은 마크다운 렌더러 — 제목·목록·표·코드·굵게·기울임·링크.
 *
 * 라이브러리를 들이지 않은 이유: 보고서는 모델이 쓰는 글이라 쓰는 문법이 좁고, 콘솔은 사내
 * 설치본이라 의존성 하나가 곧 갱신 부담이다. 대신 **먼저 전부 이스케이프하고** 그 위에 태그를
 * 입힌다 — 모델이 쓴(또는 메일 본문에서 옮겨 온) HTML이 그대로 실행되면 안 된다.
 * 링크는 http(s)·mailto만 건다(javascript: 주소를 막는다).
 */

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function inline(text: string): string {
  // 인라인 코드는 먼저 떼어 둔다 — 그 안의 **나 [ ]를 서식으로 읽으면 안 된다.
  const codes: string[] = [];
  let s = escapeHtml(text).replace(/`([^`]+)`/g, (_, c: string) => {
    codes.push(`<code>${c}</code>`);
    return `\u0000${codes.length - 1}\u0000`;
  });
  s = s
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\(((?:https?:\/\/|mailto:)[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i: string) => codes[Number(i)]);
}

function cells(row: string): string[] {
  return row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
}

const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

export function renderMarkdown(src: string): string {
  const lines = src.replace(/\r\n?/g, '\n').split('\n');
  const out: string[] = [];
  let para: string[] = [];
  const flush = () => {
    if (para.length) out.push(`<p>${inline(para.join(' '))}</p>`);
    para = [];
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = line.match(/^\s*```/);
    if (fence) {
      flush();
      const body: string[] = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) body.push(lines[i]);
      out.push(`<pre><code>${escapeHtml(body.join('\n'))}</code></pre>`);
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flush();
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
      flush();
      const head = cells(line).map((c) => `<th>${inline(c)}</th>`).join('');
      const rows: string[] = [];
      for (i += 2; i < lines.length && lines[i].includes('|'); i++) {
        rows.push(`<tr>${cells(lines[i]).map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`);
      }
      i--;
      out.push(`<table><thead><tr>${head}</tr></thead><tbody>${rows.join('')}</tbody></table>`);
      continue;
    }
    const item = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
    if (item) {
      flush();
      const ordered = /\d/.test(item[1]);
      const items: string[] = [];
      for (; i < lines.length; i++) {
        const m = lines[i].match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
        if (!m || /\d/.test(m[1]) !== ordered) break;
        items.push(`<li>${inline(m[2])}</li>`);
      }
      i--;
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag}>${items.join('')}</${tag}>`);
      continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      flush();
      out.push('<hr>');
      continue;
    }
    if (!line.trim()) {
      flush();
      continue;
    }
    if (/^\s*>/.test(line)) {
      flush();
      out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ''))}</blockquote>`);
      continue;
    }
    para.push(line.trim());
  }
  flush();
  return out.join('\n');
}

/** CSV → 행 배열. 따옴표로 감싼 칸(쉼표·줄바꿈·"" 포함)을 읽는다. */
const C4_KIND = /^\s*C4(Context|Container|Component|Dynamic|Deployment)\b/;

/** 문서에서 mermaid C4 블록(첫 줄이 C4Context·C4Container·C4Component …)을 뺀다 — 에이전트 기획의 산출물 보기는
 *  그 그림을 오른쪽 「단계별 시각화」가 그리므로 본문에서 코드로 또 보일 필요가 없다. 뺀 자리에는 note 한 줄을
 *  남긴다(연속한 블록은 한 줄로). C4가 아닌 코드 블록은 그대로다. 원문(편집·확정되는 문서)은 건드리지 않는다. */
export function stripC4Blocks(src: string, note: string): { text: string; removed: number } {
  const lines = src.replace(/\r\n?/g, '\n').split('\n');
  const out: string[] = [];
  let removed = 0;
  for (let i = 0; i < lines.length; i++) {
    const open = /^\s*```\s*mermaid\s*$/i.test(lines[i]);
    const first = open ? lines.slice(i + 1).find((l) => l.trim() !== '') ?? '' : '';
    if (open && C4_KIND.test(first)) {
      let j = i + 1;
      while (j < lines.length && !/^\s*```\s*$/.test(lines[j])) j++;
      if (!out.length || out[out.length - 1] !== note) out.push(note);
      removed++;
      i = j;
      continue;
    }
    out.push(lines[i]);
  }
  return { text: out.join('\n'), removed };
}

export function parseCsv(src: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = '';
  let quoted = false;
  const text = src.replace(/\r\n?/g, '\n');
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (ch === '"') {
        quoted = false;
      } else {
        cell += ch;
      }
    } else if (ch === '"') {
      quoted = true;
    } else if (ch === ',') {
      row.push(cell);
      cell = '';
    } else if (ch === '\n') {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = '';
    } else {
      cell += ch;
    }
  }
  if (cell || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}
