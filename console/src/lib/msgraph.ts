/**
 * 아웃룩 메일 — 로그인과 메일 읽기를 **이 브라우저에서** 한다.
 *
 * 서버는 토큰을 받지 않는다: 팝업으로 Microsoft에 로그인(인가 코드 + PKCE)하고, 받은
 * 토큰으로 Graph에서 받은편지함을 읽어 메일 내용만 서버로 보낸다. 토큰은 이 페이지의
 * 메모리에만 있다가 새로고침하면 사라진다(다시 누르면 다시 로그인 — Microsoft 세션이
 * 살아 있으면 팝업이 곧 닫힌다).
 *
 * MSAL을 들이지 않은 이유: 필요한 것은 "팝업 로그인 한 번, 토큰 하나"뿐이고, 콘솔은 사내
 * 설치본이라 의존성 하나가 곧 갱신 부담이다(lib/markdown.ts와 같은 판단).
 * 브라우저에서 토큰을 바꾸려면 앱 등록에 **SPA 리디렉션 URI**가 있어야 한다 —
 * 그래야 Microsoft가 이 출처의 토큰 요청을 CORS로 받아 준다.
 */

import { getEmail } from './auth';

const SCOPE = 'Mail.Read User.Read';
const GRAPH = 'https://graph.microsoft.com/v1.0';
// 서버(services/personal.py MAIL_SYNC_COUNT)와 같은 값.
const MAIL_COUNT = 100;
const LOGIN_TIMEOUT_MS = 5 * 60 * 1000;

// 토큰은 **콘솔 사용자별**이다 — 같은 탭에서 다른 사람이 콘솔에 로그인하면 앞사람의 메일
// 토큰을 이어 쓰지 않고 그 사람이 자기 계정으로 다시 로그인한다.
let cached: { user: string; clientId: string; token: string; expires: number } | null = null;

/** 앱 등록에 넣을 리디렉션 URI — 콘솔이 놓인 자리(IIS 서브패스 포함) 옆의 빈 페이지. */
export function redirectUri(): string {
  return new URL('ms-login.html', window.location.origin + window.location.pathname).toString();
}

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export async function pkcePair(): Promise<{ verifier: string; challenge: string }> {
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return { verifier, challenge: base64url(new Uint8Array(digest)) };
}

/** 팝업이 리디렉션 페이지(같은 출처)로 돌아올 때까지 기다려 그 쿼리를 읽는다.
 *  Microsoft 쪽에 있는 동안은 다른 출처라 location을 읽으면 예외가 난다 — 그게 정상이다. */
function waitForRedirect(popup: Window, target: string): Promise<URLSearchParams> {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const timer = window.setInterval(() => {
      if (popup.closed) {
        window.clearInterval(timer);
        reject(new Error('로그인 창이 닫혔습니다.'));
        return;
      }
      if (Date.now() - started > LOGIN_TIMEOUT_MS) {
        window.clearInterval(timer);
        popup.close();
        reject(new Error('로그인이 시간 안에 끝나지 않았습니다.'));
        return;
      }
      let href = '';
      try {
        href = popup.location.href;
      } catch {
        return;
      }
      if (!href.startsWith(target)) return;
      window.clearInterval(timer);
      popup.close();
      resolve(new URL(href).searchParams);
    }, 400);
  });
}

async function login(clientId: string, tenant: string): Promise<string> {
  const user = getEmail();
  if (cached && cached.user === user && cached.clientId === clientId && cached.expires > Date.now()) {
    return cached.token;
  }
  // 팝업은 await보다 먼저 연다 — 클릭에서 멀어지면 브라우저가 팝업을 막는다.
  const popup = window.open('', 'paas-ms-login', 'width=520,height=680');
  if (!popup) throw new Error('팝업이 막혔습니다 — 이 사이트의 팝업을 허용하고 다시 누르세요.');
  const authority = `https://login.microsoftonline.com/${encodeURIComponent(tenant)}/oauth2/v2.0`;
  const redirect = redirectUri();
  const { verifier, challenge } = await pkcePair();
  const state = base64url(crypto.getRandomValues(new Uint8Array(16)));
  const url = `${authority}/authorize?${new URLSearchParams({
    client_id: clientId, response_type: 'code', redirect_uri: redirect, response_mode: 'query',
    scope: SCOPE, state, code_challenge: challenge, code_challenge_method: 'S256',
    // 브라우저에 다른 Microsoft 계정 세션이 살아 있어도 콘솔 사용자의 계정으로 로그인하게 한다.
    ...(user ? { login_hint: user } : {}),
  })}`;
  popup.location.href = url;
  const params = await waitForRedirect(popup, redirect);
  if (params.get('error')) {
    throw new Error(`메일 로그인 실패: ${params.get('error_description') || params.get('error')}`);
  }
  if (params.get('state') !== state || !params.get('code')) {
    throw new Error('메일 로그인 응답이 이 요청의 것이 아닙니다 — 다시 시도하세요.');
  }
  const res = await fetch(`${authority}/token`, {
    method: 'POST',
    body: new URLSearchParams({
      client_id: clientId, grant_type: 'authorization_code', code: params.get('code')!,
      redirect_uri: redirect, code_verifier: verifier, scope: SCOPE,
    }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(`메일 토큰을 받지 못했습니다: ${body.error_description || body.error}`);
  // 만료 1분 전까지만 다시 쓴다.
  cached = { user, clientId, token: body.access_token, expires: Date.now() + (Number(body.expires_in) - 60) * 1000 };
  return cached.token;
}

async function graph<T>(token: string, path: string): Promise<T> {
  const res = await fetch(`${GRAPH}${path}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      // 본문을 HTML이 아니라 텍스트로 받는다 — 색인·온톨로지는 텍스트를 본다.
      Prefer: 'outlook.body-content-type="text"',
    },
  });
  if (res.status === 401) cached = null;
  if (!res.ok) throw new Error(`Microsoft Graph가 HTTP ${res.status}로 답했습니다: ${(await res.text()).slice(0, 300)}`);
  return res.json() as Promise<T>;
}

/** 로그인하고 받은편지함 최신 메일을 읽는다. 결과는 그대로 서버(/personal/mail/messages)로 보낸다. */
export async function fetchInbox(clientId: string, tenant: string): Promise<{ account: string; messages: object[] }> {
  const token = await login(clientId, tenant);
  const me = await graph<{ mail?: string; userPrincipalName?: string }>(token, '/me?$select=mail,userPrincipalName');
  const inbox = await graph<{ value: object[] }>(
    token,
    `/me/mailFolders/inbox/messages?$top=${MAIL_COUNT}&$orderby=receivedDateTime desc` +
      '&$select=id,subject,from,toRecipients,receivedDateTime,body,webLink',
  );
  return { account: me.mail || me.userPrincipalName || '', messages: inbox.value };
}
