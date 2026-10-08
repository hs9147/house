import { describe, expect, it } from 'vitest';
import { parseCsv, renderMarkdown } from '../lib/markdown';

// 보고서는 모델이 쓴 글이고, 메일 본문을 옮겨 올 수도 있다 — 서식보다 먼저 볼 것은
// **HTML이 그대로 살아나지 않는가**다.

describe('renderMarkdown', () => {
  it('escapes raw HTML before applying markup', () => {
    const html = renderMarkdown('<script>alert(1)</script> **굵게** <img src=x onerror=y>');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;script&gt;');
    expect(html).toContain('<strong>굵게</strong>');
  });

  it('links only http(s) and mailto', () => {
    expect(renderMarkdown('[a](https://x.test/p)')).toContain('<a href="https://x.test/p"');
    expect(renderMarkdown('[a](javascript:alert(1))')).not.toContain('<a ');
  });

  it('renders headings, lists, tables and code', () => {
    const html = renderMarkdown([
      '# 제목', '', '- 하나', '- 둘', '', '1. 첫째', '2. 둘째', '',
      '| 이름 | 값 |', '|---|---:|', '| a | 1 |', '| b | 2 |', '',
      '```', '**그대로** <b>', '```',
    ].join('\n'));
    expect(html).toContain('<h1>제목</h1>');
    expect(html).toContain('<ul><li>하나</li><li>둘</li></ul>');
    expect(html).toContain('<ol><li>첫째</li><li>둘째</li></ol>');
    expect(html).toContain('<thead><tr><th>이름</th><th>값</th></tr></thead>');
    expect(html).toContain('<tr><td>b</td><td>2</td></tr>');
    expect(html).toContain('<pre><code>**그대로** &lt;b&gt;</code></pre>');
  });

  it('keeps markup inside inline code literal', () => {
    expect(renderMarkdown('`**x**` 와 **y**')).toBe('<p><code>**x**</code> 와 <strong>y</strong></p>');
  });
});

describe('parseCsv', () => {
  it('reads quoted cells with commas, quotes and newlines', () => {
    expect(parseCsv('이름,메모\r\n"김, 철수","말하길 ""네""\n다음 줄"\nb,2\n')).toEqual([
      ['이름', '메모'],
      ['김, 철수', '말하길 "네"\n다음 줄'],
      ['b', '2'],
    ]);
  });
});
