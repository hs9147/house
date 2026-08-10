// 내장 OIDC Provider(SSO)가 미로그인 사용자를 로그인 화면으로 보낼 때 붙이는 복귀
// 주소를 검증하는 순수 로직 — Login.tsx가 쓴다.
//
// 로그인 뒤 이 주소로 되돌아가야 Gitea 등에서 시작한 SSO 흐름이 이어진다
// (app/services/oidc_provider.py의 login_redirect_url이 ?next=... 로 붙인다).
// 다만 이 값은 URL 쿼리로 들어오므로 그대로 믿고 이동하면 open redirect가 된다 —
// 공격자가 로그인 링크에 ?next=https://evil.example 을 심어 보내면, 사용자는 진짜
// 로그인 화면에서 진짜로 로그인한 직후 공격자 사이트로 튕겨 나간다.

/**
 * @param next URL 쿼리에서 읽은 복귀 주소(없으면 null)
 * @returns 이동해도 안전한 주소, 아니면 null(호출자는 평소 목적지로 보내야 한다)
 */
export function safeNextUrl(next: string | null): string | null {
  // 같은 오리진의 절대경로만 허용한다. "//evil.example"은 프로토콜 상대 URL이라
  // "/"로 시작하면서도 외부로 나가므로 반드시 함께 막아야 한다.
  if (!next || !next.startsWith('/') || next.startsWith('//')) return null;
  // "/\evil.example" 처럼 역슬래시를 쓰는 변형도 일부 브라우저가 "//"로 해석한다.
  if (next.startsWith('/\\')) return null;
  return next;
}
