import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, hostSsoRetries, SSO_LOGIN_EVENT, SSO_RETRY_EVENT } from '../lib/api';
import { loginWithAccount } from '../lib/auth';

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
  it('uploadStorageFile이 FormData를 그대로 싣는다', async () => {
    const calls = captureFetch();
    await api.uploadStorageFile('internal', new File(['png'], 'logo.png'), 'img/logo.png');

    expect(calls).toHaveLength(1);
    expect(calls[0].init.body).toBeInstanceOf(FormData);

    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers['content-type']).toBeUndefined(); // 브라우저가 boundary와 함께 직접 설정한다

    const fd = calls[0].init.body as FormData;
    expect(calls[0].url).toBe('/paas/api/v1/storage/internal/files');
    expect(fd.get('path')).toBe('img/logo.png');
    expect(fd.get('file')).toBeInstanceOf(File);
  });
});

describe('로그인은 서버 승인 없이 세션을 만들지 않는다', () => {
  // 바깥 beforeEach가 심어 둔 세션을 지우고 시작해야 "세션이 안 생겼다"를 단언할 수 있다.
  beforeEach(() => {
    store.clear();
  });

  // 회귀: 예전에는 백엔드가 닿지 않거나 거절해도 catch에서 삼키고, 키가 'paas'로
  // 시작하면 로컬에서 admin으로 승인해 버렸다. 서버는 여전히 막지만 콘솔이 admin
  // 화면을 열어 주는 데다, 발급 키는 전부 'paas_'로 시작해 사실상 항상 admin이 됐다.
  it('네트워크 오류 시 던지고 세션을 남기지 않는다', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('Failed to fetch');
    });
    await expect(loginWithAccount('u@x.com', 'paas_anything')).rejects.toThrow('서버에 연결할 수 없습니다');
    expect(store.get('paas_console_key')).toBeUndefined();
    expect(store.get('paas_console_admin')).toBeUndefined();
  });

  it('서버가 거절하면 서버 메시지로 던지고 세션을 남기지 않는다', async () => {
    vi.stubGlobal('fetch', async () =>
      new Response(JSON.stringify({ detail: '비밀번호가 올바르지 않습니다.' }), {
        status: 400,
        headers: { 'content-type': 'application/json' },
      }),
    );
    await expect(loginWithAccount('u@x.com', 'paas_wrong')).rejects.toThrow('비밀번호가 올바르지 않습니다.');
    expect(store.get('paas_console_key')).toBeUndefined();
  });

  it('admin 여부는 서버 응답만 따른다', async () => {
    vi.stubGlobal('fetch', async () =>
      new Response(JSON.stringify({ key: 'paas_issued', is_admin: false, email: 'u@x.com' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    // 'paas_'로 시작하는 키지만 서버가 일반 사용자라고 했으므로 admin이 아니다
    const out = await loginWithAccount('u@x.com', 'paas_issued');
    expect(out.admin).toBe(false);
    expect(store.get('paas_console_admin')).toBe('0');
    expect(store.get('paas_console_key')).toBe('paas_issued');
  });

  it('비밀번호는 원문을 보내고, 저장하는 것은 서버가 준 세션 토큰이다', async () => {
    // 예전에는 클라이언트가 SHA-256을 걸어 보내고 그 해시를 그대로 x-api-key로 저장했다.
    // 비밀번호의 결정적 함수가 무기한 자격증명이 되므로, 서버가 발급한 난수 토큰만 저장한다.
    const sent: string[] = [];
    vi.stubGlobal('fetch', async (_url: string, init: RequestInit) => {
      sent.push(JSON.parse(init.body as string).password);
      return new Response(
        JSON.stringify({ key: 'paass_random-token', is_admin: false, email: 'u@x.com' }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      );
    });
    await loginWithAccount('u@x.com', 'my-password');
    expect(sent[0]).toBe('my-password');
    expect(store.get('paas_console_key')).toBe('paass_random-token');
  });
});

describe('SSO 만료 응답은 승인 주소를 화면 전체에 알린다', () => {
  // 서버(app/api/llm.py provider_error)가 로그인을 이미 시작해 두었다 — 어느 화면의 요청이
  // 실패했든 같은 안내(SsoLoginNotice)가 그 주소를 연다. 메시지는 사람이 읽을 문장으로 남긴다.
  it('detail.sso_login을 이벤트로 내보내고 message만 오류로 던진다', async () => {
    const target = new EventTarget();
    vi.stubGlobal('window', target);
    const seen: unknown[] = [];
    target.addEventListener(SSO_LOGIN_EVENT, (e) => seen.push((e as CustomEvent).detail));
    const login = { profile: 'bedrock-dev', verification_url: 'https://d-1/#/device?user_code=A',
      code_autofilled: true, user_code: 'A' };
    vi.stubGlobal('fetch', async () => new Response(
      JSON.stringify({ detail: { message: 'AWS SSO 토큰이 만료됐습니다', sso_login: login } }),
      { status: 502, headers: { 'content-type': 'application/json' } }));

    await expect(api.sendSessionMessage(1, '')).rejects.toThrow('AWS SSO 토큰이 만료됐습니다');
    expect(seen).toEqual([login]);
  });

  // 실패를 삼킨 응답(성공 포함)도 서버가 만료를 만났으면 머리글에 승인 정보를 싣는다(SsoWatchMiddleware)
  it('x-paas-sso-login 머리글도 같은 이벤트로 알리고, 응답은 그대로 쓴다', async () => {
    const target = new EventTarget();
    vi.stubGlobal('window', target);
    const seen: unknown[] = [];
    target.addEventListener(SSO_LOGIN_EVENT, (e) => seen.push((e as CustomEvent).detail));
    const login = { profile: 'bedrock-dev', verification_url: 'https://d-1/#/device?user_code=B',
      code_autofilled: true, user_code: 'B' };
    vi.stubGlobal('fetch', async () => new Response(
      JSON.stringify({ release_sha: 'abc', stale: false, profile: null }),
      { status: 200, headers: { 'content-type': 'application/json', 'x-paas-sso-login': JSON.stringify(login) } }));

    const out = await api.reviewAgentProfile(1);
    expect(out.release_sha).toBe('abc');
    expect(seen).toEqual([login]);
  });
});

describe('개인 폴더 파일은 하나씩 본문 그대로 올린다', () => {
  // 여러 파일을 multipart 한 요청에 싣지 않는다 — File을 그대로 send하면 브라우저가 흘려 보낸다.
  // fetch는 올라간 바이트를 알려 주지 않아 XHR로 보낸다 — 진행률이 그 자리에서 나와야 한다.
  it('File을 PUT 본문으로 보내고 올라간 바이트를 알린다', async () => {
    const sent: Array<{ method: string; url: string; body: unknown; headers: Record<string, string> }> = [];
    class FakeXhr {
      upload: { onprogress?: (e: { loaded: number; total: number; lengthComputable: boolean }) => void } = {};
      status = 0;
      responseText = '';
      onload?: () => void;
      onerror?: () => void;
      private req = { method: '', url: '', body: null as unknown, headers: {} as Record<string, string> };
      open(method: string, url: string) { Object.assign(this.req, { method, url }); }
      setRequestHeader(k: string, v: string) { this.req.headers[k] = v; }
      send(body: unknown) {
        this.req.body = body;
        sent.push(this.req);
        this.upload.onprogress?.({ loaded: 3, total: 6, lengthComputable: true });
        this.upload.onprogress?.({ loaded: 6, total: 6, lengthComputable: true });
        this.status = 200;
        this.responseText = JSON.stringify({ path: 'plan/q3.md', status: 'saved' });
        this.onload?.();
      }
    }
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
    const file = new Blob(['# 메모']) as File;
    const seen: number[] = [];
    const out = await api.personalUpload('1.계약&품의', { file, path: 'plan/q3.md', mtime: 1700000000 },
                                         (loaded) => seen.push(loaded));
    expect(out.status).toBe('saved');
    expect(seen).toEqual([3, 6]);
    const { method, url, body, headers } = sent[0];
    expect(method).toBe('PUT');
    // 폴더 이름(한글·&)은 경로가 아니라 쿼리에 — 경로에 넣으면 IIS/ARR 앞단이 400으로 막는다
    expect(url.split('?')[0]).toMatch(/\/smartwork\/personal\/folders\/file$/);
    const q = new URLSearchParams(url.split('?')[1]);
    expect([q.get('folder'), q.get('path'), q.get('mtime')]).toEqual(['1.계약&품의', 'plan/q3.md', '1700000000']);
    expect(body).toBe(file);
    expect(headers['x-api-key']).toBe('test-key');
  });
});

describe('SSO 만료로 실패한 요청은 로그인 뒤 다시 실행한다', () => {
  // 안내 창(SsoLoginNotice)이 떠 있으면 원래 호출을 붙잡아 두고, 다시 실행한 결과를 원래 호출로 돌려준다 —
  // 화면이 스스로 다시 그려진다. 닫으면 원래 실패(또는 원래 응답)로 끝난다.
  const login = { profile: 'bedrock-dev', verification_url: 'https://d-1/#/device?user_code=C',
    code_autofilled: true, user_code: 'C' };

  function setup(responses: Response[]) {
    const target = new EventTarget();
    vi.stubGlobal('window', target);
    const held: { retry: () => void; dismiss: () => void; label: string }[] = [];
    target.addEventListener(SSO_RETRY_EVENT, (e) => held.push((e as CustomEvent).detail));
    let calls = 0;
    vi.stubGlobal('fetch', async () => responses[Math.min(calls++, responses.length - 1)]);
    return { held, calls: () => calls };
  }

  const expired = () => new Response(JSON.stringify({ detail: { message: 'AWS SSO 토큰이 만료됐습니다', sso_login: login } }),
    { status: 502, headers: { 'content-type': 'application/json' } });
  const ok = (body: unknown, extra: Record<string, string> = {}) => new Response(JSON.stringify(body),
    { status: 200, headers: { 'content-type': 'application/json', ...extra } });

  it('다시 실행 — 같은 요청을 다시 보내 원래 호출이 그 결과로 끝난다', async () => {
    const unhost = hostSsoRetries();
    try {
      const { held, calls } = setup([expired(), ok({ reply: '답', messages: [] })]);
      const pending = api.sendSessionMessage(1, '질문');
      await new Promise((r) => setTimeout(r, 0));
      expect(held.map((h) => h.label)).toEqual(['POST /smartwork/sessions/1/messages']);
      held[0].retry();
      await expect(pending).resolves.toMatchObject({ reply: '답' });
      expect(calls()).toBe(2);
    } finally {
      unhost();
    }
  });

  it('닫기 — 원래 실패로 끝나고, 실패를 삼킨 응답은 그 응답 그대로', async () => {
    const unhost = hostSsoRetries();
    try {
      let { held } = setup([expired()]);
      const failed = api.sendSessionMessage(1, '질문');
      await new Promise((r) => setTimeout(r, 0));
      held[0].dismiss();
      await expect(failed).rejects.toThrow('AWS SSO 토큰이 만료됐습니다');

      ({ held } = setup([ok({ release_sha: 'abc', stale: false, profile: null },
        { 'x-paas-sso-login': JSON.stringify(login) })]));
      const degraded = api.reviewAgentProfile(1);
      await new Promise((r) => setTimeout(r, 0));
      held[0].dismiss();
      await expect(degraded).resolves.toMatchObject({ release_sha: 'abc' });
    } finally {
      unhost();
    }
  });

  it('안내 창이 없으면 붙잡지 않는다 — 원래 호출이 영영 끝나지 않으면 안 된다', async () => {
    const { held } = setup([expired()]);
    await expect(api.sendSessionMessage(1, '질문')).rejects.toThrow('AWS SSO 토큰이 만료됐습니다');
    expect(held).toEqual([]);
  });
});
