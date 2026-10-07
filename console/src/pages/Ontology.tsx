import { NavLink, Outlet } from 'react-router-dom';

/**
 * 온톨로지 관리 — 탭 둘.
 *
 *  - **전환 현황**: 문서가 그래프로 얼마나 옮겨졌고 어디서 멈췄는지(집계·퍼널·행렬).
 *  - **정보 조회**: 옮겨진 것을 실제로 들여다본다(검색 → 이웃 → 요구 시 확장).
 *
 * 둘을 한 화면에 섞지 않는 이유는 질문이 다르기 때문이다. 현황은 "어디를 고쳐야 하나"이고
 * 조회는 "이것이 무엇과 이어져 있나"다 — 전자는 전체 분포라 집계가 맞고, 후자는 국소
 * 구조라 노드-링크가 맞는다(그래서 쓰는 표현도 다르다).
 */
const TABS: [string, string][] = [
  ['/ontology', '전환 현황'],
  ['/ontology/explore', '정보 조회'],
];

export default function Ontology() {
  return (
    <>
      <div className="tabs">
        {TABS.map(([path, label]) => (
          <NavLink key={path} to={path} end={path === '/ontology'}>
            {label}
          </NavLink>
        ))}
      </div>
      <Outlet />
    </>
  );
}
