import { useOutletContext } from 'react-router-dom';
import Async from '../../components/Async';
import StatusPill from '../../components/StatusPill';
import { api } from '../../lib/api';
import { fmtDate, shortSha } from '../../lib/format';
import { useApi } from '../../lib/hooks';
import type { ProjectContext } from '../ProjectDetail';

export default function DeploymentsTab() {
  const { project } = useOutletContext<ProjectContext>();
  const state = useApi(() => api.deployments(project.id), [project.id]);

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>배포 이력</h2>
        <div className="spacer" />
        <button className="secondary small" onClick={state.reload}>
          새로고침
        </button>
      </div>
      <Async state={state} empty="배포 이력이 없습니다.">
        {(rows) => (
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>커밋</th>
                <th>이미지</th>
                <th>프로필</th>
                <th>상태</th>
                <th>시작</th>
                <th>완료</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((d) => (
                <tr key={d.id}>
                  <td className="mono">{d.id}</td>
                  <td className="mono">{shortSha(d.git_sha)}</td>
                  <td className="mono">{d.image_tag || '-'}</td>
                  <td><StatusPill value={d.profile} /></td>
                  <td>
                    <StatusPill value={d.status} />
                    {d.error && (
                      <div className="error" style={{ marginTop: 4, fontSize: 12 }}>
                        {d.error.slice(0, 300)}
                      </div>
                    )}
                  </td>
                  <td className="mono">{fmtDate(d.created_at)}</td>
                  <td className="mono">{fmtDate(d.finished_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
    </div>
  );
}
