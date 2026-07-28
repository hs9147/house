import { useState, useEffect } from 'react';
import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { getEmail, isAdmin, logout } from '../lib/auth';
import { useApi } from '../lib/hooks';

export default function Layout() {
  const navigate = useNavigate();
  const admin = isAdmin();
  const email = getEmail();

  const orgs = useApi(() => api.listOrgs());
  const [selectedOrgId, setSelectedOrgId] = useState<string>(
    sessionStorage.getItem('paas_selected_org_id') ?? ''
  );

  useEffect(() => {
    if (selectedOrgId) {
      sessionStorage.setItem('paas_selected_org_id', selectedOrgId);
    } else {
      sessionStorage.removeItem('paas_selected_org_id');
    }
    // 페이지 구성 필터링 트리거용 세션 이벤트
    window.dispatchEvent(new Event('paas_org_changed'));
  }, [selectedOrgId]);

  // 설치 빌드옵션(기능 모듈·호스트 OS)에 맞춰 메뉴를 구성한다
  const health = useApi(() => api.health());
  const features = health.data?.features ?? [];
  const has = (f: string) => features.includes(f);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <h1 style={{ marginBottom: 8 }}>PaaS 콘솔</h1>

        {/* 🏢 글로벌 조직 선택 필터 */}
        <div style={{ padding: '0 0 12px 0', borderBottom: '1px solid rgba(255,255,255,0.1)', marginBottom: 12 }}>
          <label style={{ fontSize: 12, display: 'block', color: '#888', marginBottom: 4 }}>
            소속 / 대상 조직
          </label>
          <select
            value={selectedOrgId}
            onChange={(e) => setSelectedOrgId(e.target.value)}
            style={{ width: '100%', padding: '6px 8px', borderRadius: 4, background: '#222', color: '#fff', border: '1px solid #444' }}
          >
            <option value="">전체 조직 보기</option>
            {orgs.data?.map((o) => (
              <option key={o.id} value={String(o.id)}>
                {o.name}
              </option>
            ))}
          </select>
        </div>

        <nav>
          {admin && <NavLink to="/">대시보드</NavLink>}
          {admin && <NavLink to="/orgs">조직</NavLink>}
          <NavLink to="/projects">프로젝트</NavLink>
          {admin && <NavLink to="/modules">모듈</NavLink>}
          {admin && <NavLink to="/storage">파일 관리</NavLink>}
          {admin && has('deploy') && <NavLink to="/server-config">서버구성</NavLink>}
          {admin && has('workspace') && <NavLink to="/providers">LLM</NavLink>}
          {has('workspace') && <NavLink to="/chat">에이전트 빌더</NavLink>}
          {admin && <NavLink to="/audit">작업 로그</NavLink>}
          {admin && <NavLink to="/powershell">PowerShell</NavLink>}
        </nav>
        <div className="sidebar-footer">
          {health.data && (
            <span className="status dim" title={`tier=${health.data.tier}`}>
              {health.data.host_os}
            </span>
          )}
          <span className="mutedtext" style={{ fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={email || (admin ? 'admin' : 'member')}>
            {email || (admin ? 'admin' : 'member')}
          </span>
          <button
            className="secondary small"
            onClick={() => {
              logout();
              navigate('/login');
            }}
          >
            로그아웃
          </button>
        </div>
      </aside>
      <main>
        <Outlet />
      </main>
    </div>
  );
}
