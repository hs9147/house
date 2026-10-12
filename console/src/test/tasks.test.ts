import { describe, expect, it } from 'vitest';
import { cancelTask, dismissTask, isRunning, listTasks, runTask, subscribe } from '../lib/tasks';

describe('runTask', () => {
  it('keeps progress and the result after the caller is gone, until dismissed', async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const seen: string[] = [];
    const off = subscribe(() => seen.push(listTasks()[0]?.progress ?? ''));
    const job = runTask('sync', { label: '폴더 동기화' }, async (h) => {
      h.set({ progress: '1/2 올리는 중', sent: 10, total: 20 });
      await gate;
      h.set({ progress: '문서 2개 — 새로 변환 2' });
    });
    expect(isRunning('sync')).toBe(true);
    release();
    await job;
    off();
    const [task] = listTasks();
    expect(task).toMatchObject({ state: 'done', progress: '문서 2개 — 새로 변환 2', sent: undefined });
    expect(seen).toContain('1/2 올리는 중');
    dismissTask('sync');
    expect(listTasks()).toEqual([]);
  });

  it('refuses to start the same task twice', async () => {
    let release!: () => void;
    const first = runTask('dup', { label: '폴더 동기화' }, () => new Promise<void>((r) => { release = r; }));
    await expect(runTask('dup', { label: '폴더 동기화' }, async () => {})).rejects.toThrow('이미 진행 중');
    dismissTask('dup');  // 돌고 있는 것은 치워지지 않는다
    expect(isRunning('dup')).toBe(true);
    release();
    await first;
    dismissTask('dup');
  });

  it('records failure and cancellation', async () => {
    await expect(runTask('bad', { label: 'x' }, async () => { throw new Error('서버 오류'); }))
      .rejects.toThrow('서버 오류');
    expect(listTasks().find((t) => t.key === 'bad')).toMatchObject({ state: 'failed', error: '서버 오류' });
    dismissTask('bad');

    const job = runTask('mail', { label: '메일' }, async (h) => {
      while (!h.cancelled()) await new Promise((r) => setTimeout(r, 1));
    });
    cancelTask('mail');
    await job;
    expect(listTasks().find((t) => t.key === 'mail')?.state).toBe('cancelled');
    dismissTask('mail');
  });
});
