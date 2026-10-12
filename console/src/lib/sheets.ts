import type { WorkBook, WorkSheet } from 'xlsx';
import { coerceCell } from './localfiles';

/**
 * 파일 뷰어의 시트 쓰기 — SheetJS(xlsx)는 화면을 열 때만 받으므로(동적 import) 모듈을 인자로 받는다.
 */
type Xlsx = typeof import('xlsx');

/** 대화가 만든 시트(글자 칸) → 통합 문서. 숫자처럼 생긴 칸은 숫자로(lib/localfiles.coerceCell). */
export function bookFromRows(X: Xlsx, sheets: { name: string; rows: string[][] }[]): WorkBook {
  const book = X.utils.book_new();
  for (const s of sheets) {
    X.utils.book_append_sheet(book, X.utils.aoa_to_sheet(s.rows.map((r) => r.map(coerceCell))), s.name);
  }
  return book;
}

/** 칸 하나를 고친다. 빈 값이면 칸을 지운다. */
export function writeCell(X: Xlsx, ws: WorkSheet, addr: string, value: string): void {
  const old = ws[addr];
  const v = coerceCell(value);
  if (value === '') delete ws[addr];
  // 표시 형식(z)은 남긴다 — 금액·날짜 칸이 고친 뒤에 맨 숫자로 바뀌지 않게. 수식(f)은 버린다:
  // 값을 직접 넣은 칸에 옛 수식이 남으면 엑셀이 열 때 그 수식으로 다시 계산해 덮는다.
  else ws[addr] = { ...(old?.z ? { z: old.z } : {}), t: typeof v === 'number' ? 'n' : 's', v };
  // 범위 밖에 쓰면 !ref를 넓힌다 — 안 넓히면 저장본에서 그 칸이 빠진다.
  const range = X.utils.decode_range(ws['!ref'] ?? addr);
  const at = X.utils.decode_cell(addr);
  range.s.r = Math.min(range.s.r, at.r);
  range.s.c = Math.min(range.s.c, at.c);
  range.e.r = Math.max(range.e.r, at.r);
  range.e.c = Math.max(range.e.c, at.c);
  ws['!ref'] = X.utils.encode_range(range);
}
