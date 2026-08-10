import { describe, expect, it } from 'vitest';
import { safeNextUrl } from '../lib/ssoReturn';

describe('safeNextUrl', () => {
  it('SSO가 붙여준 백엔드 복귀 경로는 그대로 통과시킨다', () => {
    const next = '/paas/oauth2/authorize?client_id=gitea&redirect_uri=https%3A%2F%2Fgit.example.com%2Fcb';
    expect(safeNextUrl(next)).toBe(next);
  });

  it('next가 없으면 null — 호출자는 평소 목적지로 보낸다', () => {
    expect(safeNextUrl(null)).toBeNull();
    expect(safeNextUrl('')).toBeNull();
  });

  it('절대 URL은 거부한다 — 로그인 직후 외부로 튕기는 open redirect가 된다', () => {
    expect(safeNextUrl('https://evil.example/steal')).toBeNull();
    expect(safeNextUrl('http://evil.example')).toBeNull();
  });

  // 회귀: "//evil.example"은 "/"로 시작해 경로처럼 보이지만 프로토콜 상대 URL이라
  // 브라우저가 외부 호스트로 이동시킨다 — startsWith('/') 검사만으로는 못 막는다.
  it('프로토콜 상대 URL(//host)을 거부한다', () => {
    expect(safeNextUrl('//evil.example/steal')).toBeNull();
  });

  it('역슬래시 변형(/\\host)도 거부한다', () => {
    expect(safeNextUrl('/\\evil.example')).toBeNull();
  });
});
