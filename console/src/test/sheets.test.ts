import { describe, expect, it } from 'vitest';
import * as X from 'xlsx';
import { bookFromRows, writeCell } from '../lib/sheets';

// 내려받은 사본을 다시 읽어 본다 — 화면에서 보인 값이 파일에 그대로 들어갔는지가 기준이다.
function roundTrip(book: X.WorkBook, bookType: 'xlsx' | 'biff8' = 'xlsx'): X.WorkBook {
  return X.read(X.write(book, { bookType, type: 'array' }), { type: 'array' });
}

describe('bookFromRows', () => {
  it('keeps numbers as numbers and codes as text', () => {
    const back = roundTrip(bookFromRows(X, [{ name: '명단', rows: [['코드', '점수'], ['007', '90']] }]));
    const ws = back.Sheets['명단'];
    expect(ws.A2.v).toBe('007');
    expect(ws.B2).toMatchObject({ t: 'n', v: 90 });
  });
});

describe('writeCell', () => {
  it('writes inside and outside the used range and survives xlsx and xls', () => {
    for (const bookType of ['xlsx', 'biff8'] as const) {
      const book = bookFromRows(X, [{ name: 'S', rows: [['단가', '120']] }]);
      const ws = book.Sheets.S;
      writeCell(X, ws, 'B1', '150');
      writeCell(X, ws, 'D5', '비고');  // 범위 밖 — !ref가 넓어져야 저장본에 남는다
      const back = roundTrip(book, bookType).Sheets.S;
      expect(back.B1).toMatchObject({ t: 'n', v: 150 });
      expect(back.D5.v).toBe('비고');
    }
  });

  it('keeps the number format but drops an old formula', () => {
    const book = bookFromRows(X, [{ name: 'S', rows: [['1', '2', '']] }]);
    const ws = book.Sheets.S;
    ws.C1 = { t: 'n', v: 3, f: 'A1+B1', z: '#,##0' };
    writeCell(X, ws, 'C1', '1000');
    expect(ws.C1).toEqual({ t: 'n', v: 1000, z: '#,##0' });
  });

  it('clears a cell on empty value', () => {
    const book = bookFromRows(X, [{ name: 'S', rows: [['a', 'b']] }]);
    writeCell(X, book.Sheets.S, 'B1', '');
    expect(roundTrip(book).Sheets.S.B1).toBeUndefined();
  });
});
