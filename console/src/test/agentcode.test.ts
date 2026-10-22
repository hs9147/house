import { describe, expect, it } from 'vitest';
import { agentCode } from '../lib/agentCode';

const base = { origin: 'http://gpax.lge.com', pathname: '/console/' };
const body = (code: string) => decodeURIComponent(code).replace(/^javascript:/, '');

describe('통합 북마크 GPAX', () => {
  it('사람·사이트와 상관없이 콘솔 주소만 든 같은 코드 하나다', () => {
    const code = agentCode(base);
    expect(() => new Function(body(code))).not.toThrow();
    expect(code).toContain('"receive":"http://gpax.lge.com/console/#/agent"');
    expect(code).not.toContain('__GPAX');
    expect(agentCode(base)).toBe(code);
  });

  it('웹 Outlook에서는 메일 코드가 돈다 — 열린 메일이 없으면 그렇다고 알린다', () => {
    const said: string[] = [];
    const run = new Function('location', 'document', 'alert', 'window', body(agentCode(base)));
    run({ hostname: 'outlook.cloud.microsoft' }, { querySelector: () => null }, (t: string) => said.push(t), {});
    expect(said[0]).toContain('열린 메일을 찾지 못했습니다');
  });
});
