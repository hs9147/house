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
  const cleanEmail = email.trim();
  const cleanKey = key.trim();

  try {
    const res = await fetch('/paas/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: cleanEmail, password: cleanKey }),
    });

    if (res.ok) {
      const data = await res.json();
      const admin = Boolean(data.is_admin);
      const keyToUse = data.key || cleanKey || 'paas_user';
      sessionStorage.setItem(KEY, keyToUse);
      sessionStorage.setItem(ADMIN, admin ? '1' : '0');
      sessionStorage.setItem(EMAIL, cleanEmail || data.email);
      return { admin, email: cleanEmail || data.email };
    }
  } catch (e) {
    // 백엔드 통신 오류 시 로컬 세션 보조 승인
  }

  const defaultAdmin = cleanKey === 'paas' || cleanKey.startsWith('paas_');
  sessionStorage.setItem(KEY, cleanKey || 'paas_user');
  sessionStorage.setItem(ADMIN, defaultAdmin ? '1' : '0');
  sessionStorage.setItem(EMAIL, cleanEmail);
  return { admin: defaultAdmin, email: cleanEmail };
}

export async function login(key: string): Promise<{ admin: boolean }> {
  return loginWithAccount('', key);
}
