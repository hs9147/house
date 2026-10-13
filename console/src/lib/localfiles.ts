/**
 * 내 PC 파일 — 동기화할 때 고른 폴더를 기억해 두고, 파일 뷰어와 다음 동기화가 다시 고르지 않게 한다.
 *
 * 서버에는 원본이 없다(services/personal.py). 기억하는 것은 셋이다(모두 이 PC의 브라우저 안 —
 * 서버로 가지 않는다):
 *  - 폴더 핸들(showDirectoryPicker) — **그 폴더를 다시 읽을 권한**. 다음 동기화는 폴더를 다시
 *    고르지 않고 이것으로 지금의 파일을 읽는다. 폴더가 없어졌거나 핸들이 없을 때만 다시 고른다.
 *    이 API는 보안 출처(https·localhost)에서만 있다 — 평문 http면 없어서 매번 고른다(canKeepFolders).
 *  - 고른 파일의 사본(IndexedDB는 File을 그 순간의 사본으로 담는다) — 파일 뷰어가 새로 고친
 *    뒤에도 연다. 다시 동기화하면 그 폴더의 사본을 통째로 바꾼다.
 *  - 이 페이지의 메모리 — 위 둘의 빠른 길.
 * 폴더를 제거하거나 동의를 철회하면 모두 지운다. 고친 결과는 내려받기로 저장한다.
 */
const remembered = new Map<string, File>();

const DB_NAME = 'paas-localfiles';
const FILES = 'files';
const HANDLES = 'handles';

function openDb(): Promise<IDBDatabase> | null {
  if (typeof indexedDB === 'undefined') return null;
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 2);
    req.onupgradeneeded = () => {
      for (const name of [FILES, HANDLES]) {
        if (!req.result.objectStoreNames.contains(name)) req.result.createObjectStore(name);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

/** 한 트랜잭션 — 끝나면 닫는다. IndexedDB가 없거나(시험 환경) 실패하면 메모리만 쓴다. */
async function withStore<T>(store: string, mode: IDBTransactionMode,
  fn: (s: IDBObjectStore) => IDBRequest<T> | void): Promise<T | undefined> {
  const opening = openDb();
  if (!opening) return undefined;
  const db = await opening;
  try {
    return await new Promise<T | undefined>((resolve, reject) => {
      const tx = db.transaction(store, mode);
      const req = fn(tx.objectStore(store));
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
  await withStore(FILES, 'readwrite', (s) => {
    s.delete(folderRange(folder));
    for (const it of items) s.put(it.file, `${folder}/${it.path}`);
  }).catch(() => { /* 저장 공간이 모자라면 이 페이지 동안만 기억한다 */ });
}

export async function lookup(folder: string, path: string): Promise<File | undefined> {
  const key = `${folder}/${path}`;
  const hit = remembered.get(key);
  if (hit) return hit;
  const stored = await withStore<File>(FILES, 'readonly', (s) => s.get(key)).catch(() => undefined);
  if (stored) remembered.set(key, stored);
  return stored;
}

/** 폴더 제거(folder)·동의 철회(없음) — 이 브라우저에 남긴 사본과 폴더 핸들도 지운다. */
export async function forget(folder?: string): Promise<void> {
  for (const key of [...remembered.keys()]) {
    if (folder === undefined || key.startsWith(`${folder}/`)) remembered.delete(key);
  }
  await withStore(FILES, 'readwrite', (s) => {
    if (folder === undefined) s.clear(); else s.delete(folderRange(folder));
  }).catch(() => undefined);
  await withStore(HANDLES, 'readwrite', (s) => {
    if (folder === undefined) s.clear(); else s.delete(folder);
  }).catch(() => undefined);
}

// --- 폴더 핸들 — TS의 DOM 정의에 아직 없는 부분만 적는다 ---

interface DirHandle {
  kind: 'directory';
  name: string;
  values(): AsyncIterableIterator<DirHandle | FileHandle>;
  queryPermission(opts: { mode: 'read' }): Promise<PermissionState>;
  requestPermission(opts: { mode: 'read' }): Promise<PermissionState>;
}
interface FileHandle {
  kind: 'file';
  name: string;
  getFile(): Promise<File>;
}
type PickerWindow = { showDirectoryPicker(opts?: { mode?: 'read'; id?: string }): Promise<DirHandle> };

export interface PickedFolder {
  folder: string;
  // 폴더 안의 상대 경로와 파일 — 핸들로 읽었으면 read가 지금의 파일을 읽는다(무거운 일이라 작업 안에서).
  read: () => Promise<{ path: string; file: File }[]>;
}

/** 이 브라우저가 고른 폴더를 다시 읽을 수 있는가 — 보안 출처(https·localhost)에서만. */
export const canKeepFolders = typeof window !== 'undefined' && 'showDirectoryPicker' in window;

async function walk(dir: DirHandle, prefix = ''): Promise<{ path: string; file: File }[]> {
  const out: { path: string; file: File }[] = [];
  for await (const entry of dir.values()) {
    const path = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.kind === 'file') out.push({ path, file: await entry.getFile() });
    else out.push(...await walk(entry, path));
  }
  return out;
}

/** 새 폴더를 고른다 — 핸들을 남겨 다음 동기화가 다시 고르지 않게. 취소하면 null. */
export async function pickFolder(): Promise<PickedFolder | null> {
  let dir: DirHandle;
  try {
    dir = await (window as unknown as PickerWindow).showDirectoryPicker({ mode: 'read', id: 'paas-sync' });
  } catch (e) {
    if ((e as DOMException).name === 'AbortError') return null;
    throw e;
  }
  await withStore(HANDLES, 'readwrite', (s) => { s.put(dir, dir.name); }).catch(() => undefined);
  return { folder: dir.name, read: () => walk(dir) };
}

/**
 * 전에 고른 폴더를 다시 연다 — 핸들이 없거나, 읽기 권한을 받지 못했거나, 폴더가 없어졌으면 null
 * (그때만 다시 고른다). 권한 확인은 버튼을 누른 그 자리에서 불러야 한다(사람의 동작이 있어야 묻는다).
 */
export async function reopenFolder(folder: string): Promise<PickedFolder | null> {
  if (!canKeepFolders) return null;
  const dir = await withStore<DirHandle>(HANDLES, 'readonly', (s) => s.get(folder)).catch(() => undefined);
  if (!dir) return null;
  try {
    if (await dir.queryPermission({ mode: 'read' }) !== 'granted'
        && await dir.requestPermission({ mode: 'read' }) !== 'granted') return null;
    await dir.values().next();  // 폴더가 없어졌으면 여기서 NotFoundError
  } catch {
    return null;
  }
  return { folder, read: () => walk(dir) };
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
