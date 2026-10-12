import { NavLink, Outlet } from 'react-router-dom';

/**
 * 워크플로 관리 — 탭 둘.
 *
 *  - **워크플로**: 조직마다의 워크플로 목록과 구성(무엇을 어떤 순서로 하는가).
 *  - **스마트워크**: 그 워크플로를 실제로 돌리는 자리 — 업무 하나를 대화로 진행한다.
 *
 * 같은 화면 아래 둔 이유는 둘이 한 일의 앞뒤이기 때문이다. 워크플로를 짜 두고 쓰지 않으면
 * 아무 일도 일어나지 않고, 스마트워크의 제안은 부서 워크플로에서 나온다 — 메뉴에서 둘이
 * 멀리 떨어져 있으면 그 연결이 보이지 않는다.
 */
const TABS: [string, string][] = [
  ['/workflows', '워크플로'],
  ['/workflows/smartwork', '스마트워크'],
];

export default function Workflows() {
  return (
    <>
      <div className="tabs">
        {TABS.map(([path, label]) => (
          <NavLink key={path} to={path} end={path === '/workflows'}>
            {label}
          </NavLink>
        ))}
      </div>
      <Outlet />
    </>
  );
}
