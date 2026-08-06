// 새 프로젝트를 만들 조직을 정하는 순수 로직 — Projects.tsx의 CreateModal이 쓴다.
//
// 회귀: 이 계산을 컴포넌트 마운트 시점에 useState(() => ...) 초기값으로 한 번만 잡으면,
// 그 순간엔 /me 응답(userOrgs)이 아직 없어 늘 빈 배열이다. 결과가 '1' 같은 임의 기본값에
// 박히고, /me가 나중에 응답해도 그 값은 다시 계산되지 않는다 — 화면은 사용자의 실제
// 소속 조직 이름을 보여주면서, 실제 제출은 엉뚱한(먼저 있던) 조직으로 나간다.
// 그래서 이 함수는 매 렌더마다 다시 불러 최신 userOrgs를 반영해야 한다.

export interface OrgRef {
  id: number;
  name: string;
}

/**
 * @param selectedOrgId 사용자가 드롭다운에서 직접 고른 값(빈 문자열 = 아직 안 골랐음)
 * @param userOrgs 지금까지 확인된 사용자의 소속 조직 목록(/me 응답 기반)
 * @returns 제출에 쓸 조직 id. 아직 아무것도 확인되지 않았으면 빈 문자열
 *          (호출자는 이 경우 제출을 막아야 한다 — 임의 기본값으로 대체하지 않는다).
 */
export function resolveTargetOrgId(selectedOrgId: string, userOrgs: OrgRef[]): string {
  return selectedOrgId || (userOrgs.length > 0 ? String(userOrgs[0].id) : '');
}
