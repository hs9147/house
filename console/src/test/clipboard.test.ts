import { afterEach, describe, expect, it, vi } from 'vitest';
import { copyText } from '../lib/clipboard';

// 평문 http(사내 접속)에서는 navigator.clipboard가 아예 없다. 그것만 쓰면 복사가 조용히
// 안 되고 화면은 "복사됨"이라고 말한다 — 그래서 **성공 여부를 돌려주는** 것이 요점이다.

function fakeDom(execResult: boolean | (() => never)) {
  const removed: unknown[] = [];
  const area = {
    value: '', style: {} as Record<string, string>,
    setAttribute: vi.fn(), select: vi.fn(), setSelectionRange: vi.fn(),
  };
  vi.stubGlobal('document', {
    createElement: () => area,
    body: { appendChild: vi.fn(), removeChild: (n: unknown) => removed.push(n) },
    execCommand: () => {
      if (typeof execResult === 'function') return execResult();
      return execResult;
    },
  });
  return { area, removed };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('copyText', () => {
  it('보안 컨텍스트에서는 표준 API를 쓴다', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    expect(await copyText('설정')).toBe(true);
    expect(writeText).toHaveBeenCalledWith('설정');
  });

  it('clipboard가 없으면(평문 http) 폴백으로 복사한다', async () => {
    vi.stubGlobal('navigator', {});
    const { area } = fakeDom(true);
    expect(await copyText('토큰')).toBe(true);
    expect(area.value).toBe('토큰');
    expect(area.select).toHaveBeenCalled();
  });

  it('표준 API가 거부되면 폴백으로 넘어간다', async () => {
    // 권한 거부·포커스 없음 — 여기서 멈추면 사용자는 이유 없이 복사가 안 된다.
    vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('no')) } });
    fakeDom(true);
    expect(await copyText('x')).toBe(true);
  });

  it('둘 다 안 되면 false — 호출측이 "직접 복사하세요"를 말해야 한다', async () => {
    vi.stubGlobal('navigator', {});
    fakeDom(false);
    expect(await copyText('x')).toBe(false);
  });

  it('폴백이 예외를 던져도 false로 떨어진다(화면이 죽지 않는다)', async () => {
    vi.stubGlobal('navigator', {});
    fakeDom(() => { throw new Error('execCommand 없음'); });
    expect(await copyText('x')).toBe(false);
  });

  it('복사한 뒤 임시 textarea를 치운다 — 화면에 쌓이면 안 된다', async () => {
    vi.stubGlobal('navigator', {});
    const { area, removed } = fakeDom(true);
    await copyText('x');
    expect(removed).toEqual([area]);
  });
});
