import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { isAdmin, logout } from '../lib/auth';
import { useApi } from '../lib/hooks';

export default function Layout() {
  const navigate = useNavigate();
  const admin = isAdmin();
  // 설치 빌드옵션(기능 모듈·호스트 OS)에 맞춰 메뉴를 구성한다
  const health = useApi(() => api.health());
  const features = health.data?.features ?? [];
  const has = (f: string) => features.includes(f);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <h1>PaaS 콘솔</h1>
        <nav>
          {admin && <NavLink to="/">대시보드</NavLink>}
          {admin && <NavLink to="/orgs">조직</NavLink>}
          <NavLink to="/projects">프로젝트</NavLink>
          {health.data?.gitea_url && <NavLink to="/git">Git</NavLink>}
          <NavLink to="/modules">모듈</NavLink>
          {has('deploy') && <NavLink to="/server-config">서버구성</NavLink>}
          {has('workspace') && <NavLink to="/providers">LLM</NavLink>}
          {has('workspace') && <NavLink to="/chat">채팅</NavLink>}
          {admin && <NavLink to="/audit">감사 로그</NavLink>}
        </nav>
        <div className="sidebar-footer">
          {health.data && (
            <span className="status dim" title={`tier=${health.data.tier}`}>
              {health.data.host_os}
            </span>
          )}
          <span className="mutedtext" style={{ fontSize: 12 }}>
            {admin ? 'admin' : 'member'}
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
