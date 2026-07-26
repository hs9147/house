import { NavLink, Outlet, useParams } from 'react-router-dom';
import Async from '../components/Async';
import StatusPill from '../components/StatusPill';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';
import type { ProjectOut } from '../lib/types';

export interface ProjectContext {
  project: ProjectOut;
}

const TABS = [
  ['overview', '개요'],
  ['code', '코드'],
  ['deployments', '배포 이력'],
  ['logs', '로그'],
  ['env', '환경변수'],
  ['modules', '모듈'],
  ['previews', '프리뷰'],
] as const;

export default function ProjectDetail() {
  const { id } = useParams();
  const projectId = Number(id);
  // 백엔드에 단건 조회가 없어 목록에서 찾는다
  const state = useApi(async () => {
    const list = await api.listProjects();
    const project = list.find((p) => p.id === projectId);
    if (!project) throw new Error('프로젝트를 찾을 수 없습니다.');
    return project;
  }, [projectId]);

  return (
    <Async state={state}>
      {(project) => (
        <>
          <div className="row" style={{ marginBottom: 12 }}>
            <h2 style={{ margin: 0 }}>{project.name}</h2>
            <StatusPill value={project.type} />
            <span className="mono mutedtext">
              {project.git_url} @ {project.branch}
            </span>
          </div>
          <div className="tabs">
            {TABS.map(([path, label]) => (
              <NavLink key={path} to={path}>
                {label}
              </NavLink>
            ))}
          </div>
          <Outlet context={{ project } satisfies ProjectContext} />
        </>
      )}
    </Async>
  );
}
