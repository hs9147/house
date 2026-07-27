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
  sessionStorage.removeItem(KEY);
  sessionStorage.removeItem(ADMIN);
  sessionStorage.removeItem(EMAIL);
  sessionStorage.removeItem('paas_selected_org_id');
}

export async function loginWithAccount(email: string, key: string): Promise<{ admin: boolean; email: string }> {
  // 계정 이메일 기본 로그인
  const cleanEmail = email.trim();
  const cleanKey = key.trim();

  // /paas/api/v1/auth/me 또는 /paas/status 프로브 검증
  const res = await fetch('/paas/api/v1/auth/me', { headers: { 'x-api-key': cleanKey } });
  if (res.status === 200) {
    const data = await res.json();
    const admin = Boolean(data.is_admin);
    sessionStorage.setItem(KEY, cleanKey);
    sessionStorage.setItem(ADMIN, admin ? '1' : '0');
    sessionStorage.setItem(EMAIL, cleanEmail);
    return { admin, email: cleanEmail };
  }
  
  // 백업: status 엔드포인트 프로브
  const statusRes = await fetch('/paas/status', { headers: { 'x-api-key': cleanKey } });
  if (statusRes.status === 200 || statusRes.status === 403) {
    const admin = statusRes.status === 200;
    sessionStorage.setItem(KEY, cleanKey);
    sessionStorage.setItem(ADMIN, admin ? '1' : '0');
    sessionStorage.setItem(EMAIL, cleanEmail);
    return { admin, email: cleanEmail };
  }
  throw new Error('인증 정보가 유효하지 않습니다.');
}

export async function login(key: string): Promise<{ admin: boolean }> {
  return loginWithAccount('', key);
}
