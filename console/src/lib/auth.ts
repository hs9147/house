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

function rightRotate(value: number, amount: number): number {
  return (value >>> amount) | (value << (32 - amount));
}

function sha256Pure(ascii: string): string {
  const mathPow = Math.pow;
  const maxWord = mathPow(2, 32);
  let result = '';
  const words: number[] = [];
  const asciiBitLength = ascii.length * 8;
  const hash: number[] = [];
  const k: number[] = [];
  const isComposite: Record<number, boolean> = {};

  let primeCounter = 0;
  for (let candidate = 2; primeCounter < 64; candidate++) {
    if (!isComposite[candidate]) {
      for (let i = 0; i < 300; i += candidate) {
        isComposite[i] = true;
      }
      if (primeCounter < 8) {
        hash[primeCounter] = (mathPow(candidate, 1 / 2) * maxWord) | 0;
      }
      k[primeCounter] = (mathPow(candidate, 1 / 3) * maxWord) | 0;
      primeCounter++;
    }
  }

  let str = ascii + '\x80';
  while (str.length % 64 !== 56) str += '\x00';
  for (let i = 0; i < str.length; i++) {
    const j = str.charCodeAt(i);
    words[i >> 2] |= j << ((3 - i) % 4 * 8);
  }
  words[words.length] = (asciiBitLength / maxWord) | 0;
  words[words.length] = asciiBitLength | 0;

  for (let j = 0; j < words.length;) {
    const w = words.slice(j, j += 16);
    const oldHash = hash.slice(0);

    for (let i = 0; i < 64; i++) {
      const w15 = w[i - 15], w2 = w[i - 2];
      const a = hash[0], e = hash[4];
      const temp1 = hash[7]
        + (rightRotate(e, 6) ^ rightRotate(e, 11) ^ rightRotate(e, 25))
        + ((e & hash[5]) ^ (~e & hash[6]))
        + k[i]
        + (w[i] = (i < 16) ? w[i] : (
            w[i - 16]
            + (rightRotate(w15, 7) ^ rightRotate(w15, 18) ^ (w15 >>> 3))
            + w[i - 7]
            + (rightRotate(w2, 17) ^ rightRotate(w2, 19) ^ (w2 >>> 10))
          ) | 0
        );
      const temp2 = (rightRotate(a, 2) ^ rightRotate(a, 13) ^ rightRotate(a, 22))
        + ((a & hash[1]) ^ (a & hash[2]) ^ (hash[1] & hash[2]));

      hash.unshift((temp1 + temp2) | 0);
      hash[4] = (hash[4] + temp1) | 0;
      hash.pop();
    }

    for (let i = 0; i < 8; i++) {
      hash[i] = (hash[i] + oldHash[i]) | 0;
    }
  }

  for (let i = 0; i < 8; i++) {
    for (let j = 3; j >= 0; j--) {
      const b = (hash[i] >> (j * 8)) & 255;
      result += (b < 16 ? '0' : '') + b.toString(16);
    }
  }
  return result;
}

export async function hashPassword(plainText: string): Promise<string> {
  if (!plainText) return '';
  if (typeof crypto !== 'undefined' && crypto && crypto.subtle && typeof crypto.subtle.digest === 'function') {
    try {
      const encoder = new TextEncoder();
      const data = encoder.encode(plainText);
      const hashBuffer = await crypto.subtle.digest('SHA-256', data);
      const hashArray = Array.from(new Uint8Array(hashBuffer));
      return hashArray.map((b) => b.toString(16).padStart(2, '0')).join('');
    } catch {
      // Fallback below
    }
  }
  return sha256Pure(plainText);
}

/**
 * 계정 로그인. 성공한 서버 응답만 세션을 만든다 — 서버가 거절하거나 닿지 않으면 던진다.
 *
 * rawKey는 발급된 API 키(또는 관리자 키)로 로그인할 때 쓴다. 비밀번호는 해시해서 보내지만
 * API 키는 서버가 원문을 해시해 대조하므로(hash_key(원문) == key_hash), 여기서 한 번 더
 * 해시하면 영영 매칭되지 않는다.
 */
export async function loginWithAccount(
  email: string,
  secret: string,
  opts: { rawKey?: boolean } = {},
): Promise<{ admin: boolean; email: string }> {
  const cleanEmail = email.trim();
  const cleanSecret = secret.trim();
  const password = opts.rawKey ? cleanSecret : await hashPassword(cleanSecret);

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
  return loginWithAccount('', key, { rawKey: true });
}
