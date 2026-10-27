import { describe, expect, it } from 'vitest';
import {
  applyTextEdit, changesOf, coerceCell, decodeText, editedName, encodeText, parseCell,
} from '../lib/localfiles';

describe('decodeText / encodeText', () => {
  it('reads UTF-8 and keeps whether it had a BOM', () => {
    const bytes = new Uint8Array([0xef, 0xbb, 0xbf, ...new TextEncoder().encode('단가,120')]);
    expect(decodeText(bytes)).toEqual({ text: '단가,120', bom: true, legacy: false });
  });

  it('falls back to EUC-KR, which Korean Excel writes', () => {
    // "단가" in EUC-KR
    const bytes = new Uint8Array([0xb4, 0xdc, 0xb0, 0xa1]);
    expect(decodeText(bytes)).toEqual({ text: '단가', bom: false, legacy: true });
  });

  it('writes a BOM back when the original was EUC-KR so Excel still reads Korean', () => {
    const out = encodeText('단가', { bom: false, legacy: true });
    expect(Array.from(out.slice(0, 3))).toEqual([0xef, 0xbb, 0xbf]);
    expect(decodeText(out).text).toBe('단가');
    expect(encodeText('a', { bom: false, legacy: false })).toEqual(new TextEncoder().encode('a'));
  });
});

describe('applyTextEdit', () => {
  it('replaces every occurrence and says how many', () => {
    expect(applyTextEdit('120원, 또 120원', '120원', '150원')).toEqual({ text: '150원, 또 150원', count: 2 });
  });

  it('reports a miss instead of silently doing nothing', () => {
    expect(applyTextEdit('abc', 'zzz', 'y')).toEqual({ text: 'abc', count: 0 });
    expect(applyTextEdit('abc', '', 'y').count).toBe(0);
  });
});

describe('parseCell', () => {
  it('reads sheet-qualified and bare references', () => {
    expect(parseCell('명단!b3')).toEqual({ sheet: '명단', cell: 'B3' });
    expect(parseCell("'내 시트'!$C$10")).toEqual({ sheet: '내 시트', cell: 'C10' });
    expect(parseCell('AA7')).toEqual({ sheet: null, cell: 'AA7' });
    expect(parseCell('3B')).toBeNull();
  });
});

describe('coerceCell', () => {
  it('turns plain numbers into numbers but keeps codes and long ids as text', () => {
    expect(coerceCell('1200')).toBe(1200);
    expect(coerceCell('-3.5')).toBe(-3.5);
    expect(coerceCell('007')).toBe('007');
    expect(coerceCell('1234567890123456')).toBe('1234567890123456');
    expect(coerceCell('1,200')).toBe('1,200');
  });
});

describe('editedName', () => {
  it('names the download as a copy so the original is not overwritten by accident', () => {
    expect(editedName('단가표.xlsx')).toBe('단가표 (수정).xlsx');
    expect(editedName('README')).toBe('README (수정)');
  });
});

describe('changesOf', () => {
  it('re-copies only new or changed files and drops the ones that are gone', () => {
    const stored = new Map([
      ['work/same.docx', { size: 10, lastModified: 1000 }],
      ['work/edited.xlsx', { size: 20, lastModified: 1000 }],
      ['work/gone.txt', { size: 5, lastModified: 1000 }],
      ['other/x.txt', { size: 5, lastModified: 1000 }],
    ]);
    const out = changesOf('work', stored, [
      { path: 'same.docx', file: { size: 10, lastModified: 1000 } },
      { path: 'edited.xlsx', file: { size: 20, lastModified: 2000 } },
      { path: 'new/plan.md', file: { size: 3, lastModified: 3000 } },
    ]);
    expect(out).toEqual({ put: ['work/edited.xlsx', 'work/new/plan.md'], remove: ['work/gone.txt'] });
    expect(changesOf('work', new Map([['work/a.md', { size: 1, lastModified: 1 }]]),
      [{ path: 'a.md', file: { size: 1, lastModified: 1 } }])).toEqual({ put: [], remove: [] });
  });
});
