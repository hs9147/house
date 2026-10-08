import { describe, expect, it } from 'vitest';
import { pkcePair } from '../lib/msgraph';

// Microsoft가 challenge를 다시 계산해 맞춰 보므로 한 글자라도 다르면 로그인이 토큰 교환에서 실패한다.
describe('pkcePair', () => {
  it('derives an S256 base64url challenge from the verifier', async () => {
    const { verifier, challenge } = await pkcePair();
    expect(verifier).toMatch(/^[A-Za-z0-9_-]{43}$/);
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
    const expected = btoa(String.fromCharCode(...digest)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    expect(challenge).toBe(expected);
    expect(challenge).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });
});
