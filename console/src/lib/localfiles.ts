/**
 * 파일 뷰어가 여는 내 PC 파일 — 브라우저가 쥐고 있는 File을 이 페이지가 살아 있는 동안 기억한다.
 *
 * 서버에는 원본이 없다(services/personal.py). 콘솔이 평문 http라 브라우저의 파일 시스템
 * API(showDirectoryPicker)도 없어서, 한 번 고른 폴더를 다시 읽을 수도 PC의 원본에 바로 쓸
 * 수도 없다. 그래서 동기화할 때 고른 폴더의 File을 남겨 두고, 없을 때만 같은 폴더를 다시
 * 고르게 한다. 고친 결과는 내려받기로 저장한다.
 *
 * 남겨 두는 곳은 둘이다 — 이 페이지의 메모리와, 새로 고쳐도 남도록 **이 브라우저의 IndexedDB**.
 * IndexedDB는 File을 그 순간의 사본으로 담는다(이 PC의 브라우저 안 — 서버로 가지 않는다). 그래서
 * 다시 동기화하면 그 폴더의 사본을 통째로 바꾸고, 폴더를 제거하거나 동의를 철회하면 지운다.
 */
const remembered = new Map<string, File>();

const DB_NAME = 'paas-localfiles';
const STORE = 'files';

function openDb(): Promise<IDBDatabase> | null {
  if (typeof indexedDB === 'undefined') return null;
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

/** 한 트랜잭션 — 끝나면 닫는다. IndexedDB가 없거나(시험 환경) 실패하면 메모리만 쓴다. */
async function withStore<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T> | void): Promise<T | undefined> {
  const opening = openDb();
  if (!opening) return undefined;
  const db = await opening;
  try {
    return await new Promise<T | undefined>((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const req = fn(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(req ? req.result : undefined);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

const folderRange = (folder: string) => IDBKeyRange.bound(`${folder}/`, `${folder}/￿`);

/** 동기화할 때 고른 폴더의 파일 — 그 폴더의 이전 사본은 통째로 바꾼다(지운 파일이 남지 않게). */
export async function remember(folder: string, items: { path: string; file: File }[]): Promise<void> {
  for (const key of [...remembered.keys()]) if (key.startsWith(`${folder}/`)) remembered.delete(key);
  for (const it of items) remembered.set(`${folder}/${it.path}`, it.file);
  await withStore('readwrite', (s) => {
    s.delete(folderRange(folder));
    for (const it of items) s.put(it.file, `${folder}/${it.path}`);
  }).catch(() => { /* 저장 공간이 모자라면 이 페이지 동안만 기억한다 */ });
}

export async function lookup(folder: string, path: string): Promise<File | undefined> {
  const key = `${folder}/${path}`;
  const hit = remembered.get(key);
  if (hit) return hit;
  const stored = await withStore<File>('readonly', (s) => s.get(key)).catch(() => undefined);
  if (stored) remembered.set(key, stored);
  return stored;
}

/** 폴더 제거(folder)·동의 철회(없음) — 이 브라우저에 남긴 사본도 지운다. */
export async function forget(folder?: string): Promise<void> {
  for (const key of [...remembered.keys()]) {
    if (folder === undefined || key.startsWith(`${folder}/`)) remembered.delete(key);
  }
  await withStore('readwrite', (s) => {
    if (folder === undefined) s.clear(); else s.delete(folderRange(folder));
  }).catch(() => undefined);
}

export interface DecodedText {
  text: string;
  bom: boolean;
  // UTF-8이 아니었다(한글 윈도우의 메모장·엑셀이 저장한 EUC-KR/CP949).
  legacy: boolean;
}

/** UTF-8로 읽어 보고, 깨지면 EUC-KR로 — 한글 윈도우의 CSV는 대개 후자다. */
export function decodeText(bytes: ArrayBuffer | Uint8Array): DecodedText {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const bom = view[0] === 0xef && view[1] === 0xbb && view[2] === 0xbf;
  try {
    return { text: new TextDecoder('utf-8', { fatal: true }).decode(view), bom, legacy: false };
  } catch {
    return { text: new TextDecoder('euc-kr').decode(view), bom: false, legacy: true };
  }
}

/**
 * 저장은 UTF-8로만 된다(브라우저의 TextEncoder가 다른 인코딩을 쓰지 못한다). 원본이 EUC-KR이었거나
 * BOM이 있었으면 BOM을 붙인다 — BOM이 없는 UTF-8 CSV를 한글 엑셀은 깨진 글자로 연다.
 */
export function encodeText(text: string, from: { bom: boolean; legacy: boolean }): Uint8Array<ArrayBuffer> {
  const body = new TextEncoder().encode(text);
  if (!from.bom && !from.legacy) return body;
  const out = new Uint8Array(body.length + 3);
  out.set([0xef, 0xbb, 0xbf]);
  out.set(body, 3);
  return out;
}

/** 수정안 하나(find → replace)를 적용한다. 같은 문구가 여러 곳이면 모두 — 몇 곳인지 함께 돌려준다. */
export function applyTextEdit(text: string, find: string, replace: string): { text: string; count: number } {
  if (!find) return { text, count: 0 };
  const parts = text.split(find);
  return { text: parts.join(replace), count: parts.length - 1 };
}

/** '시트이름!B3' · "'내 시트'!B3" · 'B3'(시트를 안 밝히면 null) — 못 읽으면 null. */
export function parseCell(ref: string): { sheet: string | null; cell: string } | null {
  const m = /^(?:'?(.+?)'?!)?\$?([A-Za-z]{1,3})\$?(\d{1,7})$/.exec(ref.trim());
  if (!m) return null;
  return { sheet: m[1] ?? null, cell: `${m[2].toUpperCase()}${m[3]}` };
}

/**
 * 셀에 넣을 값 — 숫자처럼 생긴 글자는 숫자로. 단 0으로 시작하는 코드(007)와 15자리를 넘는
 * 번호(사업자·계좌)는 글자로 둔다: 숫자로 바꾸면 앞의 0이 사라지거나 끝자리가 뭉개진다.
 */
export function coerceCell(value: string): string | number {
  const v = value.trim();
  if (!/^-?(0|[1-9]\d*)(\.\d+)?$/.test(v) || v.replace(/[-.]/g, '').length > 15) return value;
  return Number(v);
}

/** 고친 PC 파일은 사본 이름으로 내려받는다 — 원본을 덮을지는 사람이 정한다. */
export function editedName(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? `${name.slice(0, dot)} (수정)${name.slice(dot)}` : `${name} (수정)`;
}
