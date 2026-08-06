import { describe, expect, it } from 'vitest';
import { resolveTargetOrgId } from '../lib/projectOrg';

describe('resolveTargetOrgId', () => {
  it('사용자가 직접 고른 값이 있으면 그것을 쓴다', () => {
    expect(resolveTargetOrgId('7', [{ id: 1, name: 'a' }])).toBe('7');
  });

  it('아직 고르지 않았으면 소속 조직 중 첫 번째로 떨어진다', () => {
    expect(resolveTargetOrgId('', [{ id: 5, name: 'shop-team' }])).toBe('5');
  });

  it('소속 조직 정보가 아직 없으면 빈 문자열 — 임의 기본값으로 대체하지 않는다', () => {
    expect(resolveTargetOrgId('', [])).toBe('');
  });

  // 회귀: /me가 마운트 후 응답해 userOrgs가 비어있다가 채워지는 상황을 그대로 재현한다.
  // 이 계산을 useState(() => ...) 초기값으로 한 번만 했다면, /me가 나중에 응답해도
  // selectedOrgId는 '1' 같은 값에 멈춰 있어 실제 소속 조직으로 갱신되지 않았다.
  it('/me가 늦게 응답해도 다음 렌더에서 정확한 소속 조직으로 갱신된다', () => {
    const beforeMeLoads = resolveTargetOrgId('', []); // 마운트 시점: /me 아직 없음
    expect(beforeMeLoads).toBe('');

    const afterMeLoads = resolveTargetOrgId(beforeMeLoads, [{ id: 42, name: 'real-org' }]);
    expect(afterMeLoads).toBe('42'); // 사용자의 실제 소속 조직으로 정확히 떨어진다
  });

  it('다수 소속 조직 중 드롭다운에서 다른 조직을 고르면 그 선택이 우선한다', () => {
    const userOrgs = [{ id: 1, name: 'a' }, { id: 2, name: 'b' }];
    expect(resolveTargetOrgId('', userOrgs)).toBe('1'); // 기본값: 첫 번째
    expect(resolveTargetOrgId('2', userOrgs)).toBe('2'); // 사용자가 직접 고른 값
  });
});
