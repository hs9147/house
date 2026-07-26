// x-api-key를 sessionStorage에 보관 (기존 admin/mail 대시보드 관례).
// 로그인 검증은 admin 전용 GET /status를 프로브로 재사용:
//   200 = admin 키, 403 = 유효한 일반 키, 401 = 무효.
const KEY = 'paas_console_key';
const ADMIN = 'paas_console_admin';

export function getKey(): string {
  return sessionStorage.getItem(KEY) ?? '';
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
}

export async function login(key: string): Promise<{ admin: boolean }> {
  const res = await fetch('/paas/status', { headers: { 'x-api-key': key } });
  if (res.status === 200 || res.status === 403) {
    const admin = res.status === 200;
    sessionStorage.setItem(KEY, key);
    sessionStorage.setItem(ADMIN, admin ? '1' : '0');
    return { admin };
  }
  throw new Error('잘못된 API 키입니다.');
}
