import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../lib/api';

// api.ts는 요청 시점에 sessionStorage에서 키를 읽는다 — node 환경이라 최소 스텁을 심는다.
const store = new Map<string, string>();

beforeEach(() => {
  store.clear();
  store.set('paas_console_key', 'test-key');
  vi.stubGlobal('sessionStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function captureFetch() {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ id: 1, path: 'x' }), {
      status: 201,
      headers: { 'content-type': 'application/json' },
    });
  });
  return calls;
}

describe('파일 업로드는 multipart로 나간다', () => {
  // 회귀: FormData를 JSON 경로로 보내면 JSON.stringify(FormData)가 "{}"로 직렬화되어
  // 파일이 통째로 사라진다. 서버는 422로 거절하고 원인이 드러나지 않는다.
  it('uploadFileStorageModule이 FormData를 그대로 싣는다', async () => {
    const calls = captureFetch();
    await api.uploadFileStorageModule(new File(['zip-bytes'], 'assets.zip'), 'assets');

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('/paas/api/v1/modules/upload-storage');
    expect(calls[0].init.body).toBeInstanceOf(FormData);

    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers['content-type']).toBeUndefined(); // 브라우저가 boundary와 함께 직접 설정한다

    const fd = calls[0].init.body as FormData;
    expect(fd.get('name')).toBe('assets');
    expect(fd.get('zip_file')).toBeInstanceOf(File);
  });

  it('uploadStorageFile도 FormData를 그대로 싣는다', async () => {
    const calls = captureFetch();
    await api.uploadStorageFile('assets', new File(['png'], 'logo.png'), 'img/logo.png');

    const fd = calls[0].init.body as FormData;
    expect(calls[0].url).toBe('/paas/api/v1/storage/assets/files');
    expect(fd.get('path')).toBe('img/logo.png');
    expect(fd.get('file')).toBeInstanceOf(File);
  });
});
