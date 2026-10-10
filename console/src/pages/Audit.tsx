import { NavLink, Outlet } from 'react-router-dom';

/**
 * 작업 로그 — 탭 셋.
 *
 *  - **기록**: 사람이 지시한 일 한 줄씩(배포·키 발급·승인 — audit_events).
 *  - **대시보드**: 그 안에서 실제로 일어난 모델 호출의 숫자(llm_calls) — 느려졌는지,
 *    실패하는지, 토큰을 얼마나 쓰는지.
 *  - **평가**: 그 답이 도움이 됐는지 — 사람이 누른 좋음·아쉬움(answer_ratings).
 *    숫자(지연·토큰)로는 "빠른 헛소리"가 좋아 보인다. 품질은 따로 재야 한다.
 *
 * 한 화면에 섞지 않는 이유는 단위가 다르기 때문이다. 기록은 사건 하나하나를 보는 자리고,
 * 대시보드는 분포를 보는 자리다 — 수천 건의 모델 호출을 기록처럼 늘어놓으면 사람이 지시한
 * 일이 그 속에 묻힌다(그래서 모델 호출은 audit_events에 넣지 않았다).
 */
const TABS: [string, string][] = [
  ['/audit', '기록'],
  ['/audit/dashboard', '대시보드'],
  ['/audit/assessment', '평가'],
];

export default function Audit() {
  return (
    <>
      <div className="tabs">
        {TABS.map(([path, label]) => (
          <NavLink key={path} to={path} end={path === '/audit'}>
            {label}
          </NavLink>
        ))}
      </div>
      <Outlet />
    </>
  );
}
