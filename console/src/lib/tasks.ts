import { useSyncExternalStore } from 'react';

/**
 * 브라우저가 직접 돌리는 긴 작업 — **화면(컴포넌트) 밖에** 둔다.
 *
 * 폴더 동기화는 PC의 파일을 하나씩 올리는 일이라 서버가 대신 할 수 없다(서버는 PC를 읽지
 * 못한다). 그 루프를 화면 안에서 돌렸더니, 다른 메뉴로 가거나 「대화/설정」 탭만 바꿔도
 * 진행 표시가 화면과 함께 사라졌다. 루프는 계속 돌았지만 사람은 멈춘 줄 알았고, 돌아와 다시
 * 누르면 같은 폴더를 두 루프가 함께 올렸다. 작업을 여기 두면 화면이 바뀌어도 이어지고,
 * 어느 화면에서든 진행이 보이며(components/TaskTray), 같은 작업은 두 번 걸리지 않는다.
 *
 * 페이지를 닫거나 새로 고치면 브라우저가 작업을 끊는다 — 그때는 먼저 묻는다(beforeunload).
 */
export interface Task {
  key: string;
  label: string;
  // 이 작업을 보는 화면 — 트레이에서 그리로 간다.
  to?: string;
  state: 'running' | 'done' | 'failed' | 'cancelled';
  progress: string;
  sent?: number;
  total?: number;
  // 작업마다의 값(메일 로그인의 주소·코드 등) — 화면이 다시 열려도 보여야 하는 것.
  data?: unknown;
  error?: string;
  startedAt: number;
}

export interface TaskHandle {
  set(patch: Partial<Pick<Task, 'progress' | 'sent' | 'total' | 'data'>>): void;
  cancelled(): boolean;
}

const tasks = new Map<string, Task>();
const cancels = new Set<string>();
const listeners = new Set<() => void>();
let snapshot: Task[] = [];

function emit() {
  snapshot = [...tasks.values()];
  listeners.forEach((l) => l());
}

function patch(key: string, change: Partial<Task>) {
  const t = tasks.get(key);
  if (!t) return;
  tasks.set(key, { ...t, ...change });
  emit();
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function listTasks(): Task[] {
  return snapshot;
}

export function isRunning(key: string): boolean {
  return tasks.get(key)?.state === 'running';
}

/**
 * 작업을 건다. 같은 key가 돌고 있으면 걸지 않고 예외를 던진다 — 같은 폴더를 두 루프가 함께
 * 올리던 것을 여기서 막는다. 끝난 작업은 dismissTask로 치울 때까지 결과(progress·error)가 남는다.
 */
export async function runTask<T>(
  key: string,
  meta: { label: string; to?: string },
  fn: (h: TaskHandle) => Promise<T>,
): Promise<T> {
  if (isRunning(key)) throw new Error(`이미 진행 중입니다 — ${meta.label}`);
  cancels.delete(key);
  tasks.set(key, { key, ...meta, state: 'running', progress: '', startedAt: Date.now() });
  emit();
  const handle: TaskHandle = {
    set: (change) => patch(key, change),
    cancelled: () => cancels.has(key),
  };
  try {
    const out = await fn(handle);
    patch(key, { state: cancels.has(key) ? 'cancelled' : 'done', sent: undefined, total: undefined });
    return out;
  } catch (e) {
    patch(key, { state: 'failed', error: (e as Error).message, sent: undefined, total: undefined });
    throw e;
  }
}

/** 작업에게 멈추라고 알린다 — 작업이 handle.cancelled()를 볼 때 멈춘다. */
export function cancelTask(key: string): void {
  if (isRunning(key)) cancels.add(key);
}

/** 끝난 작업을 치운다. 돌고 있는 것은 치우지 않는다. */
export function dismissTask(key: string): void {
  if (tasks.has(key) && !isRunning(key)) {
    tasks.delete(key);
    emit();
  }
}

export function useTasks(): Task[] {
  return useSyncExternalStore(subscribe, listTasks);
}

export function useTask(key: string): Task | undefined {
  return useTasks().find((t) => t.key === key);
}

if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', (e) => {
    if (snapshot.some((t) => t.state === 'running')) {
      // 문구는 브라우저가 정한다(사이트가 바꿀 수 없다) — 막는 것만 알린다.
      e.preventDefault();
      e.returnValue = '';
    }
  });
}
