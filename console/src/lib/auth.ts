// x-api-key를 sessionStorage에 보관 (기존 admin/mail 대시보드 관례).
// 로그인 검증은 admin 전용 GET /status를 프로브로 재사용:
//   200 = admin 키, 403 = 유효한 일반 키, 401 = 무효.
const KEY = 'paas_console_key';
const ADMIN = 'paas_console_admin';
const EMAIL = 'paas_console_email';

export function getKey(): string {
  return sessionStorage.getItem(KEY) ?? '';
}

export function getEmail(): string {
  return sessionStorage.getItem(EMAIL) ?? '';
}

export function isAdmin(): boolean {
  return sessionStorage.getItem(ADMIN) === '1';
}

export function isLoggedIn(): boolean {
  return getKey() !== '';
}

export function logout(): void {
  const token = getKey();
  sessionStorage.removeItem(KEY);
  sessionStorage.removeItem(ADMIN);
  sessionStorage.removeItem(EMAIL);
  sessionStorage.removeItem('paas_selected_org_id');
  // 저장소만 비우면 토큰은 서버에서 계속 유효하다 — 폐기를 요청한다(응답은 기다리지 않는다).
  if (token) {
    void fetch('/paas/api/v1/auth/logout', {
      method: 'POST',
      headers: { 'x-api-key': token },
    }).catch(() => {});
  }
}

/**
 * 계정 로그인. 성공한 서버 응답만 세션을 만든다 — 서버가 거절하거나 닿지 않으면 던진다.
 *
 * 비밀번호는 TLS 위로 원문을 보내고 서버가 솔트 + scrypt로 검증한다. 예전에는 여기서
 * SHA-256을 걸어 "암호화"라 불렀지만, 그건 암호화가 아닐뿐더러 그 해시가 그대로
 * x-api-key가 되어 비밀번호의 결정적 함수가 무기한 자격증명이 되는 문제가 있었다.
 * 로그인 응답으로 받는 것은 비밀번호와 무관한 난수 세션 토큰이다.
 */
export async function loginWithAccount(
  email: string,
  secret: string,
): Promise<{ admin: boolean; email: string }> {
  const cleanEmail = email.trim();
  const password = secret.trim();

  let res: Response;
  try {
    res = await fetch('/paas/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: cleanEmail, password }),
    });
  } catch {
    throw new Error('서버에 연결할 수 없습니다. 잠시 후 다시 시도하세요.');
  }

  if (!res.ok) {
    let detail = `로그인에 실패했습니다 (HTTP ${res.status})`;
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* 본문 없는 응답 */
    }
    throw new Error(detail);
  }

  const data = await res.json();
  if (!data.key) throw new Error('로그인 응답에 인증 키가 없습니다.');

  const admin = Boolean(data.is_admin);
  sessionStorage.setItem(KEY, data.key);
  sessionStorage.setItem(ADMIN, admin ? '1' : '0');
  sessionStorage.setItem(EMAIL, cleanEmail || data.email);
  return { admin, email: cleanEmail || data.email };
}

export async function login(key: string): Promise<{ admin: boolean }> {
  return loginWithAccount('', key);
}
