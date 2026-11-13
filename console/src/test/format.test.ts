import { describe, expect, it } from 'vitest';
import { fmtElapsed, fmtKst, serverTime } from '../lib/format';

describe('serverTime', () => {
  it('시간대가 없으면 UTC로 읽는다 — 서버는 UTC로 저장하고 SQLite는 시간대 없이 돌려준다', () => {
    expect(serverTime('2026-11-11T03:46:27.694337').toISOString()).toBe('2026-11-11T03:46:27.694Z');
    expect(serverTime('2026-11-11 03:46:27').toISOString()).toBe('2026-11-11T03:46:27.000Z');
  });
  it('시간대가 있으면 그대로', () => {
    expect(serverTime('2026-11-11T03:46:27Z').toISOString()).toBe('2026-11-11T03:46:27.000Z');
    expect(serverTime('2026-11-11T12:46:27+09:00').toISOString()).toBe('2026-11-11T03:46:27.000Z');
  });
});

describe('fmtElapsed', () => {
  it('초·분·시간', () => {
    expect(fmtElapsed(42_000)).toBe('42초');
    expect(fmtElapsed(185_000)).toBe('3분 05초');
    expect(fmtElapsed(3_720_000)).toBe('1시간 02분');
    expect(fmtElapsed(-5)).toBe('0초');
  });
});

describe('fmtKst', () => {
  it('UTC 시각을 KST(+9)로 — 날짜가 넘어가도', () => {
    expect(fmtKst(serverTime('2026-11-13T07:40:05'))).toBe('11-13 16:40:05 KST');
    expect(fmtKst(serverTime('2026-11-13T15:00:00Z'))).toBe('11-14 00:00:00 KST');
  });
});
