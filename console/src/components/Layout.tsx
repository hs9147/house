import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { getEmail, isAdmin, logout } from '../lib/auth';
import { useApi } from '../lib/hooks';

export default function Layout() {
  const navigate = useNavigate();
  const admin = isAdmin();
  const email = getEmail();



  // 설치 빌드옵션(기능 모듈·호스트 OS)에 맞춰 메뉴를 구성한다
  const health = useApi(() => api.health());
  const me = useApi(() => api.me());
  const features = health.data?.features ?? [];
  const has = (f: string) => features.includes(f);
  const systemName = health.data?.platform_name || (import.meta as any).env?.VITE_SYSTEM_NAME || 'house';

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <h1 style={{ marginBottom: 8 }}>{systemName} 콘솔</h1>

        {/* 🏢 소속 조직 다수 뱃지 표시 */}
        <div style={{ padding: '0 0 12px 0', borderBottom: '1px solid rgba(255,255,255,0.1)', marginBottom: 12 }}>
          <div style={{ fontSize: 11, color: '#888', marginBottom: 4 }}>소속 조직</div>
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {me.data?.organizations && me.data.organizations.length > 0 ? (
              me.data.organizations.map((o) => (
                <span
                  key={o.id}
                  style={{
                    fontSize: 11,
                    padding: '2px 8px',
                    borderRadius: 12,
                    background: 'rgba(56, 189, 248, 0.15)',
                    color: '#38bdf8',
                    border: '1px solid rgba(56, 189, 248, 0.3)',
                    fontWeight: 600,
                  }}
                >
                  🏢 {o.name}
                </span>
              ))
            ) : (
              <span style={{ fontSize: 12, fontWeight: 600, color: '#38bdf8' }}>
                🏢 {me.data?.organization_name || '기본 조직'}
              </span>
            )}
          </div>
        </div>

        <nav>
          {admin && <NavLink to="/">대시보드</NavLink>}
          {admin && <NavLink to="/accounts">계정 관리</NavLink>}
          {admin && <NavLink to="/orgs">조직 관리</NavLink>}
          <NavLink to="/projects">프로젝트</NavLink>
          {has('workspace') && <NavLink to="/planning">에이전트 기획</NavLink>}
          {admin && <NavLink to="/modules">모듈 관리</NavLink>}
          {admin && <NavLink to="/storage">파일 관리</NavLink>}
          {admin && has('workspace') && <NavLink to="/providers">LLM 관리</NavLink>}
          {admin && has('deploy') && <NavLink to="/server-config">서버구성</NavLink>}
          {admin && <NavLink to="/powershell">서버연결</NavLink>}
          {admin && <NavLink to="/audit">작업 로그</NavLink>}
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
